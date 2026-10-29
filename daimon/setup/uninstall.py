"""`daimon uninstall`: stop it, remove start-at-login, delete its folders and secrets, then the program itself."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from ..home import Home, resolve_home
from . import service

RUNNING = ["daimon_messages run", "-m daimon --home"]  # what start-at-login and setup leave running


def addon_homes() -> list[Path]:
    return [Path(os.environ.get(var) or default).expanduser()
            for var, default in (("DAIMON_CANVAS_HOME", "~/.daimon-canvas"),
                                 ("DAIMON_MESSAGES_HOME", "~/.daimon-messages"),
                                 ("DAIMON_CALENDAR_HOME", "~/.daimon-calendar"))]


def tool_install() -> bool:
    """Whether this daimon is the uv tool the installer made (not a source checkout or someone's venv)."""
    from .update import tool_dir
    return tool_dir() is not None


def plan(home: Home) -> tuple[list[Path], list[str]]:
    """The paths to delete, and notes on what's kept."""
    paths = [p for p in (home.root, home.secrets_file, *addon_homes()) if p.exists()]
    kept = []
    if home.exists() and home.knowledge.exists():
        try:
            home.knowledge.resolve().relative_to(home.root.resolve())
        except ValueError:
            kept.append(f"your knowledge folder, {home.knowledge} (it lives outside the agent's folder)")
    return paths, kept


def describe(home: Home, paths: list[Path], kept: list[str], program: bool) -> str:
    lines = ["This removes Daimon from this computer:"]
    if home.exists() and service.installed(home):
        lines.append("  - starting at login (and stops what's running)")
    for p in paths:
        extra = ""
        if p == home.root and home.exists():
            notes = [n for n in home.knowledge.rglob("*.md") if n.name not in ("index.md", "log.md")] \
                if home.knowledge.exists() else []
            if not kept and notes:
                extra = f", including {len(notes)} knowledge notes"
            extra = f" (the agent: its memory, workers, history{extra})"
        elif p == home.secrets_file:
            extra = " (your API keys and tokens)"
        lines.append(f"  - {p}{extra}")
    lines.append("  - the daimon program" if program else
                 "  - (not the program: this daimon wasn't installed by the installer, so remove it the way you "
                 "installed it)")
    lines += [f"Kept: {k}" for k in kept]
    return "\n".join(lines)


def stop_running() -> None:
    if os.name == "nt":
        query = " -or ".join(f"$_.CommandLine -like '*{pattern}*'" for pattern in RUNNING)
        ps = (f"Get-CimInstance Win32_Process | Where-Object {{ ({query}) -and $_.ProcessId -ne {os.getpid()} -and $_.Name -notlike 'powershell*' }} | "
              "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    else:
        for pattern in RUNNING:
            subprocess.run(["pkill", "-f", pattern], capture_output=True)


def delete(path: Path) -> str:
    """Delete a file or folder. Returns '' or what went wrong."""
    def writable(func, p, _exc):  # git makes its object files read-only, which Windows won't delete
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        if path.is_dir():
            shutil.rmtree(path, onexc=writable) if sys.version_info >= (3, 12) else shutil.rmtree(path, onerror=writable)
        else:
            path.unlink(missing_ok=True)
    except OSError as e:
        return str(e)
    return ""


def remove_program() -> str:
    """Uninstall the uv tool once this process has exited (Windows won't delete a running program)."""
    uv = shutil.which("uv")
    if os.name == "nt":
        # ping is the delay: `timeout` needs a console, which a detached cmd doesn't have
        script = f'for /l %i in (1,1,10) do (ping -n 3 127.0.0.1 >nul & "{uv}" tool uninstall daimon >nul 2>&1 && exit)'
        subprocess.Popen(["cmd", "/c", script], close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS |
                         subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        return "The daimon program is removed in a few seconds, once this command has finished."
    r = subprocess.run([uv, "tool", "uninstall", "daimon"], capture_output=True, text=True)
    return "Removed the daimon program." if r.returncode == 0 else \
        f"Couldn't remove the program ({r.stderr.strip()}); run `uv tool uninstall daimon`."


def run(home_flag: str | None = None, yes: bool = False, ask=input) -> int:
    home = Home(resolve_home(home_flag))
    paths, kept = plan(home)
    program = tool_install()
    print(describe(home, paths, kept, program))
    if not yes:
        try:
            answer = ask("\nAre you sure? (y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing was removed.")
            return 1
    if home.exists():
        for line in service.uninstall(home):
            print(line)
    stop_running()
    failed = [f"{p}: {problem}" for p in paths if (problem := delete(p))]
    for p in paths:
        if not p.exists():
            print(f"Deleted {p}")
    if program:
        print(remove_program())
    if addon_homes()[1] in paths:
        print("If you paired text messages, also remove this computer in Google Messages > Device pairing.")
    if failed:
        print("Couldn't delete (close anything using them and run `daimon uninstall` again, or delete them by "
              "hand):\n  " + "\n  ".join(failed))
        return 1
    print("Daimon is uninstalled.")
    return 0
