"""The Finish setup checklist: what's done, skipping, and the sign-in link."""
import asyncio
import json

from test_agent import home  # noqa: F401 - fixture
from test_web import W, client, make


def steps(r):
    return {s["id"]: s for s in r.json()["steps"]}


def test_checklist_shape_and_doneness(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    ch, agent, _ = make(home, monkeypatch)
    async def go():
        async with client(ch) as c:
            r = await c.get("/api/setup")
            j = r.json()
            assert [s["id"] for s in j["steps"]] == ["model", "web", "telegram", "calendar", "messages", "canvas",
                                                      "claude", "devices"]
            assert j["total"] == 8 and not j["finished"]
            assert all({"id", "title", "done", "sub", "skipped"} <= set(s) for s in j["steps"])
            assert steps(r)["web"]["done"] and not steps(r)["telegram"]["done"] and not steps(r)["calendar"]["done"]
            before = j["done"]

            monkeypatch.setenv("TELEGRAM_TOKEN", "t")
            monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
            data = home.mcp()
            data["mcpServers"]["calendar"] = {"command": "x"}
            home.write_mcp(data)
            r = await c.get("/api/setup")
            assert steps(r)["telegram"]["done"] and steps(r)["calendar"]["done"]
            assert r.json()["done"] == before + 2

    asyncio.run(go())

def test_skip_unskip_and_dismiss_persist(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    ch, agent, _ = make(home, monkeypatch)
    path = home.cache / "setup.json"
    async def go():
        async with client(ch) as c:
            r = await c.post("/api/setup", json={"skip": "canvas"}, headers=W)
            assert steps(r)["canvas"]["skipped"]
            assert json.loads(path.read_text())["skipped"] == ["canvas"]
            r = await c.post("/api/setup", json={"unskip": "canvas"}, headers=W)
            assert not steps(r)["canvas"]["skipped"]
            assert json.loads(path.read_text())["skipped"] == []

            assert not (await c.get("/api/setup")).json()["finished"]
            r = await c.post("/api/setup", json={"dismissed": True}, headers=W)
            assert r.json()["finished"] and json.loads(path.read_text())["dismissed"] is True
            r = await c.post("/api/setup", json={"dismissed": False}, headers=W)
            assert not r.json()["finished"]

            for s in steps(r):
                await c.post("/api/setup", json={"skip": s}, headers=W)
            assert (await c.get("/api/setup")).json()["finished"]

    asyncio.run(go())

def test_link_gives_a_qr_and_marks_devices_done(home, monkeypatch):  # noqa: F811
    ch, agent, _ = make(home, monkeypatch)
    ch.urls = ["http://localhost:8770"]
    async def go():
        async with client(ch) as c:
            assert not steps(await c.get("/api/setup"))["devices"]["done"]
            j = (await c.get("/api/link")).json()
            assert j["url"] == "http://localhost:8770" and j["link"].startswith("http://localhost:8770")
            assert "<svg" in j["qr"]
            assert steps(await c.get("/api/setup"))["devices"]["done"]

    asyncio.run(go())
