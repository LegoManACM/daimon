"""Keep Daimon running in the background, started at login.

Windows: a Task Scheduler logon task (no admin needed), or the Startup folder if that's refused. It runs a hidden
.cmd launcher that restarts the process after a crash. macOS: a LaunchAgent. Linux: a systemd user unit.
Output goes to <home>/.cache/<job>.log on every platform.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..home import Home
from . import addons

PASSED_ENV = ("DAIMON_SECRETS", "DAIMON_CANVAS_HOME", "DAIMON_MESSAGES_HOME")


@dataclass
class Job:
    key: str            # daimon | daimon-messages | daimon-canvas
    title: str          # the Windows task name
    label: str          # the macOS LaunchAgent label
    argv: list[str]
    log: Path

    @property
    def unit(self) -> str:
        return f"{self.key}.service"


def addon_job(home: Home, key: str, python: str) -> Job:
    a = addons.ADDONS[key]
    return Job(f"daimon-{key}", f"Daimon {key.title()}", f"com.daimon.{key}", [python, "-m", a.module, *a.background],
               home.cache / f"daimon-{key}.log")


def jobs(home: Home, messages: bool = True, agent: bool = True, python: str = sys.executable,
         canvas: bool = True) -> list[Job]:
    out = []
    if agent:
        out.append(Job("daimon", "Daimon", "com.daimon.agent",
                       [python, "-m", "daimon", "--home", str(home.root), "run"], home.cache / "daimon.log"))
    out += [addon_job(home, key, python) for key, wanted in (("messages", messages), ("canvas", canvas)) if wanted]
    return out


def env() -> dict[str, str]:
    return {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}


def sh(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a service-manager command quietly. Tests replace this."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def platform() -> str:
    return "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"


def err(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout or f"exit code {r.returncode}").strip().splitlines()[-1]


# ---- Windows ---------------------------------------------------------------------------------------------
def launcher_dir(home: Home) -> Path:
    return home.cache / "service"


def startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs/Startup"


def windows_cmd(job: Job, variables: dict[str, str]) -> str:
    def q(s: str) -> str:
        return '"' + s.replace("%", "%%") + '"'
    lines = ["@echo off", "rem Daimon's background launcher (`daimon service`). Restarts the process if it fails."]
    lines += [f'set "{k}={v.replace("%", "%%")}"' for k, v in variables.items()]
    lines += [":loop", f"echo [%date% %time%] starting >> {q(str(job.log))}",
              " ".join(q(a) for a in job.argv) + f" >> {q(str(job.log))} 2>&1",
              "if errorlevel 1 (", "  ping -n 31 127.0.0.1 >nul", "  goto loop", ")"]
    return "\r\n".join(lines) + "\r\n"


def windows_vbs(cmd: Path) -> str:
    return f'CreateObject("WScript.Shell").Run "cmd /c ""{cmd}""", 0, True\r\n'


def windows_task_xml(job: Job, vbs: Path, user: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(job.title)}, started at logon (daimon service)</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT20S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>wscript.exe</Command><Arguments>"{escape(str(vbs))}"</Arguments></Exec>
  </Actions>
</Task>
"""


def _win_paths(home: Home, job: Job) -> tuple[Path, Path, Path]:
    d = launcher_dir(home)
    return d / f"{job.key}.cmd", d / f"{job.key}.vbs", startup_dir() / f"{job.title}.vbs"


def _win_pids(cmd: Path) -> list[int]:
    path = str(cmd).replace("'", "''")
    r = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -and "
            f"$_.CommandLine.Contains('{path}') }} | ForEach-Object {{ $_.ProcessId }}"])
    return [int(x) for x in r.stdout.split() if x.isdigit()] if r.returncode == 0 else []


def _win_install(home: Home, job: Job) -> str:
    cmd, vbs, startup = _win_paths(home, job)
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text(windows_cmd(job, env()), encoding="utf-8")
    vbs.write_text(windows_vbs(cmd), encoding="utf-8")
    xml = cmd.with_suffix(".xml")
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    xml.write_text(windows_task_xml(job, vbs, user), encoding="utf-16")
    r = sh(["schtasks", "/Create", "/TN", job.title, "/XML", str(xml), "/F"])
    if r.returncode == 0:
        startup.unlink(missing_ok=True)
        return f"Task Scheduler task \"{job.title}\" (at logon)"
    startup.parent.mkdir(parents=True, exist_ok=True)
    startup.write_text(windows_vbs(cmd), encoding="utf-8")
    return f"{startup} (Task Scheduler said: {err(r)})"


def _win_task(job: Job) -> bool:
    return sh(["schtasks", "/Query", "/TN", job.title]).returncode == 0


def _win_start(home: Home, job: Job) -> str:
    cmd, vbs, _ = _win_paths(home, job)
    if _win_pids(cmd):
        return ""
    if _win_task(job) and sh(["schtasks", "/Run", "/TN", job.title]).returncode == 0:
        return ""
    if not vbs.exists():
        return "not installed"
    try:
        subprocess.Popen(["wscript.exe", str(vbs)], stdin=subprocess.DEVNULL)
    except OSError as e:
        return str(e)
    return ""


def _win_stop(home: Home, job: Job) -> str:
    cmd, _, _ = _win_paths(home, job)
    for pid in _win_pids(cmd):
        sh(["taskkill", "/PID", str(pid), "/T", "/F"])
    return ""


def _win_restart(home: Home, job: Job) -> str:
    return _win_stop(home, job) or _win_start(home, job)


def _win_uninstall(home: Home, job: Job) -> None:
    _win_stop(home, job)
    if _win_task(job):
        sh(["schtasks", "/Delete", "/TN", job.title, "/F"])
    cmd, vbs, startup = _win_paths(home, job)
    for p in (startup, cmd, vbs, cmd.with_suffix(".xml")):
        p.unlink(missing_ok=True)


def _win_status(home: Home, job: Job) -> tuple[bool, bool]:
    cmd, _, startup = _win_paths(home, job)
    return _win_task(job) or startup.exists(), bool(_win_pids(cmd))


# ---- macOS -----------------------------------------------------------------------------------------------
def plist_path(job: Job) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{job.label}.plist"


def plist(job: Job, variables: dict[str, str], home: Home) -> str:
    return plistlib.dumps({
        "Label": job.label,
        "ProgramArguments": job.argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "WorkingDirectory": str(home.root),
        "StandardOutPath": str(job.log),
        "StandardErrorPath": str(job.log),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **variables},
    }).decode()


def _gui(job: Job | None = None) -> str:
    return f"gui/{os.getuid()}" + (f"/{job.label}" if job else "")


def _mac_install(home: Home, job: Job) -> str:
    p = plist_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(plist(job, env(), home), encoding="utf-8")
    if sh(["launchctl", "print", _gui(job)]).returncode == 0:  # reload an older version that's running
        sh(["launchctl", "bootout", _gui(job)])
        sh(["launchctl", "bootstrap", _gui(), str(p)])
    return str(p)


def _mac_start(home: Home, job: Job) -> str:
    if sh(["launchctl", "print", _gui(job)]).returncode == 0:
        r = sh(["launchctl", "kickstart", _gui(job)])
    else:
        r = sh(["launchctl", "bootstrap", _gui(), str(plist_path(job))])
    return "" if r.returncode == 0 else err(r)


def _mac_stop(home: Home, job: Job) -> str:
    r = sh(["launchctl", "bootout", _gui(job)])  # it loads again at the next login
    return "" if r.returncode == 0 or "No such process" in (r.stderr or "") else err(r)


def _mac_restart(home: Home, job: Job) -> str:
    if sh(["launchctl", "print", _gui(job)]).returncode != 0:
        return _mac_start(home, job)
    r = sh(["launchctl", "kickstart", "-k", _gui(job)])  # -k: kill the running one first
    return "" if r.returncode == 0 else err(r)


def _mac_uninstall(home: Home, job: Job) -> None:
    sh(["launchctl", "bootout", _gui(job)])
    plist_path(job).unlink(missing_ok=True)


def _mac_status(home: Home, job: Job) -> tuple[bool, bool]:
    r = sh(["launchctl", "print", _gui(job)])
    return plist_path(job).exists(), r.returncode == 0 and "state = running" in r.stdout


# ---- Linux -----------------------------------------------------------------------------------------------
def unit_path(job: Job) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd/user" / job.unit


def _unit_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit(job: Job, variables: dict[str, str], home: Home) -> str:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **variables}
    lines = ["[Unit]", f"Description={job.title} (daimon service)", "After=network-online.target", "",
             "[Service]", "ExecStart=" + " ".join(_unit_quote(a) for a in job.argv),
             f"WorkingDirectory={_unit_quote(str(home.root))}"]
    lines += [f"Environment={_unit_quote(f'{k}={v}')}" for k, v in environment.items()]
    lines += ["Restart=on-failure", "RestartSec=30", f"StandardOutput=append:{job.log}",
              f"StandardError=append:{job.log}", "", "[Install]", "WantedBy=default.target"]
    return "\n".join(lines) + "\n"


def _linux_install(home: Home, job: Job) -> str:
    p = unit_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(unit(job, env(), home), encoding="utf-8")
    sh(["systemctl", "--user", "daemon-reload"])
    r = sh(["systemctl", "--user", "enable", job.unit])
    sh(["systemctl", "--user", "try-restart", job.unit])  # picks up the new unit if an older one is running
    return str(p) if r.returncode == 0 else f"{p} (enabling it failed: {err(r)})"


def _linux_start(home: Home, job: Job) -> str:
    r = sh(["systemctl", "--user", "start", job.unit])
    return "" if r.returncode == 0 else err(r)


def _linux_stop(home: Home, job: Job) -> str:
    r = sh(["systemctl", "--user", "stop", job.unit])
    return "" if r.returncode == 0 else err(r)


def _linux_restart(home: Home, job: Job) -> str:
    r = sh(["systemctl", "--user", "restart", job.unit])
    return "" if r.returncode == 0 else err(r)


def _linux_uninstall(home: Home, job: Job) -> None:
    sh(["systemctl", "--user", "disable", "--now", job.unit])
    unit_path(job).unlink(missing_ok=True)
    sh(["systemctl", "--user", "daemon-reload"])


def _linux_status(home: Home, job: Job) -> tuple[bool, bool]:
    return unit_path(job).exists(), sh(["systemctl", "--user", "is-active", job.unit]).stdout.strip() == "active"


# ---- the public surface ----------------------------------------------------------------------------------
OPS = {
    "windows": (_win_install, _win_start, _win_stop, _win_uninstall, _win_status),
    "macos": (_mac_install, _mac_start, _mac_stop, _mac_uninstall, _mac_status),
    "linux": (_linux_install, _linux_start, _linux_stop, _linux_uninstall, _linux_status),
}
RESTART = {"windows": _win_restart, "macos": _mac_restart, "linux": _linux_restart}


def install(home: Home, messages: bool, start_now: bool = False, agent: bool = True, canvas: bool = False) -> list[str]:
    """Install auto-start for each job (replacing an older install, removing jobs left out). Returns a line per job."""
    do_install, do_start, *_ = OPS[platform()]
    home.cache.mkdir(parents=True, exist_ok=True)
    out = []
    for key, wanted in (("daimon", agent), ("daimon-messages", messages), ("daimon-canvas", canvas)):
        if not wanted:
            uninstall(home, only=key)
    for job in jobs(home, messages, agent, canvas=canvas):
        where = do_install(home, job)
        line = f"{job.key}: {where}"
        if start_now:
            problem = do_start(home, job)
            line += f"; not started: {problem}" if problem else "; started"
        out.append(line)
    return out


def uninstall(home: Home, only: str | None = None) -> list[str]:
    *_, do_uninstall, do_status = OPS[platform()]
    out = []
    for job in jobs(home):
        if only in (None, job.key) and any(do_status(home, job)):
            do_uninstall(home, job)
            out.append(f"{job.key}: removed")
    return out


def installed(home: Home) -> list[Job]:
    do_status = OPS[platform()][4]
    return [job for job in jobs(home) if do_status(home, job)[0]]


def start(home: Home) -> list[str]:
    do_start = OPS[platform()][1]
    return [f"{job.key}: " + (f"not started: {p}" if (p := do_start(home, job)) else "started")
            for job in installed(home)]


def stop(home: Home) -> list[str]:
    do_stop, do_status = OPS[platform()][2], OPS[platform()][4]
    return [f"{job.key}: " + (f"couldn't stop: {p}" if (p := do_stop(home, job)) else "stopped")
            for job in jobs(home) if any(do_status(home, job))]


def restart(home: Home) -> list[str]:
    """Stop and start each installed job, so it picks up config changes."""
    do_restart = RESTART[platform()]
    return [f"{job.key}: " + (f"couldn't restart: {p}" if (p := do_restart(home, job)) else "restarted")
            for job in installed(home)]


def running(home: Home, key: str = "daimon") -> bool:
    do_status = OPS[platform()][4]
    return any(job.key == key and do_status(home, job)[1] for job in jobs(home))


def detach(where: Path, name: str, text: str) -> None:
    """Run a script (.cmd on Windows, sh elsewhere) on its own, outside this process and the background service,
    so stopping them doesn't stop it."""
    where.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        cmd = where / f"{name}.cmd"
        cmd.write_text(text, encoding="utf-8")
        vbs = where / f"{name}.vbs"  # wscript starts the script hidden and exits, so nothing is its parent
        vbs.write_text(f'CreateObject("WScript.Shell").Run "cmd /c ""{cmd}""", 0, False\r\n', encoding="utf-8")
        subprocess.Popen(["wscript.exe", str(vbs)], close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    script = where / f"{name}.sh"
    script.write_text(text, encoding="utf-8")
    # Under systemd, systemd-run puts it outside the service, which `service stop` would otherwise take down.
    argv = ["systemd-run", "--user", "--collect", "--quiet", "sh", str(script)] \
        if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run") else ["sh", str(script)]
    subprocess.Popen(argv, start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart_later(home: Home, after_s: int = 5) -> Path:
    """`daimon service restart` from outside the service, after `after_s` seconds (for the agent restarting itself:
    its last reply gets out first). Returns the log."""
    log = home.cache / "restart.log"
    argv = " ".join(f'"{a}"' for a in (sys.executable, "-m", "daimon", "--home", str(home.root), "service", "restart"))
    if os.name == "nt":  # no console here, so no `timeout`
        text = f"@echo off\r\nping -n {after_s + 1} 127.0.0.1 >nul\r\n{argv} >> \"{log}\" 2>&1\r\n"
    else:
        text = f"#!/bin/sh\nsleep {after_s}\n{argv} >> \"{log}\" 2>&1\n"
    detach(home.cache, "restart", text)
    return log


def status(home: Home) -> list[str]:
    do_status = OPS[platform()][4]
    out = []
    for job in jobs(home):
        on, running = do_status(home, job)
        if on or running:
            out.append(f"{job.key}: {'starts at login' if on else 'not installed'}, "
                       f"{'running' if running else 'not running'} (log: {job.log})")
    return out
