"""Connections for the web page: secrets, MCP servers, add-ons and Telegram, as `daimon config` sets them up.

Secrets go in and never come back out: the page only sees the last four characters. Add-ons sign in with their own
setup commands, run as jobs whose output the page shows (links to open, codes to type, an emoji to tap).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..home import Home

SECRET_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")
LEVELS = ("auto", "notify", "ask")


# ---- secrets -----------------------------------------------------------------------------------------
def secrets_view(home: Home) -> list[dict[str, Any]]:
    """Names, last four characters and what uses each. Never a value."""
    refs = {}
    sources = {"config.yaml": home.read("config.yaml"), "models.yaml": home.read("models.yaml")}
    for name, entry in home.mcp().get("mcpServers", {}).items():
        sources[name] = json.dumps(entry)
    for where, text in sources.items():
        text = re.sub(r"(?m)#.*$", "", text) if where.endswith(".yaml") else text  # not the examples in comments
        for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
            refs.setdefault(var, set()).add(where)
    from ..setup.providers import PROVIDERS
    for p in PROVIDERS.values():
        if p.env:
            refs.setdefault(p.env, set()).add("models")
    refs.setdefault("WEB_TOKEN", set()).add("web page")
    have = home.secrets()
    names = sorted(set(have) | {n for n in refs if n in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")} |
                   {n for n, w in refs.items() if w - {"models", "web page"}})
    return [{"name": n, "set": bool(have.get(n)), "last4": have.get(n, "")[-4:] if len(have.get(n, "")) > 8 else "",
             "used_by": sorted(refs.get(n, set()))} for n in names]


def set_secret(home: Home, name: str, value: str) -> None:
    if not SECRET_NAME.match(name or ""):
        raise ValueError("A secret's name is CAPITALS_AND_UNDERSCORES")
    value = (value or "").strip()
    if not value or "\n" in value:
        raise ValueError("Paste the value on one line")
    home.set_secret(name, value)


# ---- MCP servers -------------------------------------------------------------------------------------
def servers_view(home: Home, hub) -> list[dict[str, Any]]:  # noqa: ANN001 - an McpHub
    from ..setup.addons import ADDONS
    out = []
    for name, entry in home.mcp().get("mcpServers", {}).items():
        live = hub.servers.get(name)
        target = entry.get("url") or " ".join([Path(str(entry.get("command", ""))).name, *map(str, entry.get("args", []))])
        out.append({"name": name, "kind": "url" if entry.get("url") else "command", "target": target,
                    "disabled": bool(entry.get("disabled")), "addon": name in ADDONS,
                    "tools": len(live.tools) if live and not live.error else 0,
                    "asks": sum(1 for t in live.tools if hub.level(name, t) == "ask") if live and not live.error else 0,
                    "error": live.error if live else "",
                    "connected": bool(live and not live.error)})
    return out


def server_detail(home: Home, hub, name: str) -> dict[str, Any]:  # noqa: ANN001
    entry = home.mcp().get("mcpServers", {}).get(name)
    if entry is None:
        raise ValueError(f"no server named {name}")
    live = hub.servers.get(name)
    tools = [{"name": t.name, "about": (t.description or "").strip().split("\n")[0][:200],
              "read_only": bool(t.annotations and t.annotations.read_only_hint), "level": hub.level(name, t)}
             for t in (live.tools if live else [])]
    hide = lambda d: {k: v if "${" in str(v) else "••••" for k, v in (d or {}).items()}  # noqa: E731
    workers = []
    for p in sorted((home.root / "workers").glob("*.md")):
        from ..home import split_frontmatter
        mcps = split_frontmatter(p.read_text(encoding="utf-8"))[0].get("mcps", "all")
        if mcps == "all" or (isinstance(mcps, list) and name in mcps):
            workers.append(p.stem)
    return {"name": name, "url": entry.get("url"), "command": entry.get("command"), "args": entry.get("args", []),
            "headers": hide(entry.get("headers")), "env": hide(entry.get("env")), "disabled": bool(entry.get("disabled")),
            "consent": entry.get("consent", {}), "tools": tools, "error": live.error if live else "",
            "workers": workers, "defaults": {"read_only": "auto", "other": "ask", **home.mcp().get("defaults", {})}}


def add_server(home: Home, name: str, url: str = "", command: str = "", args: list[str] | None = None,
               headers: dict[str, str] | None = None, env: dict[str, str] | None = None) -> str | None:
    """A new server. Header and env values that aren't ${NAME} already go to the secrets file."""
    if not SERVER_NAME.match(name or ""):
        raise ValueError("Name it with letters, digits, - or _ (e.g. github)")
    data = home.mcp()
    if name in data.get("mcpServers", {}):
        raise ValueError(f"There's already a server called {name}")
    if not (url or command):
        raise ValueError("Give its address, or the command that runs it")

    def stash(values: dict[str, str] | None, suffix: str) -> dict[str, str]:
        out = {}
        for k, v in (values or {}).items():
            k, v = str(k).strip(), str(v).strip()
            if not k or not v:
                continue
            if "${" not in v:
                secret = re.sub(r"[^A-Z0-9]+", "_", f"{name}_{k if suffix == 'ENV' else suffix}".upper()).strip("_")
                home.set_secret(secret, v.removeprefix("Bearer ").strip() if v.startswith("Bearer ") else v)
                v = f"Bearer ${{{secret}}}" if v.startswith("Bearer ") else f"${{{secret}}}"
            out[k] = v
        return out

    entry: dict[str, Any] = {"url": url.strip(), "headers": stash(headers, "TOKEN")} if url \
        else {"command": command.strip(), "args": [a for a in (args or []) if a], "env": stash(env, "ENV")}
    data.setdefault("mcpServers", {})[name] = {k: v for k, v in entry.items() if v}
    home.write_mcp(data)
    return home.commit(f"user (web): added MCP server {name}", ["mcp.json"])


def change_server(home: Home, hub, name: str, action: str, b: dict[str, Any]) -> str | None:  # noqa: ANN001
    data = home.mcp()
    entry = data.get("mcpServers", {}).get(name)
    if entry is None:
        raise ValueError(f"no server named {name}")
    if action == "remove":
        del data["mcpServers"][name]
        msg = f"user (web): removed MCP server {name}"
    elif action == "toggle":
        if b.get("on"):
            entry.pop("disabled", None)
        else:
            entry["disabled"] = True
        msg = f"user (web): MCP server {name} {'on' if b.get('on') else 'off'}"
    elif action == "consent":
        level, tool = b.get("level"), str(b.get("tool") or "")
        if level not in LEVELS or not tool:
            raise ValueError("pick a tool and auto, notify or ask")
        hub.set_level(name, tool, level)
        return home.commit(f"user (web): {name}/{tool} → {level}", ["mcp.json"])
    else:
        raise ValueError(f"unknown action {action}")
    home.write_mcp(data)
    return home.commit(msg, ["mcp.json"])


def set_defaults(home: Home, read_only: str, other: str) -> str | None:
    if read_only not in LEVELS or other not in LEVELS:
        raise ValueError("auto, notify or ask")
    data = home.mcp()
    data["defaults"] = {"read_only": read_only, "other": other}
    home.write_mcp(data)
    return home.commit(f"user (web): new tools: read-only {read_only}, others {other}", ["mcp.json"])


# ---- add-ons: their own setup commands, run as jobs --------------------------------------------------
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class Job:
    id: str
    title: str
    lines: list[str] = field(default_factory=list)
    done: bool = False
    ok: bool = False
    started: float = field(default_factory=time.time)
    proc: subprocess.Popen | None = None

    def json(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "lines": self.lines[-200:], "done": self.done, "ok": self.ok}


class Jobs:
    """Setup commands running for the page. Their output is kept for the page to show as it comes."""

    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._n = 0

    def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise ValueError("That setup step isn't running any more")
        return self.jobs[job_id]

    def start(self, title: str, steps: list[tuple[list[str], str]], after=None, cleanup: list[Path] | None = None) -> Job:  # noqa: ANN001
        """Run each (argv, stdin) in turn, stopping at the first that fails; then after(job) and remove `cleanup`."""
        self._n += 1
        job = Job(f"j{self._n}", title)
        self.jobs[job.id] = job

        def run():
            try:
                for argv, stdin in steps:
                    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
                    kw: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    job.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                                errors="replace", env=env, **kw)
                    if stdin:
                        job.proc.stdin.write(stdin)
                    job.proc.stdin.close()
                    for line in job.proc.stdout:
                        line = ANSI.sub("", line.rstrip())
                        if line.strip():
                            job.lines.append(line)
                    if job.proc.wait() != 0:
                        return
                job.ok = True
                if after:
                    after(job)
            except Exception as e:  # noqa: BLE001 - shown on the page
                job.lines.append(f"{type(e).__name__}: {e}")
                job.ok = False
            finally:
                for p in cleanup or []:
                    p.unlink(missing_ok=True)
                job.done = True

        threading.Thread(target=run, daemon=True, name=f"setup-{job.id}").start()
        return job

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.proc and job.proc.poll() is None:
            job.proc.terminate()
            job.lines.append("Cancelled.")


def addons_view(home: Home, hub) -> list[dict[str, Any]]:  # noqa: ANN001
    from ..setup import addons
    servers = home.mcp().get("mcpServers", {})
    out = []
    for a in addons.ADDONS.values():
        live = hub.servers.get(a.key)
        out.append({"key": a.key, "title": a.title, "blurb": a.blurb, "added": a.key in servers,
                    "installed": a.key in servers or addons.installed(a),
                    "tools": len(live.tools) if live and not live.error else 0,
                    "error": live.error if live else ""})
    return out


def addon_status(key: str) -> str:
    """What the add-on says about itself (its accounts, link state)."""
    from ..setup.addons import ADDONS
    a = ADDONS.get(key)
    if a is None:
        raise ValueError(f"no add-on {key}")
    try:
        r = subprocess.run([sys.executable, "-m", a.module, "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=25, stdin=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"},
                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    except subprocess.TimeoutExpired:
        return "It didn't answer in time."
    return ANSI.sub("", (r.stdout + r.stderr).strip())


CALENDAR_KINDS = {"google", "microsoft", "apple", "fastmail", "yahoo", "caldav", "ics"}


def connect_addon(home: Home, jobs: Jobs, key: str, fields: dict[str, Any], done) -> Job:  # noqa: ANN001
    """Install the add-on if needed, add its server, and run its sign-in with what the page collected."""
    from ..setup import addons
    a = addons.ADDONS.get(key)
    if a is None:
        raise ValueError(f"no add-on {key}")
    py, steps, cleanup = sys.executable, [], []
    if not addons.installed(a):
        steps.append((addons.install_command(a), ""))
    mod = [py, "-m", a.module]
    secret = lambda name: str(fields.get(name) or "").strip()  # noqa: E731
    if key == "canvas":
        url = secret("url")
        if not url or not secret("token"):
            raise ValueError("Give your school's Canvas address and an access token")
        steps.append(([*mod, "login", "--url", url], secret("token") + "\n"))
    elif key == "calendar":
        kind = secret("kind")
        if kind not in CALENDAR_KINDS:
            raise ValueError("Pick which calendar")
        argv = [*mod, "add", kind]
        if secret("name"):
            argv += ["--name", secret("name")]
        stdin = ""
        if kind == "google":
            if not secret("client_id"):
                raise ValueError("Google needs your OAuth client id and secret")
            client = home.cache / f"google-client-{int(time.time())}.json"
            client.parent.mkdir(parents=True, exist_ok=True)
            client.write_text(json.dumps({"installed": {"client_id": secret("client_id"),
                                                       "client_secret": secret("client_secret")}}), encoding="utf-8")
            cleanup.append(client)
            argv += ["--client-file", str(client)]
        elif kind == "microsoft":
            if not secret("client_id"):
                raise ValueError("Microsoft needs your app registration's client id")
            argv += ["--client-id", secret("client_id"), "--tenant", secret("tenant") or "common"]
        elif kind == "ics":
            if not secret("url"):
                raise ValueError("Give the calendar's .ics or webcal address")
            argv += ["--url", secret("url")] + (["--title", secret("title")] if secret("title") else [])
        else:
            if not secret("username") or not secret("password"):
                raise ValueError("Give the username and password")
            if kind == "caldav" and not secret("url"):
                raise ValueError("Give the CalDAV server's address")
            argv += ["--username", secret("username")] + (["--url", secret("url")] if secret("url") else [])
            stdin = secret("password") + "\n"
        steps.append((argv, stdin))
    elif key == "messages":
        if not secret("cookies"):
            raise ValueError("Paste what you copied from messages.google.com")
        found = home.cache / f"messages-cookies-{int(time.time())}.txt"
        found.parent.mkdir(parents=True, exist_ok=True)
        found.write_text(str(fields["cookies"]), encoding="utf-8")
        cleanup.append(found)
        if not addons.messages_running():
            addons.start_messages(home.cache / "daimon-messages.log")
            steps.append(([py, "-c", WAIT_FOR_MESSAGES], ""))
        steps.append(([*mod, "pair", "--cookies", str(found)], ""))
    else:
        raise ValueError(f"no add-on {key}")

    def after(job: Job) -> None:
        if addons.add_server(home, a):
            job.lines.append(f'Added the "{a.key}" server.')
        done(job)

    return jobs.start(f"{a.title}", steps, after, cleanup)


WAIT_FOR_MESSAGES = ("import subprocess,sys,time\nprint('Starting the messaging service (the first start builds a "
                     "small helper)...',flush=True)\nend=time.time()+180\nwhile time.time()<end:\n"
                     " if subprocess.run([sys.executable,'-m','daimon_messages','status'],capture_output=True)"
                     ".returncode==0: sys.exit(0)\n time.sleep(2)\nprint('The messaging service did not start.')\n"
                     "sys.exit(1)")


def remove_calendar(name: str) -> str:
    r = subprocess.run([sys.executable, "-m", "daimon_calendar", "remove", name], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ValueError((r.stdout + r.stderr).strip() or f"couldn't remove {name}")
    return r.stdout.strip()


# ---- Telegram ----------------------------------------------------------------------------------------
def telegram_view(home: Home, running: bool) -> dict[str, Any]:
    cfg = (home.config().get("channels") or {}).get("telegram") or {}
    return {"token": bool(os.environ.get("TELEGRAM_TOKEN")), "chat": bool(os.environ.get("TELEGRAM_CHAT_ID")),
            "running": running, "events": cfg.get("events", ["notify", "error"]), "configured": bool(cfg)}


def telegram_bot(token: str) -> str:
    from ..setup.wizard import telegram
    return telegram(token, "getMe").get("username", "")


def telegram_token(home: Home, token: str) -> str:
    token = (token or "").strip()
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        raise ValueError("That doesn't look like a bot token (123456789:AA...)")
    try:
        bot = telegram_bot(token)
    except ValueError as e:
        raise ValueError(f"Telegram didn't take it: {e}") from None
    home.set_secret("TELEGRAM_TOKEN", token)
    if not (home.config().get("channels") or {}).get("telegram"):
        home.set_config(["channels", "telegram", "token"], "${TELEGRAM_TOKEN}")
        home.set_config(["channels", "telegram", "chat_id"], "${TELEGRAM_CHAT_ID}")
        home.commit("user (web): Telegram channel", ["config.yaml"])
    return bot


def telegram_wait(home: Home, seconds: float = 25) -> dict[str, Any] | None:
    from ..setup.wizard import telegram, wait_for_chat
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise ValueError("Add the bot's token first")
    found = wait_for_chat(token, seconds)
    if not found:
        return None
    chat_id, who = found
    home.set_secret("TELEGRAM_CHAT_ID", str(chat_id))
    try:
        telegram(token, "sendMessage", chat_id=chat_id, text="Daimon is connected. I'll talk to you here.")
    except ValueError:
        pass
    return {"chat": chat_id, "who": who}


def telegram_test() -> None:
    from ..setup.wizard import telegram
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise ValueError("Telegram isn't set up yet")
    telegram(token, "sendMessage", chat_id=chat, text="A test from the web page. This is where I reach you.")


def telegram_events(home: Home, events: list[str]) -> str | None:
    allowed = {"notify", "error", "edit", "task", "routine"}
    events = [e for e in events if e in allowed]
    home.set_config(["channels", "telegram", "events"], events)
    return home.commit(f"user (web): Telegram sends {', '.join(events) or 'no notices'}", ["config.yaml"])


def telegram_remove(home: Home) -> None:
    for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        home.remove_secret(k)
