"""The web channel: a page to chat with the agent and watch it work, served by the running agent itself.

It's a channel like Telegram (gateway.py): your messages go to the main agent, approvals show up as cards and
whichever channel answers first wins. On top of that the page gets everything the terminal's status bar sees:
live progress of every model call and tool, background jobs, the activity log, tasks, spend and the agent's own
change history. Updates reach the page over one server-sent-events stream; everything else is a small JSON API.

Hosting is up to config.yaml's `web` section (see access.py): where it listens and who gets in.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..gateway import OnMessage, Prompt
from . import access, data

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8770
PROGRESS_EVERY_S = 0.12  # live progress is coalesced to this rate per browser
EVENTS_KEPT = 500
QUEUE = 2000             # updates waiting for a slow browser before it's dropped (it reconnects and catches up)


@dataclass
class Client:
    id: str
    visitor: access.Visitor
    ip: str
    queue: asyncio.Queue
    since: float = field(default_factory=time.time)


@dataclass
class Pending:
    id: str
    prompt: Prompt
    answer: asyncio.Future
    created: float = field(default_factory=time.time)

    def json(self) -> dict[str, Any]:
        p = self.prompt
        return {"id": self.id, "title": p.title, "details": p.details, "options": p.options, "who": p.who,
                "free_text": p.free_text, "created": self.created}


class WebChannel:
    name = "web"

    def __init__(self, home, cfg: dict[str, Any], log: Callable[[str], None] = print):  # noqa: ANN001 - a Home
        self.home, self.cfg, self.log = home, cfg, log
        self.listen = str(cfg.get("listen") or "localhost")
        self.port = int(cfg.get("port") or DEFAULT_PORT)
        self.agent = None
        self.on_message: OnMessage | None = None
        self.auth: access.Auth | None = None
        self.bound = ""
        self.urls: list[str] = []
        self.started = time.time()
        self.clients: dict[str, Client] = {}
        self.pending: dict[str, Pending] = {}
        self.live: dict[str, dict[str, Any]] = {}     # who -> latest progress, while something is in flight
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self._ids = itertools.count(1)
        self._dirty: set[str] = set()
        self._flush: asyncio.TimerHandle | None = None
        self._server = None
        self._serving: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- Channel -------------------------------------------------------------------------------------
    def attach(self, agent) -> None:  # noqa: ANN001 - called by the gateway before connect
        self.agent = agent
        agent.thread.listeners.append(self._thread_changed)

    async def connect(self, on_message: OnMessage) -> None:
        import uvicorn
        self.on_message = on_message
        auth = str(self.cfg.get("auth") or ("tailscale" if self.listen == "tailscale" else "token"))
        if auth == "token" and not os.environ.get("WEB_TOKEN"):  # first start: make one, kept in the secrets file
            self.home.set_secret("WEB_TOKEN", access.new_token())
            self.log(f"Web: made a sign-in token (WEB_TOKEN in {self.home.secrets_file}); `daimon config web` "
                     "shows a link that signs a browser in")
        self.auth = access.make_auth(self.cfg, self.home)
        self.bound = await asyncio.to_thread(access.resolve_listen, self.listen)
        sock = socket.socket(socket.AF_INET6 if ":" in self.bound else socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":  # on Windows this would let another program take the port too
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.bound, self.port))
        except OSError as e:
            sock.close()
            raise OSError(f"can't listen on {self.bound}:{self.port} ({e.strerror or e}); is something else using "
                          f"port {self.port}? Change web.port in config.yaml") from None
        self.port = sock.getsockname()[1]  # the one picked, with port 0
        sock.listen(64)
        sock.setblocking(False)
        from .server import app
        config = uvicorn.Config(app(self), log_level="warning", access_log=False, lifespan="off",
                                proxy_headers=bool(self.cfg.get("behind_proxy")),
                                forwarded_allow_ips="*" if self.cfg.get("behind_proxy") else None)
        self._server = QuietServer(config)
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        self.urls = await asyncio.to_thread(access.page_urls, self.listen, self.bound, self.port)
        self.log(f"Web: open {self.urls[0]} ({self.auth.describe()})")

    async def close(self) -> None:
        for c in self.clients.values():
            c.queue.put_nowait(None)
        if self._server:
            self._server.should_exit = True
        if self._serving:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._serving, 5)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def send(self, text: str) -> None:
        """A reply to a message from the page, or the agent speaking up in the background."""
        self.broadcast({"t": "say", "text": text, "ts": time.time()})

    def event(self, kind: str, text: str) -> None:
        who = "main"
        if text.startswith("[") and "] " in text:
            who, text = text[1:].split("] ", 1)
        e = {"ts": time.time(), "kind": kind, "who": who, "text": text}
        self.events.append(e)
        self.broadcast({"t": "event", **e})
        if kind in ("task", "routine"):
            self.broadcast({"t": "changed", "what": "tasks"})
        elif kind == "edit":  # the agent changed one of its files: a new commit
            self.broadcast({"t": "changed", "what": "changes"})

    def progress(self, who: str, state: dict[str, Any]) -> None:
        """Live state of a model call or tool (progress.py). The times in it are this process's monotonic clock, so
        they're turned into ages before they go out."""
        now = time.monotonic()
        s = {k: v for k, v in state.items() if k not in ("started", "first_token")}
        if state.get("started"):
            s["elapsed"] = round(now - state["started"], 2)
        if state.get("first_token") and state.get("started"):
            s["ttft"] = round(state["first_token"] - state["started"], 2)
        prev = self.live.get(who, {})
        if state.get("phase") in ("idle", "done"):
            self.live.pop(who, None)
        else:
            if prev.get("model") and not s.get("model"):
                s["model"] = prev["model"]
            self.live[who] = s
        self._dirty.add(who)
        if s.get("phase") == "done" or s.get("phase") == "idle":
            self.broadcast({"t": "progress", "who": who, "state": s})
            self._dirty.discard(who)
        elif self._flush is None:
            self._flush = asyncio.get_running_loop().call_later(PROGRESS_EVERY_S, self._send_progress)

    def _send_progress(self) -> None:
        self._flush = None
        for who in list(self._dirty):
            if who in self.live:
                self.broadcast({"t": "progress", "who": who, "state": self.live[who]})
        self._dirty.clear()

    async def prompt(self, prompt: Prompt) -> str:
        p = Pending(str(next(self._ids)), prompt, asyncio.get_running_loop().create_future())
        self.pending[p.id] = p
        self.broadcast({"t": "prompt", **p.json()})
        outcome = "answered elsewhere"
        try:
            answer = await p.answer
            outcome = dict(prompt.options).get(answer, f'Replied: "{answer}"')
            return answer
        finally:
            self.pending.pop(p.id, None)
            self.broadcast({"t": "prompt_done", "id": p.id, "outcome": outcome})

    # ---- to the browsers -----------------------------------------------------------------------------
    def broadcast(self, item: dict[str, Any]) -> None:
        for c in list(self.clients.values()):
            try:
                c.queue.put_nowait(item)
            except asyncio.QueueFull:  # too far behind: drop it; the page reconnects and starts fresh
                self.clients.pop(c.id, None)
                with contextlib.suppress(asyncio.QueueFull):
                    c.queue.get_nowait()
                    c.queue.put_nowait(None)

    def _thread_changed(self, op: str, index: int | None = None, message: dict[str, Any] | None = None) -> None:
        if op == "append" and message is not None:
            self.broadcast({"t": "thread", "op": "append", "m": data.message(index, message)})
        else:
            self.broadcast({"t": "thread", "op": "reset"})

    def add_client(self, visitor: access.Visitor, ip: str) -> Client:
        c = Client(f"c{next(self._ids)}", visitor, ip, asyncio.Queue(QUEUE))
        self.clients[c.id] = c
        return c

    def drop_client(self, c: Client) -> None:
        self.clients.pop(c.id, None)

    def spawn(self, coro) -> None:  # noqa: ANN001
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---- what the page asks for ----------------------------------------------------------------------
    def waiting(self) -> list[dict[str, Any]]:
        """Everything that needs the user: approvals, then agent-proposed tasks, then workers' questions."""
        out = [{"kind": "approval", **p.json()} for p in self.pending.values()]
        for t in self.agent.tasks.all(("proposed", "waiting")):
            row = data.task_row(t)
            out.append({"kind": "proposal" if t.status == "proposed" else "question", **row})
        return out

    def snapshot(self, client: Client) -> dict[str, Any]:
        agent = self.agent
        return {"t": "hello", "client": client.id, "thread": data.thread(agent.thread), "live": self.live,
                "waiting": self.waiting(), "events": list(self.events)[-150:], "models": data.models(agent.home),
                "spend": {"today": data.spend(agent.home)["today"]}, "paused": getattr(agent, "paused", False),
                "started": self.started, "version": data.status(agent, self.started)["version"],
                "network": self.network()}

    def network(self) -> dict[str, Any]:
        return {"listen": self.listen, "bound": self.bound, "port": self.port, "urls": self.urls,
                "auth": self.auth.describe() if self.auth else "", "auth_name": self.auth.name if self.auth else "",
                "clients": [{"id": c.id, "device": c.visitor.device, "user": c.visitor.user, "ip": c.ip,
                             "since": c.since} for c in self.clients.values()]}


class QuietServer:
    """uvicorn's Server, minus its signal handling: Ctrl+C belongs to `daimon chat` / `daimon run`."""

    def __new__(cls, config):  # noqa: ANN001
        import uvicorn

        class _Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        return _Server(config)


def from_config(home, log: Callable[[str], None] = print) -> WebChannel | None:  # noqa: ANN001
    """The web channel if config.yaml's `web` section turns it on."""
    cfg = home.config().get("web") or {}
    if not cfg.get("enabled"):
        return None
    return WebChannel(home, cfg, log)


def to_json(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, default=str)
