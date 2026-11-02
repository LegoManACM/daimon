"""The web page's editing of tasks and the schedule: the Edit form, routines, and Add."""
import asyncio
from datetime import timedelta

from daimon.home import split_frontmatter
from daimon.schedule import now
from test_agent import home  # noqa: F401 - fixture
from test_web import W, client, make


def soon(**kw):
    return (now() + timedelta(**(kw or {"hours": 3}))).strftime("%Y-%m-%dT%H:%M")


def run(coro):
    return asyncio.run(coro)


def setup(home, monkeypatch):  # noqa: F811
    ch, agent, _ = make(home, monkeypatch)
    return ch, agent, client(ch)


def post(c, path, body):
    return c.post(path, json=body, headers=W)


def test_edit_every_field(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    t = agent.tasks.create("Old title", "Old brief", worker="main", run_at=soon(hours=1))
    agent._tick.clear()

    async def go():
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "title": "New title", "brief": "New brief",
                                                  "worker": "researcher", "run_at": soon(hours=5), "due": "2026-12-01"})
        assert r.status_code == 200, r.text
        got = r.json()
        assert got["title"] == "New title" and got["worker"] == "researcher" and got["due"] == "2026-12-01"
        assert got["sections"]["Brief"] == "New brief" and got["run_at"] != t.meta["run_at"]
        assert "edited by the user on the web page" in got["sections"]["Log"]
        assert agent._tick.is_set()
        # clearing the time and the due date
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "run_at": "", "due": ""})
        assert "run_at" not in r.json() and "due" not in r.json()
        # nothing changed: no log line added
        before = r.json()["sections"]["Log"]
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "title": "New title"})
        assert r.json()["sections"]["Log"] == before
    run(go())
    assert agent.tasks.get(t.id).status == "queued"


def test_edit_project(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    agent.projects.create("thesis", "Thesis", "Write it")
    t = agent.tasks.create("T", "B", worker="main")

    async def go():
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "project": "thesis"})
        assert r.json()["project"] == "thesis"
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "project": "nope"})
        assert r.status_code == 400 and "nope" in r.json()["error"]
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "project": ""})
        assert "project" not in r.json()
        assert "thesis" in (await c.get("/api/tasks")).json().get("projects", [])
    run(go())


def test_rejected_edits(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    t = agent.tasks.create("T", "B", worker="main")
    running = agent.tasks.create("R", "RB", worker="main")
    agent.tasks.update(running.id, status="running")

    async def err(task, **f):
        r = await post(c, f"/api/task/{task.id}", {"action": "edit", **f})
        assert r.status_code == 400, r.text
        return r.json()["error"]

    async def go():
        assert "worker" in await err(t, worker="ghost")
        assert "valid" in await err(t, run_at="tomorrow-ish")
        assert "valid" in await err(t, due="31st of never")
        assert "passed" in await err(t, run_at="2020-01-01T09:00")
        assert "title" in await err(t, title="   ")
        assert "brief" in await err(running, brief="different")
        assert "worker" in await err(running, worker="researcher")
        # a running task can still change its title, and resending the same brief is fine
        r = await post(c, f"/api/task/{running.id}", {"action": "edit", "title": "Renamed", "brief": "RB", "worker": "main"})
        assert r.status_code == 200 and r.json()["title"] == "Renamed" and r.json()["status"] == "running"
    run(go())
    assert agent.tasks.get(t.id).worker == "main"


def test_closed_task_edit_then_run_again(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    t = agent.tasks.create("T", "old", worker="main")
    agent.tasks.update(t.id, status="done")

    async def go():
        r = await post(c, f"/api/task/{t.id}", {"action": "edit", "brief": "new brief"})
        assert r.json()["status"] == "done" and r.json()["sections"]["Brief"] == "new brief"
        r = await post(c, f"/api/task/{t.id}", {"action": "status", "status": "queued"})
        assert r.json()["status"] == "queued" and r.json()["sections"]["Brief"] == "new brief"
    run(go())


def test_add_reminder_matches_agent_tool(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    tool = next(x for x in agent.main_extra() if x.name == "schedule_wake")
    when = soon()

    async def go():
        await tool.run({"note": "Call the dentist\nbring the card", "when": when})
        r = await post(c, "/api/schedule/add", {"kind": "reminder", "text": "Call the dentist\nbring the card", "when": when})
        assert r.status_code == 200, r.text
        return r.json()["id"]
    mine = agent.tasks.get(run(go()))
    theirs = next(t for t in agent.tasks.all() if t.meta.get("created_by") == "agent")
    for key in ("title", "worker", "status", "run_at", "type"):
        assert mine.meta[key] == theirs.meta[key], key
    assert mine.sections["Brief"].strip() == theirs.sections["Brief"].strip() and mine.meta["created_by"] == "user (web)"

    async def bad():
        for b in ({"kind": "reminder", "text": "", "when": when}, {"kind": "reminder", "text": "x", "when": ""},
                  {"kind": "reminder", "text": "x", "when": "2020-01-01T09:00"}, {"kind": "nope"}):
            assert (await post(c, "/api/schedule/add", b)).status_code == 400, b
    run(bad())


def test_add_scheduled_task(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)

    async def go():
        r = await post(c, "/api/schedule/add", {"kind": "task", "title": "Find flights", "brief": "Boston to SFO",
                                                 "worker": "researcher", "when": soon()})
        assert r.status_code == 200, r.text
        t = agent.tasks.get(r.json()["id"])
        assert (t.worker, t.status, t.sections["Brief"].strip()) == ("researcher", "queued", "Boston to SFO") and t.meta["run_at"]
        for b in ({"title": "", "when": soon()}, {"title": "x", "when": ""}, {"title": "x", "when": soon(), "worker": "ghost"}):
            assert (await post(c, "/api/schedule/add", {"kind": "task", **b})).status_code == 400, b
        items = (await c.get("/api/schedule")).json()["items"]
        assert any(i.get("id") == t.id and i["kind"] == "task" for i in items)
    run(go())


def test_routine_create_edit_enable_delete(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    path = home.root / "routines" / "weekly-review.md"

    async def go():
        r = await post(c, "/api/schedule/add", {"kind": "routine", "name": "weekly-review", "schedule": "weekly sun 18:00",
                                                 "worker": "main", "body": "Review my week."})
        assert r.status_code == 200, r.text
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        assert meta == {"schedule": "weekly sun 18:00", "worker": "main", "enabled": True} and body.strip() == "Review my week."
        assert "user (web): routine weekly-review added" in home.history(3)
        assert (await post(c, "/api/schedule/add", {"kind": "routine", "name": "weekly-review", "schedule": "daily 08:00",
                                                     "body": "x"})).status_code == 400  # exists

        # a hand-written extra key and comment survive an edit
        path.write_text(path.read_text(encoding="utf-8").replace("enabled: true", "enabled: true\nproject: thesis  # keep"), encoding="utf-8")
        r = await post(c, "/api/routine/weekly-review", {"action": "save", "schedule": "daily 07:30", "worker": "researcher",
                                                          "enabled": False, "body": "Do it differently."})
        assert r.status_code == 200 and r.json()["commit"], r.text
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
        assert meta["schedule"] == "daily 07:30" and meta["worker"] == "researcher" and meta["enabled"] is False
        assert meta["project"] == "thesis" and "# keep" in text and body.strip() == "Do it differently."
        assert "routine weekly-review changed" in home.history(3)
        got = (await c.get("/api/routine/weekly-review")).json()
        assert got["enabled"] is False and got["words"] == "Every day at 7:30 am" and len(got["runs"]) == 3

        # just the switch back on
        r = await post(c, "/api/routine/weekly-review", {"action": "save", "enabled": True})
        assert r.json()["enabled"] is True and split_frontmatter(path.read_text(encoding="utf-8"))[0]["enabled"] is True
        assert any(i["name"] == "weekly-review" for i in (await c.get("/api/schedule")).json()["items"])

        r = await post(c, "/api/routine/weekly-review", {"action": "delete"})
        assert r.status_code == 200 and not path.exists() and "routine weekly-review deleted" in home.history(3)
        assert (await post(c, "/api/routine/nightly", {"action": "delete"})).status_code == 400
        assert (await post(c, "/api/routine/ghost", {"action": "save", "enabled": True})).status_code == 400
    run(go())


def test_routine_validation(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)

    async def go():
        base = {"kind": "routine", "name": "r1", "schedule": "daily 08:00", "worker": "main", "body": "Do it."}
        for change, word in ((dict(schedule="whenever"), "schedule"), (dict(schedule="daily 25:00"), "time"),
                             (dict(schedule=""), "schedule"), (dict(name="Bad Name"), "name"), (dict(name="../x"), "name"),
                             (dict(worker="ghost"), "ghost"), (dict(body=""), "instructions")):
            r = await post(c, "/api/schedule/add", {**base, **change})
            assert r.status_code == 400 and word in r.json()["error"].lower(), (change, r.text)
        assert not (home.root / "routines" / "r1.md").exists()
        r = await post(c, "/api/schedule/add", {**base, "worker": "none", "body": ""})  # none: a reaction does the work
        assert r.status_code == 200
    run(go())


def test_schedule_preview(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)

    async def go():
        r = (await c.get("/api/schedule/preview", params={"schedule": "weekly mon,wed,fri 09:00"})).json()
        assert r["ok"] and r["words"] == "Every Monday, Wednesday and Friday at 9:00 am" and len(r["runs"]) == 3
        r = (await c.get("/api/schedule/preview", params={"schedule": "every 2h"})).json()
        assert r["ok"] and r["words"].startswith("Every 2 hours")
        r = (await c.get("/api/schedule/preview", params={"schedule": "monthly 1 18:00"})).json()
        assert r["ok"] and "day 1 of every month at 6:00 pm" in r["words"]
        r = (await c.get("/api/schedule/preview", params={"schedule": "sometimes"})).json()
        assert not r["ok"] and r["error"]
    run(go())


def test_run_routine_now(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)
    home.write("routines/hello.md", "---\nschedule: daily 08:00\nworker: main\nenabled: true\n---\nSay hello.\n")

    async def go():
        r = await post(c, "/api/routine/hello", {"action": "run"})
        assert r.status_code == 200, r.text
        item = agent.inbox.get_nowait()
        assert item.kind == "routine" and "Say hello." in item.text
    run(go())


def test_requires_header_and_sign_in(home, monkeypatch):  # noqa: F811
    ch, agent, c = setup(home, monkeypatch)

    async def go():
        assert (await c.post("/api/schedule/add", json={"kind": "reminder"})).status_code == 403
        assert (await c.post("/api/routine/nightly", json={"action": "run"})).status_code == 403
        out = client(ch, signed_in=False)
        assert (await out.get("/api/routine/nightly")).status_code == 401
    run(go())
