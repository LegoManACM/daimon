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
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .gate import Tool
from .home import Home, expand_secrets

NOT_CONNECTION = {"consent", "disabled", "description"}


@dataclass
class Server:
    name: str
    stack: AsyncExitStack | None = None
    client: Client | None = None
    tools: list[Any] = field(default_factory=list)
    error: str = ""


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


class McpHub:
    def __init__(self, home: Home):
        self.home = home
        self.servers: dict[str, Server] = {}
        self._signature = None
        self._requests: asyncio.Queue | None = None
        self._owner: asyncio.Task | None = None

    # Connections live inside anyio cancel scopes, which must be closed by the task that opened them. Workers
    # run in their own tasks, so every connect/close goes through one owner task.
    async def _request(self, op: str) -> None:
        if self._owner is None:
            if op == "close":
                return
            self._requests = asyncio.Queue()
            self._owner = asyncio.create_task(self._own())
        done = asyncio.get_running_loop().create_future()
        await self._requests.put((op, done))
        await done

    async def _own(self) -> None:
        while True:
            op, done = await self._requests.get()
            try:
                await (self._sync() if op == "sync" else self._close())
                if not done.done():  # the requester may have been cancelled meanwhile (agent stopping mid-turn)
                    done.set_result(None)
            except BaseException as e:  # noqa: BLE001
                if not done.done():
                    done.set_exception(e)
            if op == "close":
                return

    async def sync(self) -> None:
        """Reconnect if the server list changed. Called before every model call, so the agent's own edits to
        mcp.json take effect immediately. Consent rules are read per call and don't trigger reconnects."""
        await self._request("sync")

    async def close(self) -> None:
        await self._request("close")
        self._owner = None

    async def _sync(self) -> None:
        entries = self.home.mcp().get("mcpServers", {})
        signature = json.dumps({n: {k: v for k, v in e.items() if k not in NOT_CONNECTION}
                                for n, e in entries.items() if not e.get("disabled")}, sort_keys=True)
        if signature == self._signature:
            return
        await self._close()
        self._signature = signature
        for name, entry in entries.items():
            if not entry.get("disabled"):
                self.servers[name] = await self._connect(name, entry)

    async def _connect(self, name: str, entry: dict[str, Any]) -> Server:
        server = Server(name)
        stack = AsyncExitStack()
        try:
            entry = expand_secrets(entry)
            if entry.get("url"):
                http = create_mcp_http_client(headers=entry.get("headers") or None)
                transport = streamable_http_client(entry["url"], http_client=http)
            elif entry.get("command"):
                self.home.cache.mkdir(exist_ok=True)
                errlog = stack.enter_context(open(self.home.cache / f"mcp-{name}.log", "a", encoding="utf-8"))
                params = StdioServerParameters(command=entry["command"], args=entry.get("args", []),
                                               env=entry.get("env") or None, cwd=entry.get("cwd"))
                transport = stdio_client(params, errlog=errlog)
            else:
                raise ValueError("needs either 'command' or 'url'")
            server.client = await stack.enter_async_context(Client(transport, read_timeout_seconds=120))
            server.tools = (await server.client.list_tools()).tools
            server.stack = stack
        except BaseException as e:  # noqa: BLE001 - one bad server must not take the agent down
            server.error = f"{type(e).__name__}: {e}"
            try:
                await stack.aclose()
            except BaseException:  # noqa: BLE001
                pass
        return server

    async def _close(self) -> None:
        self._signature = None
        for server in self.servers.values():
            if server.stack:
                try:
                    await server.stack.aclose()
                except BaseException:  # noqa: BLE001
                    pass
        self.servers.clear()

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
