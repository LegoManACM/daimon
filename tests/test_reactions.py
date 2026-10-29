"""Reactions: events from add-ons turned into tool calls with no model in between."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from daimon import inbox, reactions, selftools
from daimon.agent import Agent
from daimon.home import Home
from daimon.mcp_hub import McpHub

from test_agent import DEMO, Surface

# What the messages add-on drops when its `plans` watcher fires.
PLANS = {"title": "Dinner with Sam", "start": "2026-10-30T19:00", "end": "", "location": "Luigi's",
         "who": "Sam Lee", "watcher": "plans", "chat_id": "c1", "chat": "Sam Lee", "from": "Sam Lee",
         "summary": "Sam suggests dinner at Luigi's Friday at 7.", "message_ids": ["m1"]}

TO_DEMO = """---
on: messages.watch_hit
when: {watcher: plans}
call: demo__send_message
args:
  to: "{who}"
  text: "{title} at {start} ({location?}) {end?}"
then: notify
---
Plans from texts, mechanically passed on.
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    data = h.mcp()
    data["mcpServers"]["demo"] = {"command": sys.executable, "args": [DEMO]}
    h.write_mcp(data)
    return h


def test_matching_and_filling():
    r = reactions.parse("x", TO_DEMO)
    hit = reactions.Event("messages.watch_hit", PLANS)
    assert r.matches(hit)
    assert not r.matches(reactions.Event("messages.watch_hit", {**PLANS, "watcher": "changes"}))
    assert not r.matches(reactions.Event("calendar.event_changed", PLANS))
    assert r.render(PLANS) == {"to": "Sam Lee", "text": "Dinner with Sam at 2026-10-30T19:00 (Luigi's) "}

    r = reactions.parse("y", "---\non: [messages.*]\nwhen: {who: 'sam*', location: '*'}\ncall: t\n"
                             "args: {ids: '{message_ids}', first: '{message_ids.0}', end: '{end?}', n: 3}\n---\n")
    assert r.matches(reactions.Event("messages.watch_hit", PLANS))
    assert r.render(PLANS) == {"ids": ["m1"], "first": "m1", "n": 3}  # a whole-field arg keeps its type
    with pytest.raises(ValueError, match="no title"):
        reactions.parse("z", "---\non: a\ncall: t\nargs: {t: '{title}'}\n---\n").render({"title": ""})
    with pytest.raises(ValueError, match="`call` is missing"):
        reactions.parse("bad", "---\non: a\n---\n")


def test_inbox_event_calls_tool_without_a_model(home, monkeypatch):
    """The messages → calendar shape: a watcher hit lands in the inbox and a reaction calls the tool directly."""
    home.write("reactions/plans.md", TO_DEMO)
    monkeypatch.setattr(inbox, "SETTLE_SECONDS", 0)
    outbox = Path(DEMO).parent / "demo_outbox.txt"
    before = outbox.read_text(encoding="utf-8") if outbox.exists() else ""
    inbox.drop(home, "Text watcher `plans` fired.", source="sms-watch", event="messages.watch_hit", data=PLANS,
               **{"for": "main"})
    inbox.drop(home, "Something else.", source="email", **{"for": "main"})

    async def go():
        s = Surface()
        a = Agent(home, s)  # never started: no model runs, so nothing here can call one
        a._drops()
        while a._reacting:  # reaction.done may start more
            await asyncio.gather(*list(a._reacting))
        await a.hub.close()
        return a, s
    a, s = asyncio.run(go())
    assert s.asked == [("demo__send_message", "reaction:plans")]  # still behind its consent level
    assert any(k == "notify" and "reaction:plans: Sent to Sam Lee" in t for k, t in s.events)
    assert "to Sam Lee: Dinner with Sam at 2026-10-30T19:00 (Luigi's)" in outbox.read_text(encoding="utf-8")[len(before):]
    assert a.inbox.qsize() == 1  # only the email went to main; the claimed hit didn't
    log = reactions.recent(home, 10)
    assert sorted((r["event"], r["handled"]) for r in log) == [
        ("inbox.email", []), ("messages.watch_hit", ["plans"]), ("reaction.done", [])]


HOMEWORK = """---
on: canvas.assignment_due
call: calendar__calendar_add_event
args:
  title: "{title} ({course})"
  start: "{due}"
  notes: "{url?}"
  source: "canvas:{assignment_id}"
then: notify
---
Homework goes on the calendar; a moved due date or a repeat updates the same event.
"""


def test_canvas_assignment_due_goes_on_the_calendar(home, tmp_path, monkeypatch):
    """Canvas → calendar with neither knowing the other: the watcher's event, a reaction, the calendar's tool."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    from daimon_canvas.client import Assignment, CanvasClient
    from daimon_canvas.config import Home as CanvasHome
    from daimon_canvas.watch import Watch

    added = tmp_path / "calendar.jsonl"
    data = home.mcp()
    data["mcpServers"]["calendar"] = {"command": sys.executable,
                                      "args": [str(Path(__file__).parent / "calendar_server.py"), str(added)]}
    home.write_mcp(data)
    home.write("reactions/homework.md", HOMEWORK)
    monkeypatch.setattr(inbox, "SETTLE_SECONDS", 0)
    monkeypatch.setenv("DAIMON_HOME", str(home.root))

    tz = ZoneInfo("America/Los_Angeles")
    due = (datetime.now(timezone.utc) + timedelta(days=2)).astimezone(tz).replace(hour=23, minute=59, second=0,
                                                                                   microsecond=0)
    hw = Assignment(id="assignment:1", title="HW 3", course="MATH 2A", due=due, kind="assignment", submitted=False,
                    missing=False, url="https://canvas.test/courses/9/assignments/1", course_id="9", item_id="1",
                    points=20.0)

    async def emit():  # what one `daimon-canvas watch` pass drops for a newly due assignment
        client = CanvasClient("https://canvas.test", "tok", tz)
        try:
            return Watch(CanvasHome(tmp_path / "canvas"), client).assignment(hw, {}, datetime.now(tz))
        finally:
            await client.http.aclose()
    (path,) = asyncio.run(emit())
    assert path.parent == home.root / "inbox"

    async def go():
        s = Surface()
        a = Agent(home, s)
        a._drops()
        while a._reacting:
            await asyncio.gather(*list(a._reacting))
        await a.hub.close()
        return a, s
    a, s = asyncio.run(go())
    assert s.asked == [("calendar__calendar_add_event", "reaction:homework")]
    assert json.loads(added.read_text(encoding="utf-8")) == {
        "title": "HW 3 (MATH 2A)", "start": due.strftime("%Y-%m-%dT%H:%M"),
        "notes": "https://canvas.test/courses/9/assignments/1", "source": "canvas:assignment:9:1"}
    assert any(k == "notify" and "reaction:homework: Added: HW 3 (MATH 2A)" in t for k, t in s.events)
    assert a.inbox.empty()  # claimed by the reaction: main never saw it
    assert [(r["event"], r["handled"]) for r in reactions.recent(home, 10, "canvas.*")] ==         [("canvas.assignment_due", ["homework"])]


def test_failures_go_to_main_and_chains_run(home):
    home.write("reactions/needs-place.md",
               "---\non: messages.watch_hit\ncall: demo__get_weather\nargs: {city: '{place}'}\n---\n")
    home.write("reactions/weather.md",
               "---\non: routine.morning\ncall: demo__get_weather\nargs: {city: Paris}\nthen: quiet\n---\n")
    home.write("reactions/after.md",
               "---\non: reaction.done\nwhen: {reaction: weather}\ncall: demo__get_weather\n"
               "args: {city: '{result}'}\nthen: tell\n---\n")
    home.write("routines/morning.md", "---\nschedule: every 1m\nworker: none\n---\n")

    async def go():
        s = Surface()
        a = Agent(home, s)
        a.react(reactions.Event("messages.watch_hit", PLANS, "the hit"))
        a._fire("morning", {"worker": "none"}, "")
        while a._reacting:
            await asyncio.gather(*list(a._reacting))
        await a.hub.close()
        return a, s
    a, s = asyncio.run(go())
    item = a.inbox.get_nowait()
    assert "needs-place" in item.text and "no place" in item.text and "the hit" in item.text
    assert a.inbox.empty()  # worker: none put nothing in front of main
    assert s.said and s.said[0].startswith("Paris: 18°C")  # routine → weather → reaction.done → after


def test_agent_cant_save_a_broken_reaction(home):
    tools = {t.name: t for t in selftools.build(home, McpHub(home), Surface())}
    with pytest.raises(ValueError, match="`on` is missing"):
        tools["write_agent_file"].level({"path": "reactions/x.md", "content": "---\ncall: t\n---\n", "reason": "r"})
    assert tools["write_agent_file"].level({"path": "reactions/x.md", "content": TO_DEMO, "reason": "r"}) == "auto"
