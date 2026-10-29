"""Step 2 tests with a scripted model: tasks, workers, questions, wakes, routines, schedules."""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from daimon import agent as agent_mod
from daimon.agent import Agent
from daimon.gate import Decision
from daimon.home import Home
from daimon.runner import Runner
from daimon.schedule import next_run, now
from daimon.tasks import TaskStore

DEMO = str(Path(__file__).parent / "demo_server.py")


class Surface:
    def __init__(self):
        self.said, self.events, self.asked = [], [], []

    async def approve(self, tool, args, can_always, who="main"):
        self.asked.append((tool.name, who))
        return Decision(True)

    def event(self, kind, text):
        self.events.append((kind, text))

    def say(self, text):
        self.said.append(text)


def call(name, **args):
    return ("call", name, args)


class ScriptedModel:
    """Replaces Runner.complete. Each worker has a list of steps: a string (final reply) or call(...)."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.seen = {}

    async def __call__(self, runner, model, messages, tools, worker):
        self.seen.setdefault(worker, []).append(messages)
        step = self.scripts[worker].pop(0)
        if callable(step):  # computed at call time, e.g. to use a task id that doesn't exist yet
            step = step()
        if isinstance(step, tuple):
            n = sum(len(v) for v in self.scripts.values())
            tc = NS(id=f"c{n}-{step[1]}", function=NS(name=step[1], arguments=json.dumps(step[2])))
            msg = NS(content="", tool_calls=[tc])
        else:
            msg = NS(content=step, tool_calls=None)
        return NS(choices=[NS(message=msg)])


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.setattr(agent_mod, "TICK_SECONDS", 0.05)
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    h.write("workers/researcher.md", "---\ntype: worker\nmodel: test/model\nmcps: [demo]\n---\nResearch things.\n")
    h.write("routines/nightly.md", h.read("routines/nightly.md").replace("enabled: true", "enabled: false"))
    data = h.mcp()
    data["mcpServers"]["demo"] = {"command": sys.executable, "args": [DEMO]}
    h.write_mcp(data)
    return h


def script(monkeypatch, scripts):
    model = ScriptedModel(scripts)

    async def complete(self, model_id, messages, tools, worker):
        return await model(self, model_id, messages, tools, worker)
    monkeypatch.setattr(Runner, "complete", complete)
    return model


async def until(cond, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


def test_schedules():
    at = datetime(2026, 10, 25, 22, 0).astimezone()  # a Sunday
    assert next_run("daily 00:00", at).day == 26
    assert next_run("weekdays 08:30", at).weekday() == 0
    assert next_run("weekly sun 18:00", at).day == 1
    assert next_run("monthly 1 09:00", at).month == 11
    assert next_run("every 30m", at) == at + timedelta(minutes=30)
    for bad in ("hourly", "daily 25:00", "weekly funday 09:00"):
        with pytest.raises(ValueError):
            next_run(bad, at)


def test_task_roundtrip(tmp_path):
    store = TaskStore(tmp_path)
    t = store.create("Book dentist!", "Find a slot.", worker="scheduler", status="proposed", run_at="2026-11-01T09:00")
    store.update(t.id, log="looked at calendar", status="queued")
    store.set_result(t.id, "Tue 3pm")
    t2 = store.get(t.id)
    assert t2.id.endswith("book-dentist") and t2.status == "queued" and t2.worker == "scheduler"
    assert "looked at calendar" in t2.sections["Log"] and t2.sections["Result"].strip() == "Tue 3pm"
    assert store.create("Book dentist!", "again").id.endswith("-2")


def test_delegate_to_worker_and_report_back(home, monkeypatch):
    model = script(monkeypatch, {
        "main": [call("create_task", title="Oslo weather", brief="Get the weather in Oslo.", worker="researcher"),
                 "On it: the researcher is checking.",
                 "Oslo: rain until noon."],
        "researcher": [call("report_progress", note="checking"), call("demo__get_weather", city="Oslo"),
                       "Oslo: 18°C, light rain until noon."],
    })

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            assert await a.send("What's the weather in Oslo? Have the researcher check.") == "On it: the researcher is checking."
            await until(lambda: s.said)
            task = a.tasks.all()[0]
            assert task.status == "done" and "light rain" in task.sections["Result"]
            assert "checking" in task.sections["Log"]
            assert s.said == ["Oslo: rain until noon."]
            report = model.seen["main"][-1][-1]["content"]  # the worker's result reached main as an inbox event
            assert "[Worker report]" in report and "light rain" in report
            worker_system = model.seen["researcher"][0][0]["content"]
            assert "# Worker" in worker_system and "Research things." in worker_system
        finally:
            await a.stop()
    asyncio.run(go())


def test_worker_question_round_trip(home, monkeypatch):
    store = TaskStore(home.root / "tasks")
    model = script(monkeypatch, {
        "main": [call("create_task", title="Dentist", brief="Book the dentist.", worker="researcher"),
                 "Queued.",
                 lambda: call("answer_task", task_id=store.all()[0].id, answer="Tuesday"),
                 "[quiet] answered",
                 "Booked for Tuesday."],
        "researcher": [call("ask_main", question="Tuesday or Wednesday?"), "Booked Tuesday 3pm."],
    })

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            await a.send("Book the dentist.")
            await until(lambda: s.said)
            task_id = a.tasks.all()[0].id
            t = a.tasks.get(task_id)
            assert t.status == "done" and "Tuesday 3pm" in t.sections["Result"]
            assert s.said == ["Booked for Tuesday."]  # the [quiet] reply was not shown
            resumed = model.seen["researcher"][-1]
            assert any("Answer from the main agent: Tuesday" in str(m.get("content")) for m in resumed)
        finally:
            await a.stop()
    asyncio.run(go())


def test_wake(home, monkeypatch):
    soon = (now() - timedelta(minutes=1)).isoformat(timespec="minutes")
    model = script(monkeypatch, {
        "main": [call("schedule_wake", when=soon, note="Remind the user to stretch."), "I'll remind you.",
                 "Time to stretch!"],
    })

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            assert await a.send("Remind me to stretch in a bit.") == "I'll remind you."
            await until(lambda: s.said)
            assert s.said == ["Time to stretch!"]
            assert "[Wake-up you scheduled]" in model.seen["main"][-1][-1]["content"]
            assert a.tasks.all()[0].status == "done"
        finally:
            await a.stop()
    asyncio.run(go())


def test_routines_fire_once_and_reschedule(home, monkeypatch):
    model = script(monkeypatch, {"main": ["Grades look fine."], "researcher": ["Checked the news.", "[quiet]"]})
    home.write("routines/grades.md", "---\nschedule: daily 09:00\n---\nCheck the user's grades.\n")
    home.write("routines/news.md", "---\nschedule: daily 09:00\nworker: researcher\n---\nCheck the news.\n")
    past = (now() - timedelta(minutes=5)).isoformat()  # as if the agent was off when they were due
    home.cache.mkdir(exist_ok=True)
    (home.cache / "routines.json").write_text(json.dumps({
        "grades": {"schedule": "daily 09:00", "next": past}, "news": {"schedule": "daily 09:00", "next": past}}))

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            await until(lambda: any(t.meta.get("created_by") == "routine:news" and t.status == "done"
                                    for t in a.tasks.all()))
            await until(lambda: "Grades look fine." in s.said and len(model.seen["main"]) >= 2)
            assert "[Routine]" in model.seen["main"][0][-1]["content"]
            state = json.loads((home.cache / "routines.json").read_text())
            assert datetime.fromisoformat(state["grades"]["next"]) > now()
            assert len([t for t in a.tasks.all() if t.meta.get("created_by") == "routine:news"]) == 1
        finally:
            await a.stop()
    asyncio.run(go())


def test_restart_requeues_running(home):
    store = TaskStore(home.root / "tasks")
    t = store.create("x", "y", worker="researcher", run_at=(now() + timedelta(days=1)).isoformat())
    store.update(t.id, status="running")

    async def go():
        a = Agent(home, Surface())
        await a.start(scheduler=False)
        await a.stop()
    asyncio.run(go())
    assert store.get(t.id).status == "queued"


def test_wake_times(home):
    from daimon import tasktools
    store = TaskStore(home.root / "tasks")
    tools = {t.name: t for t in tasktools.main_tools(home, store, lambda: None)}
    out = asyncio.run(tools["schedule_wake"].run({"note": "stretch", "in_minutes": 5}))
    assert "Scheduled" in out
    run_at = datetime.fromisoformat(store.all()[0].meta["run_at"])
    assert timedelta(minutes=3) < run_at - now() < timedelta(minutes=6)
    with pytest.raises(ValueError, match="in the past"):
        asyncio.run(tools["schedule_wake"].run({"note": "x", "when": "2020-01-01T09:00"}))
