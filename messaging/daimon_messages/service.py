"""The running service (`daimon-messages run`): owns the Google Messages link and is the only writer of messages.db.

    sidecar (Go, libgm) --events--> Service --filters--> Store (messages.db) <--reads-- MCP server, CLI
                                       ^
    CLI, MCP server --control API------+   (127.0.0.1 only, bearer token from control.json)

Every message you send or get is stored as it arrives unless it's filtered (login codes, short codes, the denylist).
On connect it catches up from just before the newest stored message (the first pairing fetches `backfill_days`).

The control API is plain JSON over HTTP for the few things only the live link can do:
  GET  /status
  POST /pair {method: qr|google, cookies}   POST /cancel   POST /unpair {wipe}
  POST /backfill {days}                     POST /send {chat | to, text}   POST /contacts
  POST /watch/test {name, since, chat}      (see watch.py: watchers run here, over each new message)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Home
from .filters import blocked
from .sidecar import Sidecar, backfill_since, find_go
from .store import Store, utcnow
from .watch import Engine, Folder, seed

log = logging.getLogger(__name__)

UPKEEP_EVERY_S = 3600
CONTACTS_EVERY_S = 24 * 3600


class Service:
    def __init__(self, home: Home, store: Store | None = None):
        self.home = home
        self.cfg = home.config()
        self.store = store or Store(home.db)
        self.sidecar = Sidecar(home.bin, home.session, self.on_event)
        self.backfill: dict[str, Any] = {"running": False, "messages": 0, "done": self.store.kv_get("backfill_done")}
        self.dropped = 0  # messages filtered out since start
        self.received = 0  # new messages stored since start
        self._backfill_asked = False
        self._contacts_at = 0.0
        self._tasks: list[asyncio.Task] = []
        self._http: ThreadingHTTPServer | None = None
        self.watchers = Folder(home.watchers)
        seed(self.watchers)
        self.watch = Engine(self.watchers, home.watch_settings, self.store, self.cfg.tz())
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---- lifecycle ----

    async def start(self, control: bool = True) -> None:
        self.loop = asyncio.get_running_loop()
        if self.home.session.exists():
            self.sidecar.start()
        self._tasks.append(asyncio.create_task(self._upkeep()))
        self._tasks.append(asyncio.create_task(self.watch.run()))
        if control:
            self._serve_control()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._http:
            self._http.shutdown()
            self._http.server_close()
            with contextlib.suppress(FileNotFoundError):
                self.home.control.unlink()
        await self.sidecar.stop()

    async def _upkeep(self) -> None:
        while True:
            try:
                if gone := self.store.prune(self.cfg.retention_days):
                    log.info("Deleted %d message(s) older than %d days", gone, self.cfg.retention_days)
            except Exception:
                log.exception("Upkeep failed")
            await asyncio.sleep(UPKEEP_EVERY_S)

    # ---- events from the sidecar ----

    async def on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message":
            self._message(event)
        elif kind == "conversation":
            self.store.upsert_chat(str(event.get("id", "")), str(event.get("name", "")), bool(event.get("group")),
                                   [p for p in event.get("participants") or [] if isinstance(p, dict)],
                                   str(event.get("kind", "")), bool(event.get("read_only")))
        elif kind == "status":
            await self._status(str(event.get("state", "")))
        elif kind == "paired":
            if not self.store.kv_get("paired_at"):
                self.store.kv_set("paired_at", utcnow())
            self._backfill_asked = False
        elif kind == "backfill_done":
            self.backfill.update(running=False, messages=int(event.get("messages") or 0), done=utcnow())
            self.store.kv_set("backfill_done", self.backfill["done"])
            if event.get("error"):
                log.warning("Backfill ended early: %s", event["error"])
            else:
                log.info("Backfill done: %d message(s) seen", self.backfill["messages"])

    def window_start(self) -> datetime:
        """The oldest a stored message may be: the first pairing's week (from the pairing, or now if unpaired)."""
        paired = self.store.kv_get("paired_at")
        start = datetime.fromisoformat(paired) if paired else datetime.now(timezone.utc)
        return start - timedelta(days=max(self.cfg.backfill_days, 0))

    def _message(self, event: dict) -> None:
        if not event.get("id") or not event.get("conv"):
            return
        if event.get("deleted"):
            self.store.delete(str(event["id"]))
            return
        sent = datetime.fromtimestamp(int(event.get("ts_ms") or 0) / 1000, timezone.utc)
        if sent < self.window_start():
            # Google Messages resends some old messages when the link connects; nothing from before the first
            # pairing's window is kept.
            self.dropped += 1
            return
        chat = self.store.chat(str(event["conv"]))
        if why := blocked(event, chat, self.cfg.denylist, self.cfg.skip_short_codes, self.cfg.skip_login_codes):
            self.dropped += 1
            log.debug("Not storing a message (%s)", why)
            return
        if self.store.add(event):
            self.received += 1
            if event.get("backfill"):
                self.backfill["stored"] = self.backfill.get("stored", 0) + 1

    async def _status(self, state: str) -> None:
        if state == "connected" and not self._backfill_asked:
            self._backfill_asked = True
            since = backfill_since(self.store.last_ts(), self.cfg.backfill_days)
            self.backfill.update(running=True, messages=0, stored=0)
            await self.sidecar.send({"cmd": "backfill", "since_ms": since})
            if not self._contacts_at or asyncio.get_running_loop().time() - self._contacts_at > CONTACTS_EVERY_S:
                asyncio.create_task(self.refresh_contacts())
        elif state in ("logged_out", "disconnected", "unpaired"):
            self._backfill_asked = False  # a reconnect catches up on what it missed
            if state == "logged_out":
                log.warning("Google Messages unpaired this computer. Pair again with `daimon-messages pair`.")

    # ---- actions ----

    async def pair(self, method: str = "qr", cookies: dict | None = None) -> str:
        """Starts pairing. Returns '' or why it can't."""
        if self.sidecar.state in ("connected", "connecting", "phone_offline") and self.home.session.exists():
            return "Already paired. Unpair first to pair a different phone."
        if not self.sidecar.built() and (err := await self.sidecar.build()):
            return err
        if not await self.sidecar.running():
            return self.sidecar.detail or "The Google Messages link didn't start."
        if method == "google":
            if not cookies:
                return "Signing in with Google needs the sign-in cookies."
            ok = await self.sidecar.send({"cmd": "pair_google", "cookies": cookies})
        else:
            ok = await self.sidecar.send({"cmd": "pair_qr"})
        return "" if ok else "The Google Messages link isn't responding."

    async def cancel_pairing(self) -> None:
        await self.sidecar.send({"cmd": "cancel_pair"})

    async def unpair(self, wipe: bool = False) -> None:
        if self.sidecar.proc is not None and self.sidecar.proc.returncode is None:
            await self.sidecar.send({"cmd": "unpair"})
            for _ in range(50):
                if not self.home.session.exists():
                    break
                await asyncio.sleep(0.4)
        with contextlib.suppress(FileNotFoundError):
            self.home.session.unlink()
        await self.sidecar.stop()
        self.sidecar.state = "unpaired"
        if wipe:
            self.store.wipe()
            self.backfill = {"running": False, "messages": 0, "done": ""}
        self.store.kv_set("paired_at", "")

    async def catch_up(self, days: int | None = None) -> str:
        if self.sidecar.state != "connected":
            return f"Not connected ({self.sidecar.state})."
        days = self.cfg.backfill_days if days is None else days
        since = backfill_since(None, days)
        self.backfill.update(running=True, messages=0, stored=0)
        await self.sidecar.send({"cmd": "backfill", "since_ms": since})
        return ""

    async def send_text(self, text: str, chat: str = "", to: str = "") -> dict:
        if not self.cfg.allow_send:
            return {"ok": False, "error": "Sending is switched off (allow_send in config.yaml)."}
        if self.sidecar.state not in ("connected", "phone_offline"):
            return {"ok": False, "error": f"The Google Messages link isn't connected ({self.sidecar.state})."}
        reply = await self.sidecar.request({"cmd": "send", "conv": chat, "to": to, "text": text}, timeout=60)
        if reply.get("error") or not reply.get("ok"):
            return {"ok": False, "chat": reply.get("conv", chat), "error": reply.get("error") or "sending failed"}
        log.info("Sent a message to %s", self.store.chat_name(str(reply.get("conv", ""))))
        return {"ok": True, "chat": reply.get("conv", chat)}

    async def refresh_contacts(self) -> dict:
        reply = await self.sidecar.request({"cmd": "contacts"}, timeout=45)
        if reply.get("error"):
            log.warning("Couldn't list contacts: %s", reply["error"])
            return {"ok": False, "error": reply["error"]}
        n = self.store.set_contacts([c for c in reply.get("contacts") or [] if isinstance(c, dict)])
        self._contacts_at = asyncio.get_running_loop().time()
        log.info("Contacts refreshed: %d", n)
        return {"ok": True, "count": n}

    async def test_watcher(self, body: dict) -> tuple[int, dict]:
        w = self.watchers.get(str(body.get("name") or ""))
        if w is None:
            return 404, {"error": f"No watcher named {body.get('name')!r}."}
        chat_id = ""
        if ref := str(body.get("chat") or ""):
            chat, _ = self.store.resolve_chat(ref)
            if chat is None:
                return 400, {"error": f"No single chat matches {ref!r}."}
            chat_id = chat["id"]
        since = str(body.get("since") or "") or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        result = await self.watch.test(w, since, chat_id, int(body.get("max_checks") or 6))
        return 200, result | {"ok": True}

    def status(self) -> dict:
        sc = self.sidecar
        counts = self.store.counts()
        return {
            "running": True, "pid": os.getpid(), "state": sc.state, "detail": sc.detail, "since": sc.since_iso(),
            "built": sc.built(), "go": bool(find_go()), "paired": self.home.session.exists(),
            "paired_at": self.store.kv_get("paired_at"), "qr": sc.qr, "emoji": sc.emoji, "pair_error": sc.pair_error,
            "messages": counts["n"], "chats": counts["chats"], "first": counts["first"], "last": counts["last"],
            "received": self.received, "dropped": self.dropped, "backfill": self.backfill,
            "allow_send": self.cfg.allow_send, "watch": self.watch.stats | {
                "model": self.watch.settings().model, "waiting_chats": len(self.watch.pending)},
        }

    # ---- control API ----

    async def handle(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        if method == "GET" and path == "/status":
            return 200, self.status()
        if method != "POST":
            return 404, {"error": "not found"}
        if path == "/pair":
            err = await self.pair(str(body.get("method") or "qr"), body.get("cookies"))
            return (400, {"error": err}) if err else (200, {"ok": True})
        if path == "/cancel":
            await self.cancel_pairing()
            return 200, {"ok": True}
        if path == "/unpair":
            await self.unpair(bool(body.get("wipe")))
            return 200, {"ok": True}
        if path == "/backfill":
            err = await self.catch_up(body.get("days"))
            return (400, {"error": err}) if err else (200, {"ok": True})
        if path == "/send":
            result = await self.send_text(str(body.get("text") or ""), str(body.get("chat") or ""),
                                          str(body.get("to") or ""))
            return (200 if result["ok"] else 400), result
        if path == "/watch/test":
            return await self.test_watcher(body)
        if path == "/contacts":
            result = await self.refresh_contacts()
            return (200 if result["ok"] else 400), result
        return 404, {"error": "not found"}

    def _serve_control(self) -> None:
        token = secrets.token_urlsafe(24)
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _dispatch(self, method: str) -> None:
                if self.headers.get("Authorization") != f"Bearer {token}":
                    return self._reply(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    fut = asyncio.run_coroutine_threadsafe(service.handle(method, self.path, body), service.loop)
                    code, payload = fut.result(timeout=600)
                except Exception as e:  # noqa: BLE001
                    log.exception("Control request failed")
                    code, payload = 500, {"error": str(e)}
                self._reply(code, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("control: " + fmt, *args)

        self._http = ThreadingHTTPServer(("127.0.0.1", self.cfg.control_port), Handler)
        port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, name="control", daemon=True).start()
        tmp = self.home.control.with_suffix(".tmp")
        tmp.write_text(json.dumps({"port": port, "token": token, "pid": os.getpid()}), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.home.control)
        log.info("Control API on 127.0.0.1:%d", port)


async def run(home: Home) -> None:
    service = Service(home)
    await service.start()
    log.info("Messaging service running (home %s, %s)", home.root,
             "paired" if home.session.exists() else "not paired: run `daimon-messages pair`")
    try:
        await asyncio.Event().wait()
    finally:
        await service.stop()
