"""Optional add-ons (Canvas, text messages): install into Daimon's own Python, register in mcp.json, set up."""
from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import daimon

from ..home import Home

REPO = "LegoManACM/daimon"
SOURCE = Path(daimon.__file__).resolve().parent.parent  # the repo root, when running from a checkout


@dataclass
class Addon:
    key: str          # also the server name in mcp.json
    title: str
    module: str       # python -m <module> ...
    folder: str       # its folder in the repo
    blurb: str
    consent: dict[str, str] = field(default_factory=dict)

    @property
    def dist(self) -> str:
        return self.module.replace("_", "-")


ADDONS = {
    "canvas": Addon("canvas", "Canvas", "daimon_canvas", "canvas",
                    "your classes, homework, grades and course files from Canvas LMS"),
    "messages": Addon("messages", "Text messages", "daimon_messages", "messaging",
                      "your SMS/RCS texts through Google Messages", {"send_message": "ask"}),
}


def version() -> str:
    try:
        return importlib.metadata.version("daimon")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def source(addon: Addon) -> list[str]:
    """pip arguments for the add-on: editable from a source checkout, else the matching release wheel."""
    local = SOURCE / addon.folder
    if (local / "pyproject.toml").exists():
        return ["-e", str(local)]
    v = version()
    return [f"https://github.com/{REPO}/releases/download/v{v}/{addon.module}-{v}-py3-none-any.whl"]


def install_command(addon: Addon, python: str = sys.executable) -> list[str]:
    if uv := shutil.which("uv"):
        return [uv, "pip", "install", "--python", python, *source(addon)]
    return [python, "-m", "pip", "install", *source(addon)]


def installed(addon: Addon, python: str = sys.executable) -> bool:
    return subprocess.run([python, "-c", f"import {addon.module}"], capture_output=True).returncode == 0


def install(addon: Addon, python: str = sys.executable) -> str:
    """Install it (output shown as it goes). Returns '' or what went wrong."""
    try:
        r = subprocess.run(install_command(addon, python))
    except OSError as e:
        return str(e)
    return "" if r.returncode == 0 else f"the install command failed (exit code {r.returncode})"


def server_entry(addon: Addon, python: str = sys.executable) -> dict:
    entry = {"command": python, "args": ["-m", addon.module, "mcp"]}
    return entry | ({"consent": dict(addon.consent)} if addon.consent else {})


def add_server(home: Home, addon: Addon, python: str = sys.executable) -> bool:
    """Add the add-on's MCP server to mcp.json. An existing entry with that name is left alone (returns False)."""
    data = home.mcp()
    servers = data.setdefault("mcpServers", {})
    if addon.key in servers:
        return False
    servers[addon.key] = server_entry(addon, python)
    home.write_mcp(data)
    home.commit(f"setup: add the {addon.key} MCP server", ["mcp.json"])
    return True


def run_interactive(argv: list[str]) -> int:
    """Run an add-on's own setup command in this terminal."""
    try:
        return subprocess.run(argv).returncode
    except (OSError, KeyboardInterrupt):
        return 1


def messages_running(python: str = sys.executable) -> bool:
    r = subprocess.run([python, "-m", "daimon_messages", "status"], capture_output=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def start_messages(log: Path, python: str = sys.executable) -> subprocess.Popen:
    """Start `daimon-messages run` detached from this terminal, logging to `log`. It keeps running after setup."""
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "a", encoding="utf-8")
    kw: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} \
        if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([python, "-m", "daimon_messages", "run"], stdout=out, stderr=out,
                            stdin=subprocess.DEVNULL, **kw)
