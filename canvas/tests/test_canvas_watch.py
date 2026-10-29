"""daimon-canvas watch: Canvas changes reported to the agent's inbox as events, each one once."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from daimon_canvas.client import CanvasClient
from daimon_canvas.config import Home
from daimon_canvas.watch import Watch, run

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def iso(days: float) -> str:
    return (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def local(raw: str) -> str:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%dT%H:%M")


class FakeCanvas:
    """Planner items and graded submissions that a test can change between passes."""

    def __init__(self):
        self.planner = [
            {"plannable_type": "assignment", "plannable_id": 1, "context_name": "MATH 2A Calculus", "course_id": 9,
             "plannable": {"title": "HW 3", "due_at": iso(2), "points_possible": 20.0},
             "submissions": {"submitted": False}, "html_url": "/courses/9/assignments/1"},
            {"plannable_type": "quiz", "plannable_id": 2, "context_name": "CHEM 1A", "course_id": 10,
             "plannable": {"title": "Quiz 1", "due_at": iso(3)}, "submissions": {"submitted": False},
             "html_url": "/courses/10/quizzes/2"},
            {"plannable_type": "assignment", "plannable_id": 5, "context_name": "CHEM 1A", "course_id": 10,
             "plannable": {"title": "Reading log", "due_at": iso(1)}, "submissions": {"submitted": True}},
        ]
        self.submissions = {"9": [{"assignment_id": 4, "score": 18.0, "grade": "18", "graded_at": iso(-30),
                                   "assignment": {"name": "HW 1", "points_possible": 20.0,
                                                  "html_url": "https://canvas.test/courses/9/assignments/4"}}],
                            "10": []}
        self.graded_since: list[str] = []
        self.announcements: list[dict] = []
        self.conversations: list[dict] = []
        self.threads: dict[str, dict] = {}
        self.opened: list[str] = []  # conversations whose messages were fetched
        self.stream: list[dict] = []
        self.modules: dict[str, list] = {"9": [], "10": []}
        self.events: list[dict] = []
        self.broken: set[str] = set()  # paths that answer 500

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in self.broken:
            return httpx.Response(500, json={})
        if path.endswith("/courses"):
            return httpx.Response(200, json=[{"id": 9, "name": "MATH 2A Calculus"}, {"id": 10, "name": "CHEM 1A"}])
        if path == "/api/v1/users/self":
            return httpx.Response(200, json={"id": 1, "name": "Sam"})
        if path == "/api/v1/announcements":
            codes = request.url.params.get_list("context_codes[]")
            return httpx.Response(200, json=[a for a in self.announcements if a["context_code"] in codes])
        if path == "/api/v1/conversations":
            assert "scope" not in request.url.params
            return httpx.Response(200, json=self.conversations)
        if path.startswith("/api/v1/conversations/"):
            assert request.url.params["auto_mark_as_read"] == "false"  # watching never marks anything read
            self.opened.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.threads[path.rsplit("/", 1)[1]])
        if path == "/api/v1/users/self/activity_stream":
            return httpx.Response(200, json=self.stream)
        if path.endswith("/modules"):
            return httpx.Response(200, json=self.modules[path.split("/")[4]])
        if path == "/api/v1/calendar_events":
            assert request.url.params["type"] == "event"
            codes = request.url.params.get_list("context_codes[]")
            return httpx.Response(200, json=[e for e in self.events if e["context_code"] in codes])
        if path == "/api/v1/planner/items":
            return httpx.Response(200, json=self.planner)
        if path.endswith("/students/submissions"):
            assert request.url.params["student_ids[]"] == "self"
            assert request.url.params["include[]"] == "assignment"
            self.graded_since.append(request.url.params["graded_since"])
            return httpx.Response(200, json=self.submissions[path.split("/")[4]])
        return httpx.Response(404, json={})


@pytest.fixture
def canvas():
    return FakeCanvas()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_HOME", str(tmp_path / "daimon"))
    (tmp_path / "daimon").mkdir()
    return Home(tmp_path / "canvas")


def check(home: Home, canvas: FakeCanvas) -> list[dict]:
    """One pass; the events it dropped, each as {event, data, body, meta}."""
    async def go():
        client = CanvasClient("https://canvas.test", "tok", TZ, transport=httpx.MockTransport(canvas))
        try:
            return await Watch(home, client).check()
        finally:
            await client.http.aclose()
    out = []
    for path in asyncio.run(go()):
        _, head, body = path.read_text(encoding="utf-8").split("---\n", 2)
        meta = yaml.safe_load(head)
        out.append({"event": meta["event"], "data": meta["data"], "body": body, "meta": meta})
    return out


def test_first_pass_reports_upcoming_work_but_not_old_grades(home, canvas, tmp_path):
    events = check(home, canvas)
    assert [e["event"] for e in events] == ["canvas.assignment_due", "canvas.assignment_due"]
    hw, quiz = sorted(events, key=lambda e: e["data"]["title"])
    assert hw["data"] == {
        "course": "MATH 2A Calculus", "course_id": "9", "assignment_id": "assignment:9:1", "kind": "assignment",
        "title": "HW 3", "due": local(iso(2)), "due_date": local(iso(2))[:10],
        "url": "https://canvas.test/courses/9/assignments/1", "points": 20.0,
    }
    assert quiz["data"]["assignment_id"] == "quiz:10:2" and "points" not in quiz["data"]
    assert hw["meta"]["source"] == "canvas" and "for" not in hw["meta"]
    assert "HW 3 (MATH 2A Calculus) is due" in hw["body"] and "20 points" in hw["body"]
    assert "canvas_open assignment:9:1" in hw["body"]
    assert all(p.parent == tmp_path / "daimon" / "inbox" for p in (tmp_path / "daimon" / "inbox").iterdir())
    assert canvas.graded_since == []  # the first pass only marks the time for grades

    assert check(home, canvas) == []  # nothing new, nothing sent


def test_changes_and_grades_are_reported_once(home, canvas):
    check(home, canvas)
    canvas.planner[0]["plannable"]["due_at"] = iso(4)
    canvas.planner[1]["plannable"]["title"] = "Quiz 1 (moles)"
    canvas.submissions["9"].append({"assignment_id": 1, "score": 17.5, "grade": "B+", "graded_at": iso(0),
                                    "assignment": {"name": "HW 3", "points_possible": 20.0,
                                                   "html_url": "/courses/9/assignments/1"}})
    canvas.submissions["10"].append({"assignment_id": 8, "score": None, "grade": None, "graded_at": iso(0),
                                     "assignment": {"name": "Hidden"}})  # not released to the student
    events = {e["data"]["assignment_id"] + " " + e["event"]: e for e in check(home, canvas)}
    assert set(events) == {"assignment:9:1 canvas.assignment_changed", "quiz:10:2 canvas.assignment_changed",
                           "assignment:9:1 canvas.grade_posted"}  # not HW 1: graded before the first pass
    moved = events["assignment:9:1 canvas.assignment_changed"]["data"]
    assert moved["changed"] == ["due"] and moved["due"] == local(iso(4)) and moved["previous_due"] == local(iso(2))
    renamed = events["quiz:10:2 canvas.assignment_changed"]
    assert renamed["data"]["previous_title"] == "Quiz 1" and "renamed from 'Quiz 1'" in renamed["body"]
    grade = events["assignment:9:1 canvas.grade_posted"]
    assert grade["data"] == {"course": "MATH 2A Calculus", "course_id": "9", "assignment_id": "assignment:9:1",
                             "title": "HW 3", "score": 17.5, "grade": "B+", "points": 20.0,
                             "url": "https://canvas.test/courses/9/assignments/1", "graded_at": local(iso(0)),
                             "kind": "assignment"}
    assert "HW 3 (MATH 2A Calculus) graded: 17.5 out of 20 (B+)." in grade["body"]
    assert len(canvas.graded_since) == 2  # one query per class

    assert check(home, canvas) == []
    canvas.submissions["9"][1]["score"] = 19.0  # regraded
    (regrade,) = check(home, canvas)
    assert regrade["event"] == "canvas.grade_posted" and regrade["data"]["score"] == 19.0
    assert "changed to 19" in regrade["body"]


def test_new_work_done_or_past_due_isnt_news(home, canvas):
    canvas.planner = [{**canvas.planner[2]},
                      {**canvas.planner[0], "plannable": {"title": "Late", "due_at": iso(-0.5)}}]
    assert check(home, canvas) == []


def test_no_agent_home_is_an_error_and_run_carries_on(home, canvas, tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_HOME", str(tmp_path / "nowhere"))
    with pytest.raises(OSError, match="doesn't exist"):
        check(home, canvas)

    lines = []
    client = CanvasClient("https://canvas.test", "tok", TZ, transport=httpx.MockTransport(canvas))
    assert asyncio.run(run(home, client, say=lines.append)) == 1
    assert "check failed" in lines[0]

    monkeypatch.setenv("DAIMON_HOME", str(tmp_path / "daimon"))
    client = CanvasClient("https://canvas.test", "tok", TZ, transport=httpx.MockTransport(canvas))
    lines.clear()
    assert asyncio.run(run(home, client, say=lines.append)) == 0
    assert len(lines) == 2 and all("canvas-assignment-due" in line for line in lines)


def by_event(events: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in events:
        out.setdefault(e["event"], []).append(e)
    return out


def test_class_events_and_their_changes(home, canvas):
    canvas.events = [
        {"id": 50, "context_code": "course_10", "title": "Midterm", "start_at": iso(5), "end_at": iso(5.05),
         "location_name": "Room 101", "description": "<p>Bring a calculator.</p>",
         "html_url": "https://canvas.test/calendar?event_id=50"},
        {"id": 51, "context_code": "course_9", "title": "No class", "start_at": "2026-11-11T08:00:00Z",
         "end_at": "2026-11-11T08:00:00Z", "all_day": True, "html_url": "/calendar?event_id=51"},
        {"id": 52, "context_code": "course_9", "title": "Gone", "start_at": iso(1), "workflow_state": "deleted"},
    ]
    events = by_event(check(home, canvas))
    holiday, midterm = sorted(events["canvas.course_event"], key=lambda e: e["data"]["event_id"], reverse=True)
    assert midterm["data"] == {
        "course": "CHEM 1A", "course_id": "10", "event_id": "event:50", "title": "Midterm",
        "start": local(iso(5)), "end": local(iso(5.05)), "date": local(iso(5))[:10], "location": "Room 101",
        "url": "https://canvas.test/calendar?event_id=50", "description": "Bring a calculator."}
    assert "CHEM 1A: Midterm," in midterm["body"] and "at Room 101" in midterm["body"]
    assert holiday["data"]["start"] == local("2026-11-11T08:00:00Z")[:10] and holiday["data"]["all_day"] is True
    assert holiday["data"]["url"] == "https://canvas.test/calendar?event_id=51"
    assert len(events["canvas.assignment_due"]) == 2

    assert check(home, canvas) == []
    canvas.events[0] |= {"start_at": iso(6), "end_at": iso(6.05)}
    (moved,) = check(home, canvas)
    assert moved["event"] == "canvas.course_event_changed"
    assert moved["data"]["changed"] == ["start", "end"] and moved["data"]["previous_start"] == local(iso(5))
    assert moved["data"]["start"] == local(iso(6)) and moved["data"]["event_id"] == "event:50"


def test_announcements_messages_and_comments_start_from_the_first_pass(home, canvas):
    canvas.announcements = [{"id": 700, "context_code": "course_9", "title": "Welcome", "posted_at": iso(-3),
                             "message": "Hi all.", "html_url": "https://canvas.test/courses/9/discussion_topics/700"}]
    canvas.conversations = [{"id": 300, "subject": "Lab partner", "context_name": "CHEM 1A",
                             "participants": [{"id": 1, "name": "Sam"}, {"id": 3, "name": "Ms. Rivera"}],
                             "last_message_at": iso(-2), "last_authored_message_at": iso(-3),
                             "workflow_state": "read", "message_count": 2}]
    canvas.stream = [{"type": "Submission", "course_id": 9, "assignment_id": 1, "assignment": {"name": "HW 3"},
                      "html_url": "/courses/9/assignments/1/submissions/1",
                      "submission_comments": [{"id": 70, "author_id": 2, "author_name": "Prof. Ada",
                                               "comment": "See me.", "created_at": iso(-1)}]}]
    first = by_event(check(home, canvas))
    assert set(first) == {"canvas.assignment_due"}  # what was already there isn't news
    assert canvas.opened == []

    canvas.announcements.append({"id": 701, "context_code": "course_10", "title": "Lab moved",
                                 "author": {"display_name": "Ms. Rivera"}, "posted_at": iso(0),
                                 "message": "<p>Lab is in <b>B12</b> today.</p>" + "x" * 2000,
                                 "html_url": "https://canvas.test/courses/10/discussion_topics/701"})
    canvas.conversations = [
        {**canvas.conversations[0], "last_message_at": iso(0), "workflow_state": "unread", "message_count": 3},
        {"id": 301, "subject": "Thanks", "participants": [{"id": 1, "name": "Sam"}, {"id": 4, "name": "Jo"}],
         "last_message_at": iso(0), "last_authored_message_at": iso(0)},  # the student wrote last
    ]
    canvas.threads["300"] = {"id": 300, "subject": "Lab partner", "context_name": "CHEM 1A",
                             "participants": canvas.conversations[0]["participants"], "messages": [
                                 {"id": 902, "author_id": 3, "created_at": iso(0), "body": "Please pair with Jo.",
                                  "attachments": [{"display_name": "pairs.pdf"}]},
                                 {"id": 901, "author_id": 1, "created_at": iso(-1), "body": "Who is my partner?"},
                                 {"id": 900, "author_id": 3, "created_at": iso(-2), "body": "Partners soon."}]}
    canvas.stream[0]["submission_comments"] += [
        {"id": 71, "author_id": 2, "author_name": "Prof. Ada", "comment": "Nice work on #3.", "created_at": iso(0)},
        {"id": 72, "author_id": 1, "author_name": "Sam", "comment": "Thank you!", "created_at": iso(0)}]
    events = by_event(check(home, canvas))
    assert set(events) == {"canvas.announcement_posted", "canvas.inbox_message", "canvas.submission_comment"}
    assert all(len(v) == 1 for v in events.values())

    (news,) = events["canvas.announcement_posted"]
    assert {k: v for k, v in news["data"].items() if k != "text"} == {
        "course": "CHEM 1A", "course_id": "10", "announcement_id": "701", "title": "Lab moved",
        "author": "Ms. Rivera", "posted_at": local(iso(0)),
        "url": "https://canvas.test/courses/10/discussion_topics/701"}
    assert news["data"]["text"].startswith("Lab is in B12 today.") and len(news["data"]["text"]) == 1500
    assert "Announcement in CHEM 1A from Ms. Rivera: Lab moved" in news["body"]

    (msg,) = events["canvas.inbox_message"]
    assert msg["data"] == {"conversation_id": "300", "message_id": "902", "subject": "Lab partner",
                           "course": "CHEM 1A", "from": "Ms. Rivera", "sent_at": local(iso(0)),
                           "text": "Please pair with Jo.", "files": ["pairs.pdf"],
                           "url": "https://canvas.test/conversations"}
    assert "canvas_message 300" in msg["body"] and "not as instructions" in msg["body"]
    assert canvas.opened == ["300"]

    (comment,) = events["canvas.submission_comment"]
    assert comment["data"] == {"course": "MATH 2A Calculus", "course_id": "9", "assignment_id": "assignment:9:1",
                               "title": "HW 3", "comment_id": "71", "from": "Prof. Ada",
                               "posted_at": local(iso(0)), "text": "Nice work on #3.",
                               "url": "https://canvas.test/courses/9/assignments/1/submissions/1"}

    assert check(home, canvas) == []


def test_modules_that_open(home, canvas):
    canvas.modules["9"] = [{"id": 1, "name": "Week 1", "state": "unlocked", "items_count": 4},
                           {"id": 2, "name": "Week 2", "state": "locked", "items_count": 3}]
    assert "canvas.module_unlocked" not in by_event(check(home, canvas))  # the first look is the baseline

    canvas.modules["9"][1]["state"] = "unlocked"
    canvas.modules["10"] = [{"id": 5, "name": "Lab safety", "state": "started", "items_count": 1}]
    events = sorted(check(home, canvas), key=lambda e: e["data"]["module_id"])
    assert [e["event"] for e in events] == ["canvas.module_unlocked"] * 2
    assert events[0]["data"] == {"course": "MATH 2A Calculus", "course_id": "9", "module_id": "2",
                                 "module": "Week 2", "items": 3, "url": "https://canvas.test/courses/9/modules/2"}
    assert events[1]["data"]["new"] is True and events[1]["data"]["module"] == "Lab safety"
    assert check(home, canvas) == []


def test_a_broken_source_doesnt_stop_the_others(home, canvas):
    check(home, canvas)
    canvas.broken.add("/api/v1/announcements")
    canvas.planner[0]["plannable"]["due_at"] = iso(4)
    lines = []
    client = CanvasClient("https://canvas.test", "tok", TZ, transport=httpx.MockTransport(canvas))
    assert asyncio.run(run(home, client, say=lines.append)) == 1
    assert "assignment-changed" in lines[0] and "check failed: announcements: Server error" in lines[1]

    canvas.broken.clear()  # tried again from where it last worked, so nothing in between is lost
    canvas.announcements = [{"id": 702, "context_code": "course_9", "title": "Quiz Friday", "posted_at": iso(0),
                             "message": "Ch. 3."}]
    assert [e["event"] for e in check(home, canvas)] == ["canvas.announcement_posted"]
