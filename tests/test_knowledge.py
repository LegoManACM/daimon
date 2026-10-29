"""Step 3 tests: OKF notes, index, search, links, embeddings, retrieval, inbox -> curator."""
import asyncio
from types import SimpleNamespace as NS

import litellm
import pytest

from daimon import agent as agent_mod
from daimon import inbox
from daimon.agent import Agent
from daimon.home import Home, split_frontmatter
from daimon.knowledge import Knowledge, tools
from test_agent import Surface, call, script, until


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.setattr(agent_mod, "TICK_SECONDS", 0.05)
    monkeypatch.setattr(inbox, "SETTLE_SECONDS", 0)
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    h.write("routines/nightly.md", h.read("routines/nightly.md").replace("enabled: true", "enabled: false"))
    return h


def run(coro):
    return asyncio.run(coro)


def seed(kb):
    kb.write("people/sam.md", {"type": "person", "title": "Sam Lee", "description": "The user's climbing partner"},
             "Sam climbs on Thursdays. Works on [the gym project](../projects/gym.md).", "met Sam", source="sms")
    kb.write("projects/gym.md", {"type": "project", "title": "Climbing gym membership"},
             "Renewal due 2026-12-01.", "gym renewal", source="email:renewal")
    kb.write("preferences/drinks.md", {"type": "preference", "title": "Drinks"}, "Likes green tea, no coffee.",
             "drink prefs", source="user")


def test_write_read_search_links(home):
    kb = Knowledge(home)
    seed(kb)
    hits = run(kb.search("when does Sam climb"))
    assert hits[0]["path"] == "people/sam.md"
    assert run(kb.search("renewal", type="person")) == []
    run(kb.refresh())
    assert "Linked from: people/sam.md" in kb.read("projects/gym.md")
    assert kb.neighbors(["projects/gym.md"]) == ["people/sam.md"]
    meta, _ = split_frontmatter((kb.root / "people/sam.md").read_text(encoding="utf-8"))
    assert meta["sources"] == ["sms"] and meta["updated"]
    log = (kb.root / "log.md").read_text(encoding="utf-8")
    assert "created [people/sam.md](people/sam.md): met Sam (source: sms)" in log
    assert "knowledge: gym renewal" in home.history()
    assert kb.count() == 3  # index.md doesn't count


def test_sources_accumulate_and_validation(home):
    kb = Knowledge(home)
    seed(kb)
    kb.write("people/sam.md", {"type": "person", "title": "Sam Lee"}, "Sam moved to Fridays.", "update", source="user")
    meta, body = split_frontmatter((kb.root / "people/sam.md").read_text(encoding="utf-8"))
    assert meta["sources"] == ["sms", "user"] and "Fridays" in body
    with pytest.raises(ValueError, match="type"):
        kb.write("topics/x.md", {"title": "x"}, "x", "r")
    for bad in ("../SELF.md", "people/sam.txt", "/etc/passwd.md/../../x"):
        with pytest.raises(ValueError):
            kb.path(bad)


def test_edited_files_are_reindexed(home):
    kb = Knowledge(home)
    seed(kb)
    run(kb.refresh())
    p = kb.root / "preferences/drinks.md"
    import os, time
    p.write_text(p.read_text(encoding="utf-8").replace("green tea", "oolong"), encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert run(kb.search("oolong"))[0]["path"] == "preferences/drinks.md"
    p.unlink()
    assert run(kb.search("oolong")) == []


def test_context_includes_linked_notes(home):
    kb = Knowledge(home)
    seed(kb)
    ctx = run(kb.context_for("Sam", 6000))
    assert "## people/sam.md" in ctx and "## projects/gym.md" in ctx  # gym came in through the link
    assert "index.md" not in ctx
    assert len(run(kb.context_for("Sam", 120))) <= 120


def test_embeddings_find_meaning(home, monkeypatch):
    concepts = {"tea": [1, 0, 0], "beverage": [1, 0, 0], "climbs": [0, 1, 0], "renewal": [0, 0, 1]}

    async def fake_embedding(model, input):
        vecs = []
        for text in input:
            v = [0.0, 0.0, 0.0]
            for word, c in concepts.items():
                if word in text.lower():
                    v = [a + b for a, b in zip(v, c)]
            vecs.append({"embedding": v})
        return NS(data=vecs)

    monkeypatch.setattr(litellm, "aembedding", fake_embedding)
    kb = Knowledge(home)
    seed(kb)
    assert run(kb.search("beverage")) == []  # keyword search alone misses it
    home.write("config.yaml", home.read("config.yaml").replace("embeddings: none", "embeddings: fake/model"))
    assert run(kb.search("beverage"))[0]["path"] == "preferences/drinks.md"


def test_kg_tools(home):
    kb = Knowledge(home)
    t = {x.name: x for x in tools(kb)}
    out = run(t["kg_write"].run({"path": "people/ana.md", "type": "person", "title": "Ana", "body": "Ana is a TA.",
                                 "reason": "new TA", "source": "user"}))
    assert "Saved knowledge/people/ana.md" in out
    assert "people/ana.md" in run(t["kg_search"].run({"query": "TA"}))
    assert "Ana is a TA." in run(t["kg_read"].run({"path": "knowledge/people/ana.md"}))
    assert "people/ana.md [person] Ana" in run(t["kg_list"].run({}))
    assert t["kg_write"].level({}) == "auto"


def test_inbox_to_curator_and_urgent_to_main(home, monkeypatch):
    model = script(monkeypatch, {
        "curator": [call("kg_write", path="people/sam.md", type="person", title="Sam", reason="from sms",
                         body="Sam can't climb this Thursday.", source="sms"),
                    "Updated people/sam.md: Sam can't climb Thursday."],
        "main": ["[quiet] urgent one handled", "Sam cancelled Thursday climbing.", "Noted."],
    })
    inbox.drop(home, "Can't make Thursday, sorry!", source="sms", **{"from": "Sam"})
    inbox.drop(home, "Your package is at the door.", source="doorbell", **{"for": "main"})

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            await until(lambda: s.said)
            task = next(t for t in a.tasks.all() if t.worker == "curator")
            assert task.status == "done" and "Can't make Thursday" in task.sections["Brief"]
            assert (home.root / "knowledge/people/sam.md").exists()
            assert not list((home.root / "inbox").glob("*.md"))  # claimed and moved to processed/
            first = model.seen["main"][0][-1]["content"]
            assert "[Inbox item]" in first and "package" in first
            curator_prompt = model.seen["curator"][0][0]["content"]
            assert "You are the curator" in curator_prompt
        finally:
            await a.stop()
    run(go())


def test_worker_prompt_gets_retrieved_knowledge(home, monkeypatch):
    Knowledge(home).write("people/sam.md", {"type": "person", "title": "Sam"}, "Sam's birthday is 2026-11-14.",
                          "birthday", source="user")
    home.write("workers/helper.md", "---\ntype: worker\nmodel: test/model\n---\nHelp.\n")
    model = script(monkeypatch, {"main": [call("create_task", title="Plan Sam's birthday", worker="helper",
                                               brief="Plan something for Sam's birthday."), "Queued.", "[quiet]"],
                                 "helper": ["Booked dinner."]})

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            await a.send("Plan Sam's birthday.")
            await until(lambda: "helper" in model.seen)
            assert "2026-11-14" in model.seen["helper"][0][0]["content"]
            assert "# Relevant knowledge" in model.seen["main"][0][0]["content"]
        finally:
            await a.stop()
    run(go())
