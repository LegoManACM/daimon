"""Updates: checking GitHub for a newer release, and installing it (`daimon update`, or by itself when
`updates.auto` is on). The install runs in a small script of its own, after Daimon has stopped: Windows won't
replace a running program's files, and the background service has to stop for it anyway."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from ..home import Home
from . import service

REPO = "LegoManACM/daimon"
CHECK_EVERY_S = 24 * 3600
ADDONS = ("daimon_canvas", "daimon_messages", "daimon_calendar")


def current() -> str:
    try:
        return importlib.metadata.version("daimon")
    except importlib.metadata.PackageNotFoundError:
        return "0"


def parse(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3])


def newer(candidate: str, than: str) -> bool:
    return parse(candidate) > parse(than)


def local_dir() -> Path | None:
    """Release files on disk (DAIMON_RELEASE_DIR), for testing a build before it's published."""
    d = os.environ.get("DAIMON_RELEASE_DIR")
    return Path(d).expanduser().resolve() if d else None


def latest() -> str:
    """The newest release's version. Raises ConnectionError if GitHub can't be reached."""
    if (d := local_dir()) is not None:
        found = sorted(d.glob("daimon-*-py3-none-any.whl"))
        return found[-1].name.removeprefix("daimon-").removesuffix("-py3-none-any.whl") if found else "0"
    try:
        r = httpx.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=15,
                      headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return str(r.json()["tag_name"]).lstrip("v")
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise ConnectionError(f"couldn't check GitHub for updates ({type(e).__name__})") from None


def settings(home: Home) -> tuple[bool, bool]:
    """(check, auto) from config.yaml's `updates`: check on by default, auto off."""
    cfg = home.config().get("updates") or {}
    return cfg.get("check", True) is not False, cfg.get("auto", False) is True


def available(home: Home, force: bool = False) -> str | None:
    """A newer version if there is one, checking GitHub at most once a day (cached in .cache/update.json)."""
    path = home.cache / "update.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if force or time.time() - seen.get("checked", 0) > CHECK_EVERY_S:
        seen = {**seen, "checked": time.time(), "latest": latest()}
        home.cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen), encoding="utf-8")
    v = str(seen.get("latest", ""))
    return v if v and newer(v, current()) else None


def announce_once(home: Home, version: str) -> bool:
    """True the first time this version is announced, so the agent tells you about each release once."""
    path = home.cache / "update.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    if seen.get("announced") == version:
        return False
    path.write_text(json.dumps({**seen, "announced": version}), encoding="utf-8")
    return True


def wheel(name: str, version: str) -> str:
    file = f"{name}-{version}-py3-none-any.whl"
    if (d := local_dir()) is not None:
        return str(d / file)
    return f"https://github.com/{REPO}/releases/download/v{version}/{file}"


def tool_dir() -> Path | None:
    """This daimon's folder if the installer made it (a uv tool), else None (a source checkout, someone's venv)."""
    uv = shutil.which("uv")
    if not uv:
        return None
    r = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True)
    folder = Path(r.stdout.strip()) / "daimon" if r.returncode == 0 and r.stdout.strip() else None
    return folder if folder and Path(sys.prefix).resolve() == folder.resolve() else None


def install_args(version: str) -> list[str]:
    """`uv` arguments that install `version`, keeping the add-ons this install has."""
    args = ["tool", "install", "--python", "3.12", "--force", wheel("daimon", version)]
    for m in ADDONS:
        if importlib.util.find_spec(m):
            args += ["--with", wheel(m, version)]
    return args


def running_jobs(home: Home) -> bool:
    return home.exists() and any(", running" in line for line in service.status(home))


def script(home: Home | None, version: str, wait_pid: int | None, restart: bool, folder: Path, log: Path) -> str:
    """The updater: wait for Daimon to exit, stop the background service, install, start it again."""
    uv, daimon = shutil.which("uv") or "uv", shutil.which("daimon") or "daimon"
    home_args = f'--home "{home.root}" ' if home and home.exists() else ""
    args = " ".join(f'"{a}"' for a in install_args(version))
    if os.name == "nt":
        lines = ["@echo off", f'set LOG="{log}"', f"echo [%date% %time%] updating to {version} >> %LOG%"]
        if wait_pid:
            lines += [":wait", f'tasklist /FI "PID eq {wait_pid}" 2>nul | find "{wait_pid}" >nul && '
                               "(ping -n 2 127.0.0.1 >nul & goto wait)"]  # no console here, so no `timeout`
        if restart:
            lines.append(f'"{daimon}" {home_args}service stop >> %LOG% 2>&1')
        lines += ["set n=0", ":install", f'"{uv}" {args} >> %LOG% 2>&1 && goto done', "set /a n+=1",
                  "if %n% geq 4 (echo update failed: run the install line again to get Daimon back >> %LOG% "
                  "& goto after)",
                  # uv sometimes can't delete the old install even with nothing running from it; cmd can
                  f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{folder}" >nul 2>&1', "goto install",
                  ":done", f"echo updated to {version} >> %LOG%", ":after"]
        if restart:
            lines.append(f'"{daimon}" {home_args}service start >> %LOG% 2>&1')
        return "\r\n".join(lines) + "\r\n"
    lines = ["#!/bin/sh", f'LOG="{log}"', f'echo "$(date) updating to {version}" >> "$LOG"']
    if wait_pid:
        lines.append(f"while kill -0 {wait_pid} 2>/dev/null; do sleep 1; done")
    if restart:
        lines.append(f'"{daimon}" {home_args}service stop >> "$LOG" 2>&1')
    lines.append(f'if "{uv}" {args} >> "$LOG" 2>&1; then echo "updated to {version}" >> "$LOG"; '
                 'else echo "update failed: run the install line again" >> "$LOG"; fi')
    if restart:
        lines.append(f'"{daimon}" {home_args}service start >> "$LOG" 2>&1')
    return "\n".join(lines) + "\n"


def launch(home: Home | None, version: str, wait_pid: int | None) -> Path:
    """Start the updater on its own, outside this process and the background service (so stopping them doesn't
    stop it). Returns its log."""
    folder = tool_dir()
    if folder is None:
        raise RuntimeError("this daimon wasn't installed by the installer, so update it the way you installed it "
                           "(from a source checkout: git pull)")
    restart = bool(home) and running_jobs(home)
    where = home.cache if home and home.exists() else Path.home() / ".daimon-update"
    where.mkdir(parents=True, exist_ok=True)
    log = where / "update.log"
    text = script(home, version, wait_pid, restart, folder, log)
    if os.name == "nt":
        cmd = where / "update.cmd"
        cmd.write_text(text, encoding="utf-8")
        vbs = where / "update.vbs"  # wscript starts the script hidden and exits, so nothing is its parent
        vbs.write_text(f'CreateObject("WScript.Shell").Run "cmd /c ""{cmd}""", 0, False\r\n', encoding="utf-8")
        subprocess.Popen(["wscript.exe", str(vbs)], close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        sh = where / "update.sh"
        sh.write_text(text, encoding="utf-8")
        # Under systemd, systemd-run puts it outside the service, which `service stop` would otherwise take down.
        argv = ["systemd-run", "--user", "--collect", "--quiet", "sh", str(sh)] \
            if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run") else ["sh", str(sh)]
        subprocess.Popen(argv, start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return log
