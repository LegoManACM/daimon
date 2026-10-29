import asyncio

import yaml

from conftest import conversation, message
from daimon_messages.judge import read_verdict
from daimon_messages.mcp_server import build
from daimon_messages.service import Service
from daimon_messages.watch import Engine, Folder, Settings, Watcher, matches, parse, save_settings, seed
from test_service import FakeSidecar


HIT = {"match": True, "evidence": ["m1"], "summary": "Sam lands 6:40pm", "fields": {"time": "6:40pm"}}


def setup(home, tmp_path, verdict=None):
    """A service with a fake sidecar, a judge that answers `verdict` (or each of a list in turn), and the agent's
    inbox under tmp_path."""
    s = Service(home)
    s.sidecar = FakeSidecar()
    asked = []
    answers = verdict if isinstance(verdict, list) else None

    async def judge(model, params, messages):
        asked.append((model, messages[-1]["content"]))
        return dict(answers.pop(0) if answers else verdict or HIT)

    s.watch.judge = judge
    inbox = tmp_path / "agent" / "inbox"
    inbox.parent.mkdir()
    save_settings(home.watch_settings, Settings(model="ollama_chat/test", agent_inbox=str(inbox), quiet_seconds=0))
    for p in home.watchers.glob("*.md"):  # no starters here
        p.unlink()
    return s, asked, inbox


def test_starters_seeded_once(home):
    folder = Folder(home.watchers)
    seed(folder)
    assert set(folder.all()) == {"plans", "changes"}
    folder.remove("plans")
    seed(folder)
    assert set(folder.all()) == {"changes"}


def test_watcher_file_round_trip():
    w = Watcher(name="sam-flight", description="Sam's flight", look_for="When Sam lands.",
                match={"chats": ["Sam"], "any": ["land*"]}, fields=["time"], once=True, then="Remind me.")
    back = parse("sam-flight", w.dump())
    assert back == w
    w.fields = {"start": "ISO 8601"}
    assert parse("sam-flight", w.dump()) == w


def test_match():
    chat = {"id": "c1", "name": "", "is_group": 0,
            "participants": [{"name": "Sam Lee", "number": "+15551234567"}, {"name": "Me", "me": True}]}
    msg = {"chat": "c1", "from_me": 0, "sender": "Sam Lee", "number": "+15551234567", "body": "Landing at 6:40!"}
    assert matches(Watcher("a", match={"chats": ["sam"], "any": ["land*"]}), msg, chat)
    assert matches(Watcher("a", match={"from": ["5551234567"]}), msg, chat)
    assert not matches(Watcher("a", match={"any": ["land"]}), msg, chat)  # whole words unless land*
    assert not matches(Watcher("a", match={"chats": ["Alex"]}), msg, chat)
    assert not matches(Watcher("a", match={"groups": "only"}), msg, chat)
    assert not matches(Watcher("a"), msg | {"from_me": 1}, chat)  # incoming only by default
    assert matches(Watcher("a", match={"preset": "changes"}), msg | {"body": "can't make it tonight"}, chat)


def test_judged_watcher_wakes_the_agent(home, tmp_path):
    async def go():
        s, asked, inbox = setup(home, tmp_path, verdict=[{"match": False}, HIT, HIT])
        s.watchers.save(Watcher(name="sam-flight", description="Sam's flight", look_for="When Sam lands.",
                                match={"chats": ["Sam"]}, fields={"time": "ISO 8601 local datetime"},
                                then="Remind me to leave.", once=True))
        await s.on_event(conversation())
        await s.on_event({"type": "paired"})
        await s.on_event(message("m0", "boarding now", minutes_ago=30))
        await s.watch.step(force=True)
        await s.on_event(message("m1", "lands 6:40 tonight"))
        await s.on_event(message("x1", "old history", backfill=True))
        await s.watch.step(force=True)
        assert len(asked) == 2  # one read per burst; history isn't read
        prompt = asked[1][1]
        assert "e1:" in prompt and "boarding now" in prompt and "m1:" in prompt
        assert "The newest text was sent" in prompt and "- time: ISO 8601 local datetime" in prompt
        await s.on_event(message("m2", "landed!"))
        await s.watch.step(force=True)
        assert len(asked) == 2  # once: switched off after the hit
        files = list(inbox.glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "for: main" in text and "Remind me to leave." in text and "time: 6:40pm" in text
        meta = yaml.safe_load(text.split("---")[1])
        assert meta["event"] == "messages.watch_hit"
        assert meta["data"] | {"received": ""} == {"time": "6:40pm", "watcher": "sam-flight", "chat_id": "c1",
                                                   "chat": "Sam Lee", "from": "Sam Lee",
                                                   "summary": "Sam lands 6:40pm", "message_ids": ["m1"],
                                                   "received": ""}
        assert not s.watchers.get("sam-flight").enabled
        assert s.store.hits("sam-flight")[0]["messages"] == ["m1"]

    asyncio.run(go())


def test_rule_only_watcher_and_no_match(home, tmp_path):
    async def go():
        s, asked, inbox = setup(home, tmp_path, verdict={"match": False})
        s.watchers.save(Watcher(name="code-word", judge=False, match={"any": ["pineapple"]}, action="log"))
        s.watchers.save(Watcher(name="judged", look_for="anything", action="wake"))
        await s.on_event(conversation())
        await s.on_event({"type": "paired"})
        await s.on_event(message("m1", "PINEAPPLE time"))
        await s.watch.step(force=True)
        assert [h["watcher"] for h in s.store.hits()] == ["code-word"]  # judged one said no
        assert not list(inbox.glob("*.md"))  # log only
        assert len(asked) == 1

    asyncio.run(go())


def test_no_model_means_inactive(home, tmp_path):
    async def go():
        s, asked, _ = setup(home, tmp_path)
        save_settings(home.watch_settings, Settings(model=""))
        s.watchers.save(Watcher(name="judged", look_for="anything"))
        await s.on_event(conversation())
        await s.on_event({"type": "paired"})
        await s.on_event(message("m1", "hi"))
        await s.watch.step(force=True)
        assert not asked and not s.store.hits()
        assert "needs a model" in s.watchers.get("judged").problems(s.watch.settings())[0]

    asyncio.run(go())


def test_test_watcher_through_control(home, tmp_path):
    async def go():
        s, asked, inbox = setup(home, tmp_path)
        s.watchers.save(Watcher(name="sam", look_for="plans", match={"chats": ["Sam"]}))
        await s.on_event(conversation())
        await s.on_event({"type": "paired"})
        await s.on_event(message("m1", "dinner thursday?"))
        s.store.ack("watch", 10**6)  # pretend the live pass already went by
        code, r = await s.test_watcher({"name": "sam"})
        assert code == 200 and r["matched"] == 1 and r["results"][0]["match"]
        assert not list(inbox.glob("*.md"))  # a test delivers nothing

    asyncio.run(go())


def test_mcp_set_and_list(home):
    server = build(home)

    async def go():
        tools = {t.name for t in await server.list_tools()}
        assert {"set_watcher", "list_watchers", "test_watcher", "watch_settings", "watcher_hits"} <= tools
        r = await server.call_tool("set_watcher", {"name": "mom-sunday", "look_for": "Mom about Sunday",
                                                   "chats": ["Mom"], "any_words": ["sunday"]})
        assert "Made watcher" in str(r) and "needs a model" in str(r)
        r = await server.call_tool("watch_settings", {"model": "ollama_chat/qwen3.5:9b"})
        assert "Changed model" in str(r)
        r = await server.call_tool("set_watcher", {"name": "mom-sunday", "once": True})
        assert "Changed watcher" in str(r) and "inactive" not in str(r)
        w = Folder(home.watchers).get("mom-sunday")
        assert w.once and w.match == {"chats": ["Mom"], "any": ["sunday"]}
        assert "mom-sunday" in str(await server.call_tool("list_watchers", {}))
        r = await server.call_tool("set_watcher", {"name": "Bad Name!"})
        assert "lowercase" in str(r)

    asyncio.run(go())


def test_read_verdict():
    assert read_verdict('<think>hmm {"x":1}</think> Sure: {"match": "yes", "evidence": ["m2"]}')["match"] is True
    assert read_verdict('{"match": false}') == {"match": False, "evidence": [], "fields": {}}


def test_engine_starts_at_the_end_of_an_existing_store(home):
    from daimon_messages.store import Store
    store = Store(home.db)
    store.upsert_chat("c1", "", False, [])
    store.add(message("m1", "old"))
    e = Engine(Folder(home.watchers), home.watch_settings, store, None)
    assert store.feed("watch") == []
    assert e.pending == {}
