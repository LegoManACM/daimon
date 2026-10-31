"""Connections to the MCP servers listed in mcp.json, exposed as gate Tools.

mcp.json uses the standard `mcpServers` block. Extra keys per server:
  "consent":  {"<tool name or glob>": "auto" | "notify" | "ask"}   first match wins
  "disabled": true
Top-level "defaults": {"read_only": "auto", "other": "ask"} sets levels for tools with no rule, using the
server's readOnlyHint annotation.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .gate import Tool
from .home import Home, expand_secrets

NOT_CONNECTION = {"consent", "disabled", "description"}


# What a command-line server inherits besides the mcp library's minimal set (PATH, USERPROFILE...): where
# programs are installed and how they find their config. Without these, e.g. `docker mcp` can't find its plugin
# on Windows. The rest of the environment holds the secrets file's keys, which servers only get by name, in
# their entry's "env" (as "${NAME}").
SYSTEM_ENV = ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA", "COMMONPROGRAMFILES",
              "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "ALLUSERSPROFILE", "PUBLIC", "WINDIR", "COMSPEC",
              "TMP", "TMPDIR", "OS", "COMPUTERNAME", "NUMBER_OF_PROCESSORS", "HOME", "SHELL", "LANG", "LC_ALL",
              "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
              "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
LOG_TAIL = 600   # chars of a failed server's own output shown with its error
CONNECT_S = 120  # a server that hasn't answered by then counts as failed (per server: "connect_timeout_s")
CLOSE_S = 10


@dataclass
class Server:
    name: str
    client: Client | None = None
    tools: list[Any] = field(default_factory=list)
    error: str = ""
    instructions: str = ""  # what the server says about itself; an "Events:" part lists the events it sends
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def overview(self) -> str:
        """The instructions without the events list (that part is read on demand, with the guide tool)."""
        return EVENTS.split(self.instructions, 1)[0].strip()

    def events(self) -> str:
        parts = EVENTS.split(self.instructions, 1)
        return parts[1].strip() if len(parts) > 1 else ""


EVENTS = re.compile(r"^Events:[ \t]*$", re.M)  # add-ons end their instructions with this line and the list


def _since(log: Path, offset: int) -> str:
    """What a command-line server wrote to its log since it was started: usually why it didn't."""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            text = f.read().strip()
    except OSError:
        return ""
    return ("…" + text[-LOG_TAIL:]) if len(text) > LOG_TAIL else text


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


class McpHub:
    def __init__(self, home: Home):
        self.home = home
        self.servers: dict[str, Server] = {}
        self._signature = None
        self._lock = asyncio.Lock()

    async def sync(self) -> None:
        """Reconnect if the server list changed. Called before every model call, so the agent's own edits to
        mcp.json take effect immediately. Consent rules are read per call and don't trigger reconnects."""
        async with self._lock:
            entries = self.home.mcp().get("mcpServers", {})
            signature = json.dumps({n: {k: v for k, v in e.items() if k not in NOT_CONNECTION}
                                    for n, e in entries.items() if not e.get("disabled")}, sort_keys=True)
            if signature == self._signature:
                return
            await self._close()
            self._signature = signature
            names = [n for n, e in entries.items() if not e.get("disabled")]
            servers = await asyncio.gather(*(self._connect(n, entries[n]) for n in names))
            self.servers = dict(zip(names, servers))

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    # A connection lives inside anyio cancel scopes, which must be closed by the task that opened them, in
    # order. So each server gets a task of its own that opens the connection, holds it until told to stop and
    # closes it: one server failing or closing can't cancel another, or whoever asked for the sync.
    async def _connect(self, name: str, entry: dict[str, Any]) -> Server:
        server = Server(name)
        ready = asyncio.get_running_loop().create_future()
        server.task = asyncio.create_task(self._hold(server, entry, ready), name=f"mcp-{name}")
        try:
            await asyncio.wait_for(asyncio.shield(ready), float(entry.get("connect_timeout_s") or CONNECT_S))
        except TimeoutError:
            server.error = f"didn't answer within {entry.get('connect_timeout_s') or CONNECT_S}s"
            server.task.cancel()
        return server

    async def _hold(self, server: Server, entry: dict[str, Any], ready: asyncio.Future) -> None:
        log, logged_from = self.home.cache / f"mcp-{server.name}.log", 0
        try:
            async with AsyncExitStack() as stack:
                entry = expand_secrets(entry)
                if entry.get("url"):
                    http = create_mcp_http_client(headers=entry.get("headers") or None)
                    transport = streamable_http_client(entry["url"], http_client=http)
                elif entry.get("command"):
                    self.home.cache.mkdir(exist_ok=True)
                    logged_from = log.stat().st_size if log.exists() else 0
                    errlog = stack.enter_context(open(log, "a", encoding="utf-8"))
                    env = {**get_default_environment(), **{k: os.environ[k] for k in SYSTEM_ENV if k in os.environ},
                           **(entry.get("env") or {})}
                    params = StdioServerParameters(command=entry["command"], args=entry.get("args", []),
                                                   env=env, cwd=entry.get("cwd"))
                    transport = stdio_client(params, errlog=errlog)
                else:
                    raise ValueError("needs either 'command' or 'url'")
                limit = entry.get("timeout_s") or (self.home.config().get("tools") or {}).get("mcp_timeout_s", 900)
                client = await stack.enter_async_context(Client(transport, read_timeout_seconds=float(limit)))
                server.tools = (await client.list_tools()).tools
                server.instructions = (client.instructions or "").strip()
                server.client = client
                ready.set_result(None)
                await server.stop.wait()
        except BaseException as e:  # noqa: BLE001 - one bad server must not take the agent down
            if not server.stop.is_set():
                server.error = f"{type(e).__name__}: {e}" if not ready.done() else f"disconnected: {type(e).__name__}: {e}"
                said = _since(log, logged_from)
                if said:
                    server.error += f"\n  It said: {said}"
        finally:
            if not ready.done():
                ready.set_result(None)

    async def _close(self) -> None:
        self._signature = None
        servers, self.servers = list(self.servers.values()), {}
        for server in servers:
            server.stop.set()
        tasks = [s.task for s in servers if s.task]
        if tasks:
            _, slow = await asyncio.wait(tasks, timeout=CLOSE_S)
            for t in slow:
                t.cancel()
            await asyncio.gather(*slow, return_exceptions=True)

    # ---- consent -----------------------------------------------------------------------------------
    def level(self, server: str, tool: Any) -> str:
        data = self.home.mcp()
        rules = data.get("mcpServers", {}).get(server, {}).get("consent", {})
        if tool.name in rules:
            return rules[tool.name]
        for pattern, level in rules.items():
            if fnmatch.fnmatchcase(tool.name, pattern):
                return level
        defaults = {"read_only": "auto", "other": "ask", **data.get("defaults", {})}
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        return defaults["read_only" if read_only else "other"]

    def set_level(self, server: str, pattern: str, level: str) -> None:
        data = self.home.mcp()
        entry = data.get("mcpServers", {}).get(server)
        if entry is None:
            raise ValueError(f"no MCP server named {server}")
        rules = entry.setdefault("consent", {})
        rules.pop(pattern, None)
        # Specific rules go first so they win over globs.
        entry["consent"] = {pattern: level, **rules} if "*" not in pattern else {**rules, pattern: level}
        self.home.write_mcp(data)

    # ---- tools -------------------------------------------------------------------------------------
    def tools(self, allowed: Any = "all") -> list[Tool]:
        out = []
        for server in self.servers.values():
            if allowed != "all" and server.name not in (allowed or []):
                continue
            for t in server.tools:
                out.append(self._wrap(server, t))
        return out

    def _wrap(self, server: Server, t: Any) -> Tool:
        async def run(args: dict[str, Any]) -> str:
            result = await server.client.call_tool(t.name, args)
            parts = []
            for c in result.content:
                if getattr(c, "type", "") == "text":
                    parts.append(c.text)
                else:
                    parts.append(f"[{getattr(c, 'type', 'content')} omitted]")
            if not parts and result.structured_content is not None:
                parts.append(json.dumps(result.structured_content, ensure_ascii=False))
            text = "\n".join(parts) or "(no output)"
            return f"Error: {text}" if result.is_error else text

        async def always() -> None:
            self.set_level(server.name, t.name, "auto")
            self.home.commit(f"user: always allow {server.name}/{t.name}", ["mcp.json"])

        return Tool(name=tool_name(server.name, t.name), description=t.description or "",
                    schema=t.input_schema or {}, run=run, level=lambda _args: self.level(server.name, t),
                    source=server.name, allow_always=always)

    def status(self) -> list[str]:
        lines = []
        for s in self.servers.values():
            lines.append(f"{s.name}: error: {s.error}" if s.error else f"{s.name}: connected, {len(s.tools)} tools")
        return lines
