"""HTTP routes for the web channel. Every request passes the channel's Auth (access.py) first.

Requests that change something are POSTs that must carry `X-Daimon: 1`. A browser only sends a custom header like
that from the page itself (other sites would need CORS, which this server never allows), so a link or form on
another site can't act as you.
"""
from __future__ import annotations

import asyncio
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from ..gateway import Message
from ..tasks import STATUSES
from . import access, data
from .channel import STATIC, WebChannel, to_json


HEARTBEAT_S = 15
NO_STORE = {"Cache-Control": "no-store"}


def app(ch: WebChannel) -> Starlette:
    agent = lambda: ch.agent  # noqa: E731 - set by attach() before the server starts

    async def visitor(request: Request) -> access.Visitor | None:
        try:
            return await ch.auth.check(request)
        except access.AccessError:
            return None

    def guard(handler, write: bool = False):
        async def wrapped(request: Request) -> Response:
            v = await visitor(request)
            if v is None:
                return JSONResponse({"error": "sign in first"}, 401)
            if write and request.headers.get("x-daimon") != "1":
                return JSONResponse({"error": "missing X-Daimon header"}, 403)
            try:
                return await handler(request)
            except (ValueError, FileNotFoundError) as e:
                return JSONResponse({"error": str(e)}, 400)
        return wrapped

    async def body(request: Request) -> dict[str, Any]:
        try:
            got = await request.json()
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

    # ---- the page and signing in ---------------------------------------------------------------------
    async def index(request: Request) -> Response:
        token = request.query_params.get("token")
        if token is not None and isinstance(ch.auth, access.TokenAuth):
            return await sign_in(request, token, redirect=True)
        if await visitor(request) is None:
            if ch.auth.sign_in:
                return HTMLResponse((STATIC / "signin.html").read_text(encoding="utf-8"), headers=NO_STORE)
            return HTMLResponse(DENIED, 403)
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers=NO_STORE)

    async def sign_in(request: Request, token: str | None = None, redirect: bool = False) -> Response:
        if not isinstance(ch.auth, access.TokenAuth):
            return JSONResponse({"error": "this page doesn't use a sign-in token"}, 400)
        if token is None:
            form = await request.form() if "form" in request.headers.get("content-type", "") else await body(request)
            token = str(form.get("token") or "")
        if not ch.auth.valid_token(token):
            await asyncio.sleep(1)  # slows down guessing
            if redirect:
                return RedirectResponse("/?wrong=1", 303)
            return JSONResponse({"error": "That token isn't right."}, 401)
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp: Response = RedirectResponse("/", 303) if redirect else JSONResponse({"ok": True})
        resp.set_cookie(access.COOKIE, ch.auth.session(), max_age=400 * 86400, httponly=True, samesite="strict",
                        secure=secure)
        return resp

    async def login(request: Request) -> Response:
        return await sign_in(request)

    async def logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(access.COOKIE)
        return resp

    async def manifest(request: Request) -> Response:
        return Response((STATIC / "manifest.json").read_text(encoding="utf-8"), media_type="application/manifest+json")

    async def icon(request: Request) -> Response:
        return Response((STATIC / "icon.svg").read_text(encoding="utf-8"), media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})

    # ---- live updates --------------------------------------------------------------------------------
    async def stream(request: Request) -> Response:
        v = await visitor(request)
        if v is None:
            return JSONResponse({"error": "sign in first"}, 401)
        client = ch.add_client(v, request.client.host if request.client else "")
        ch.broadcast({"t": "changed", "what": "network"})

        async def events():
            try:
                yield f"data: {to_json(ch.snapshot(client))}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(client.queue.get(), HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        return
                    yield f"data: {to_json(item)}\n\n"
            finally:
                ch.drop_client(client)
                ch.broadcast({"t": "changed", "what": "network"})

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={**NO_STORE, "X-Accel-Buffering": "no"})

    # ---- reading -------------------------------------------------------------------------------------
    async def status(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(data.status, agent(), ch.started))

    async def spend(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(data.spend, agent().home))

    async def tasks(request: Request) -> Response:
        return JSONResponse({"tasks": await asyncio.to_thread(data.tasks, agent().tasks),
                             "workers": sorted(p.stem for p in (agent().home.root / "workers").glob("*.md"))})

    async def task(request: Request) -> Response:
        return JSONResponse(data.task(agent().tasks, request.path_params["id"]))

    async def schedule(request: Request) -> Response:
        return JSONResponse({"items": data.schedule(agent().home, agent().tasks)})

    async def changes(request: Request) -> Response:
        return JSONResponse({"changes": await asyncio.to_thread(data.changes, agent().home)})

    async def change(request: Request) -> Response:
        return JSONResponse({"diff": await asyncio.to_thread(data.diff, agent().home, request.path_params["sha"])})

    async def knowledge(request: Request) -> Response:
        kb = agent().runner.kb
        q = request.query_params.get("q", "").strip()
        if q:
            hits = await kb.search(q, limit=30)
            return JSONResponse({"count": kb.count(), "notes": [
                {"path": h["path"], "title": h.get("title") or h["path"], "type": h.get("type") or "",
                 "about": h.get("description") or (h.get("snippet") or "")[:200]} for h in hits]})
        files = sorted(kb.note_files().items(), key=lambda kv: -kv[1])[:60]
        notes = []
        for rel, mtime in files:
            meta, body = data.split_frontmatter((kb.root / rel).read_text(encoding="utf-8", errors="replace"))
            notes.append({"path": rel, "title": meta.get("title") or rel.rsplit("/", 1)[-1][:-3],
                          "type": meta.get("type") or "", "about": str(meta.get("description") or "")[:200],
                          "modified": mtime})
        return JSONResponse({"count": kb.count(), "notes": notes})

    async def note(request: Request) -> Response:
        return JSONResponse({"text": agent().runner.kb.read(request.query_params.get("path", ""))})

    async def files(request: Request) -> Response:
        return JSONResponse(data.files(agent().home))

    async def file(request: Request) -> Response:
        return JSONResponse({"text": data.read_file(agent().home, request.query_params.get("path", ""))})

    async def events(request: Request) -> Response:
        return JSONResponse({"events": list(ch.events)})

    async def models(request: Request) -> Response:
        return JSONResponse(data.models(agent().home))

    async def network(request: Request) -> Response:
        return JSONResponse(ch.network())

    async def waiting(request: Request) -> Response:
        return JSONResponse({"waiting": ch.waiting()})

    async def thread(request: Request) -> Response:
        return JSONResponse(data.thread(agent().thread, int(request.query_params.get("limit", 300))))

    # ---- acting --------------------------------------------------------------------------------------
    async def send(request: Request) -> Response:
        b = await body(request)
        text = str(b.get("text") or "").strip()
        if not text:
            raise ValueError("empty message")
        if b.get("answer"):  # "Reply…" on an approval card: declines it and tells the agent why
            p = ch.pending.get(str(b["answer"]))
            if p is None or p.answer.done():
                return JSONResponse({"error": "That was already answered."}, 409)
            p.answer.set_result(text)
            return JSONResponse({"ok": True, "answered": p.id})
        ch.spawn(ch.on_message(Message(text, ch.name)))
        return JSONResponse({"ok": True})

    async def answer(request: Request) -> Response:
        b = await body(request)
        p = ch.pending.get(str(b.get("id")))
        if p is None or p.answer.done():
            return JSONResponse({"error": "That was already answered."}, 409)
        p.answer.set_result(str(b.get("value") or ""))
        return JSONResponse({"ok": True})

    async def task_action(request: Request) -> Response:
        a, task_id = agent(), request.path_params["id"]
        b = await body(request)
        action, t = b.get("action"), a.tasks.get(task_id)
        if action == "go" and t.status == "proposed":
            a.tasks.update(task_id, status="queued", log="approved by the user on the web page")
        elif action == "skip" and t.status == "proposed":
            a.tasks.update(task_id, status="cancelled", log="skipped by the user on the web page")
        elif action == "cancel" and t.status in ("proposed", "queued", "running", "waiting"):
            a.tasks.update(task_id, status="cancelled", log="cancelled by the user on the web page")
        elif action == "answer" and t.status == "waiting":
            tool = next(x for x in a.main_extra() if x.name == "answer_task")
            await tool.run({"task_id": task_id, "answer": str(b.get("text") or "").strip()})
        elif action == "status" and b.get("status") in STATUSES:
            a.tasks.update(task_id, status=b["status"], log=f"set to {b['status']} on the web page")
        else:
            raise ValueError(f"can't {action} a {t.status} task")
        a._tick.set()
        ch.broadcast({"t": "changed", "what": "tasks"})
        return JSONResponse(data.task(a.tasks, task_id))

    async def new_task(request: Request) -> Response:
        a, b = agent(), await body(request)
        title, worker = str(b.get("title") or "").strip(), str(b.get("worker") or "main")
        if not title:
            raise ValueError("a task needs a title")
        a.home.worker(worker)  # raises if there's no such worker
        t = a.tasks.create(title, str(b.get("brief") or title).strip(), worker=worker, created_by="user (web)",
                           run_at=b.get("run_at") or None)
        a._tick.set()
        ch.broadcast({"t": "changed", "what": "tasks"})
        return JSONResponse(data.task(a.tasks, t.id))

    async def set_model(request: Request) -> Response:
        a = agent()
        model, before = str((await body(request)).get("model") or ""), a.home.worker("main").model
        sha = await asyncio.to_thread(data.set_main_model, a.home, model)
        a.runner.warm(model)
        ch.event("notify", f"main model: {before} → {model} (from the web page)")
        ch.broadcast({"t": "changed", "what": "models"})
        return JSONResponse({"ok": True, "commit": sha, "before": before, **data.models(a.home)})

    async def undo(request: Request) -> Response:
        a, sha = agent(), request.path_params["sha"]
        done = await asyncio.to_thread(a.home.revert, sha, "undone from the web page")
        ch.event("notify", f"undid {sha} (from the web page)")
        ch.broadcast({"t": "changed", "what": "changes"})
        return JSONResponse({"ok": True, "commit": done})

    async def pause(request: Request) -> Response:
        a = agent()
        a.paused = bool((await body(request)).get("paused"))
        if not a.paused:
            a._tick.set()
        ch.event("notify", "background work paused (from the web page)" if a.paused else "background work resumed")
        ch.broadcast({"t": "changed", "what": "status", "paused": a.paused})
        return JSONResponse({"ok": True, "paused": a.paused})

    async def command(request: Request) -> Response:
        a, name = agent(), (await body(request)).get("name")
        if name == "compact":
            done = await a.runner.maybe_compact(a.thread, force=True)
            return JSONResponse({"ok": True, "text": "Compacted." if done else "Nothing to compact yet."})
        if name == "clear":
            a.thread.clear()
            return JSONResponse({"ok": True, "text": "Started a fresh conversation."})
        raise ValueError(f"unknown command {name}")

    R = lambda path, fn, write=False: Route(path, guard(fn, write), methods=["POST"] if write else ["GET"])  # noqa: E731
    return Starlette(routes=[
        Route("/", index), Route("/login", login, methods=["POST"]),
        Route("/manifest.json", manifest), Route("/icon.svg", icon),
        R("/api/logout", logout, True),
        Route("/api/stream", stream),
        R("/api/status", status), R("/api/spend", spend), R("/api/tasks", tasks), R("/api/task/{id}", task),
        R("/api/schedule", schedule), R("/api/changes", changes), R("/api/changes/{sha}", change),
        R("/api/knowledge", knowledge), R("/api/note", note), R("/api/files", files), R("/api/file", file),
        R("/api/events", events), R("/api/models", models), R("/api/network", network), R("/api/waiting", waiting),
        R("/api/thread", thread),
        R("/api/send", send, True), R("/api/answer", answer, True), R("/api/task/{id}", task_action, True),
        R("/api/tasks", new_task, True), R("/api/model", set_model, True), R("/api/changes/{sha}/undo", undo, True),
        R("/api/pause", pause, True), R("/api/command", command, True),
    ])


DENIED = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Daimon</title><body style="font:15px system-ui;padding:40px 20px;max-width:440px;margin:auto;color:#444">
<h3>This device can't open Daimon</h3><p>The page only lets in the devices it's set up for (<code>web.auth</code>
in config.yaml). If this is your device, sign in to Tailscale with the same account as the computer running
Daimon.</p>"""

