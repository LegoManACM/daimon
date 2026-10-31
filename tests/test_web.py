"""The web page: hosting and access rules, signing in, the API, approvals and live updates."""
import asyncio
import json
import time

import httpx
import pytest

from daimon.agent import Agent
from daimon.gateway import Gateway, Prompt
from daimon.web import access, data
from daimon.web.channel import WebChannel
from daimon.web.server import app

from test_agent import home  # noqa: F401 - fixture


class Owner(access.Auth):
    """A custom auth module, loaded by name from config.yaml."""

    async def check(self, request):
        return access.Visitor("custom") if request.headers.get("x-owner") == "yes" else None


def make(home, monkeypatch, **cfg):  # noqa: F811
    monkeypatch.setenv("WEB_TOKEN", "sekrit-token")
    ch = WebChannel(home, {"enabled": True, "listen": "localhost", "port": 0, **cfg}, log=lambda _t: None)
    agent = Agent(home, Gateway([ch]))
    ch.attach(agent)
    ch.auth = access.make_auth(ch.cfg, home)
    sent = []

    async def on_message(msg):
        sent.append(msg.text)
    ch.on_message = on_message
    return ch, agent, sent


def client(ch, signed_in=True):
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app(ch)), base_url="http://test")
    if signed_in:
        c.cookies.set(access.COOKIE, ch.auth.session())
    return c


W = {"X-Daimon": "1"}


def test_listen_and_auth_rules(home, monkeypatch):  # noqa: F811
    assert access.resolve_listen("localhost") == "127.0.0.1"
    assert access.resolve_listen("all") == "0.0.0.0"
    assert access.resolve_listen("192.168.1.20") == "192.168.1.20"
    with pytest.raises(access.AccessError):
        access.resolve_listen("my-laptop")
    monkeypatch.setattr(access, "tailscale", lambda *a, **k: "100.64.0.7\nfd7a::7\n")
    assert access.resolve_listen("tailscale") == "100.64.0.7"

    monkeypatch.delenv("WEB_TOKEN", raising=False)
    with pytest.raises(access.AccessError, match="WEB_TOKEN"):
        access.make_auth({"listen": "localhost"})
    monkeypatch.setenv("WEB_TOKEN", "t")
    assert isinstance(access.make_auth({"listen": "all"}), access.TokenAuth)
    assert isinstance(access.make_auth({"listen": "tailscale"}), access.TailscaleAuth)  # its default
    assert isinstance(access.make_auth({"listen": "localhost", "auth": "none"}), access.OpenAuth)
    with pytest.raises(access.AccessError, match="anyone"):  # no sign-in on a network address needs saying so
        access.make_auth({"listen": "all", "auth": "none"})
    assert isinstance(access.make_auth({"listen": "all", "auth": "none", "allow_open": True}), access.OpenAuth)
    assert isinstance(access.make_auth({"auth": "test_web:Owner"}), Owner)
    with pytest.raises(access.AccessError):
        access.make_auth({"auth": "password"})


def test_tailscale_auth_lets_in_only_the_owners_devices(monkeypatch):
    calls = []

    def fake(*args, **_k):
        calls.append(args)
        if args[0] == "status":
            return json.dumps({"Self": {"UserID": 1, "DNSName": "desk.tail1.ts.net."}})
        users = {"100.64.0.2": (1, "phone"), "100.64.0.3": (2, "friends-laptop")}
        uid, name = users[args[2]]
        return json.dumps({"UserProfile": {"ID": uid, "LoginName": f"u{uid}@x"}, "Node": {"ComputedName": name}})
    monkeypatch.setattr(access, "tailscale", fake)
    auth = access.TailscaleAuth({})

    class R:
        def __init__(self, ip):
            self.client, self.headers = type("C", (), {"host": ip})(), {"user-agent": "Mozilla (iPhone) Safari"}

    async def go():
        mine = await auth.check(R("100.64.0.2"))
        assert mine and mine.device.startswith("phone") and mine.user == "u1@x"
        assert await auth.check(R("100.64.0.3")) is None  # on the tailnet, but someone else's device
        assert (await auth.check(R("127.0.0.1"))).device.startswith("this computer")
        n = len(calls)
        await auth.check(R("100.64.0.2"))
        assert len(calls) == n  # remembered for a while
    asyncio.run(go())
    assert access.page_urls("tailscale", "100.64.0.1", 8770)[0] == "http://desk:8770"


def test_signing_in_and_the_guard(home, monkeypatch):  # noqa: F811
    ch, agent, _ = make(home, monkeypatch)

    async def go():
        async with client(ch, signed_in=False) as c:
            r = await c.get("/")
            assert r.status_code == 200 and "Sign in to Daimon" in r.text
            assert (await c.get("/api/status")).status_code == 401
            assert (await c.post("/login", json={"token": "nope"})).status_code == 401
            r = await c.get("/?token=sekrit-token")
            assert r.status_code == 303 and access.COOKIE in r.headers["set-cookie"]
            assert "sekrit-token" not in r.headers["set-cookie"]  # the cookie never holds the token itself
            assert "HttpOnly" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()
            r = await c.get("/")
            assert "Message daimon" in r.text
            assert (await c.get("/api/status")).json()["home"] == str(home.root)
            assert (await c.post("/api/pause", json={"paused": True})).status_code == 403  # no X-Daimon header
            assert (await c.post("/api/pause", json={"paused": True}, headers=W)).json()["paused"] is True
            assert agent.paused
        monkeypatch.setenv("WEB_TOKEN", "rotated")  # a new token signs everyone out
        async with client(ch, signed_in=False) as c:
            c.cookies.set(access.COOKIE, "0" * 64)
            assert (await c.get("/api/status")).status_code == 401
    asyncio.run(go())


def test_approvals_messages_and_live_updates(home, monkeypatch):  # noqa: F811
    ch, agent, sent = make(home, monkeypatch)

    async def go():
        seen = ch.add_client(access.Visitor("test"), "127.0.0.1").queue
        asking = asyncio.create_task(ch.prompt(Prompt("calendar__add", {"title": "Study group"}, [("y", "Yes"), ("n", "No")])))
        await asyncio.sleep(0)
        [w] = ch.waiting()
        assert w["kind"] == "approval" and w["details"] == {"title": "Study group"}
        async with client(ch) as c:
            assert (await c.post("/api/answer", json={"id": w["id"], "value": "y"}, headers=W)).json()["ok"]
            assert await asyncio.wait_for(asking, 1) == "y"
            assert (await c.post("/api/answer", json={"id": w["id"], "value": "n"}, headers=W)).status_code == 409
            # a plain message goes to the agent; "Reply…" on a card answers that card instead
            await c.post("/api/send", json={"text": "hello there"}, headers=W)
            await asyncio.sleep(0.01)
            assert sent == ["hello there"]
            asking = asyncio.create_task(ch.prompt(Prompt("send_text", {}, [("y", "Yes")])))
            await asyncio.sleep(0)
            pid = ch.waiting()[0]["id"]
            await c.post("/api/send", json={"text": "not to Sam", "answer": pid}, headers=W)
            assert await asyncio.wait_for(asking, 1) == "not to Sam" and sent == ["hello there"]
        kinds = []
        while not seen.empty():
            kinds.append(seen.get_nowait())
        assert [k["t"] for k in kinds[:2]] == ["prompt", "prompt_done"] and kinds[1]["outcome"] == "Yes"

        agent.thread.append({"role": "user", "content": "hi", "ts": time.time()})
        item = seen.get_nowait()
        assert item["t"] == "thread" and item["m"]["content"] == "hi" and item["m"]["i"] == 0

        for n in range(20):  # a stream of chunks reaches the page at a calmer rate
            ch.progress("main", {"phase": "writing", "model": "m", "started": time.monotonic() - 2, "tokens": n,
                                 "first_token": time.monotonic() - 1, "text": "x" * n})
        assert seen.empty()
        await asyncio.sleep(0.2)
        [p] = [seen.get_nowait() for _ in range(seen.qsize())]
        assert p["state"]["tokens"] == 19 and "started" not in p["state"] and p["state"]["elapsed"] >= 2
        ch.progress("main", {"phase": "done", "tokens": 20})
        assert seen.get_nowait()["state"]["phase"] == "done" and "main" not in ch.live

        ch.event("tool", "[researcher · 2026-10-31-x] web_search cats")
        e = seen.get_nowait()
        assert e["who"] == "researcher · 2026-10-31-x" and e["text"] == "web_search cats"
        snap = ch.snapshot(ch.add_client(access.Visitor("t"), "x"))
        assert snap["thread"]["messages"][0]["content"] == "hi" and snap["events"][-1]["kind"] == "tool"
    asyncio.run(go())


def test_tasks_models_changes_and_spend(home, monkeypatch):  # noqa: F811
    ch, agent, _ = make(home, monkeypatch)
    models = home.models()
    models["models"].append({"id": "ollama_chat/other", "tier": "free"})
    home.write_yaml("models.yaml", models)
    home.commit("test: another model")
    proposed = agent.tasks.create("Check the grade", "when it posts", status="proposed")
    waiting = agent.tasks.create("Flights", "find flights")
    agent.tasks.update(waiting.id, status="waiting", log="asked main: Tue or Wed?")
    home.cache.mkdir(exist_ok=True)
    with (home.cache / "spend.jsonl").open("a", encoding="utf-8") as f:
        for model, worker, usd in (("a/big", "main", 0.5), ("a/big", "main", 0.25), ("b/small", "researcher", 0.1)):
            f.write(json.dumps({"ts": time.time(), "model": model, "worker": worker, "usd": usd, "in": 1000, "out": 10}) + "\n")
        f.write(json.dumps({"ts": time.time() - 40 * 86400, "model": "a/big", "worker": "main", "usd": 9}) + "\n")

    async def go():
        async with client(ch) as c:
            kinds = {w["kind"]: w for w in (await c.get("/api/waiting")).json()["waiting"]}
            assert kinds["question"]["question"] == "Tue or Wed?" and kinds["proposal"]["id"] == proposed.id
            r = await c.post(f"/api/task/{proposed.id}", json={"action": "go"}, headers=W)
            assert r.json()["status"] == "queued"
            r = await c.post(f"/api/task/{proposed.id}", json={"action": "go"}, headers=W)
            assert r.status_code == 400  # it isn't proposed any more
            r = await c.post(f"/api/task/{waiting.id}", json={"action": "answer", "text": "Wednesday"}, headers=W)
            assert r.json()["status"] == "queued" and "answered: Wednesday" in r.json()["sections"]["Log"]
            r = await c.post("/api/tasks", json={"title": "Water plants", "worker": "nobody"}, headers=W)
            assert r.status_code == 400
            assert {t["title"] for t in (await c.get("/api/tasks")).json()["tasks"]} >= {"Check the grade", "Flights"}

            before = home.worker("main").model
            r = await c.post("/api/model", json={"model": "ollama_chat/other"}, headers=W)
            assert r.json()["main"] == "ollama_chat/other" and home.worker("main").model == "ollama_chat/other"
            assert "mcps: all" in home.read("workers/main.md")  # the rest of the file is untouched
            assert (await c.post("/api/model", json={"model": "nope/x"}, headers=W)).status_code == 400
            [latest, *_] = (await c.get("/api/changes")).json()["changes"]
            assert "main model is now ollama_chat/other" in latest["subject"]
            assert latest["files"] == [{"op": "M", "path": "workers/main.md"}]
            assert "+model: ollama_chat/other" in (await c.get(f"/api/changes/{latest['sha']}")).json()["diff"]
            await c.post(f"/api/changes/{latest['sha']}/undo", json={}, headers=W)
            assert home.worker("main").model == before

            s = (await c.get("/api/spend")).json()
            assert s["today"] == pytest.approx(0.85) and s["calls"] == 3
            assert [g["name"] for g in s["by_model"]] == ["a/big", "b/small"] and s["by_model"][0]["calls"] == 2
            assert s["by_worker"][1] == {"name": "researcher", "usd": 0.1, "calls": 1, "in": 1000, "out": 10}
            assert s["month"] == pytest.approx(0.85)  # the call 40 days ago is last month's

            assert (await c.get("/api/file?path=SELF.md")).json()["text"]
            assert (await c.get("/api/file?path=../secrets.env")).status_code == 400
            assert (await c.get("/api/file?path=.git/config")).status_code == 400
    asyncio.run(go())


def test_serves_on_a_real_port(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("WEB_TOKEN", raising=False)
    ch = WebChannel(home, {"enabled": True, "port": 0}, log=lambda _t: None)
    agent = Agent(home, Gateway([ch]))

    async def go():
        ch.attach(agent)
        await ch.connect(lambda _m: asyncio.sleep(0))
        try:
            assert ch.port and ch.urls == [f"http://localhost:{ch.port}"]
            assert "WEB_TOKEN=" in home.secrets_file.read_text(encoding="utf-8")  # made on first start
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{ch.port}/")
                assert "Sign in" in r.text
                r = await c.get(f"http://127.0.0.1:{ch.port}/?token={ch.auth.token()}", follow_redirects=True)
                assert "Message daimon" in r.text
            taken = WebChannel(home, {"enabled": True, "port": ch.port}, log=lambda _t: None)
            taken.attach(agent)
            with pytest.raises(OSError, match="port"):
                await taken.connect(lambda _m: asyncio.sleep(0))
        finally:
            await ch.close()
    asyncio.run(go())


def test_spend_with_no_log(home):  # noqa: F811
    assert data.spend(home)["today"] == 0 and data.spend(home)["by_model"] == []
