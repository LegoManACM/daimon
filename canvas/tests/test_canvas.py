"""The Canvas MCP server against a fake Canvas."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp import Client

from daimon_canvas.client import CanvasClient, CanvasError, parse_ref
from daimon_canvas.config import Home
from daimon_canvas.mcp_server import build

TZ = ZoneInfo("America/Los_Angeles")
HANDOUT = b"Plain handout: show all work."
FORMULAS = b"d/dx sin x = cos x"
LECTURE = b"Limits and continuity."
HW3 = {
    "name": "HW 3", "due_at": "2026-09-25T06:59:00Z", "points_possible": 20.0,
    "submission_types": ["online_upload"], "allowed_extensions": ["pdf"],
    "html_url": "https://canvas.test/courses/9/assignments/1",
    "description": (
        '<p>Do problems 1-10.</p><p><a href="https://canvas.test/courses/9/files/77/download">handout.txt</a> '
        '<a href="/courses/9/files/78">answers.pdf</a> <a href="https://docs.google.com/t">Template</a></p>'
        '<img src="/courses/9/files/79/preview">'
    ),
    "rubric": [{"description": "Correctness", "points": 15, "long_description": "<p>Right answers</p>"}],
    "submission": {"workflow_state": "unsubmitted", "missing": False},
}


def iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


COURSES = [{"id": 9, "name": "MATH 2A Calculus"}, {"id": 10, "name": "CHEM 1A"}]
MODULES_9 = [
    {"id": 1, "name": "Week 1", "items": [
        {"type": "SubHeader", "title": "Read first"},
        {"type": "Page", "title": "Week 1 notes", "page_url": "week-1-notes"},
        {"type": "File", "title": "Lecture 1.pdf", "content_id": 81},
        {"type": "ExternalUrl", "title": "Desmos", "external_url": "https://desmos.com"},
    ]},
    {"id": 2, "name": "Week 2", "state": "locked", "items_url": "https://canvas.test/api/v1/courses/9/modules/2/items"},
]
MODULE_ITEMS = {  # module item id -> what module_item_sequence says it is
    "500": {"type": "File", "title": "Formula sheet.txt", "content_id": 82},
    "501": {"type": "ExternalUrl", "title": "Desmos", "external_url": "https://desmos.com"},
}
FILES = {
    "77": {"display_name": "handout.txt", "size": len(HANDOUT), "url": "https://canvas.test/files/77/download?verifier=v"},
    "81": {"display_name": "Lecture 1.txt", "size": len(LECTURE), "url": "https://canvas.test/files/81/download"},
    "82": {"display_name": "Formula sheet.txt", "size": len(FORMULAS), "url": "https://canvas.test/files/82/download"},
}
DOWNLOADS = {"/files/77/download": HANDOUT, "/files/80/download": b"Argue a side", "/files/81/download": LECTURE,
             "/files/82/download": FORMULAS}
LISTS = {
    "/api/v1/courses/9/modules": MODULES_9,
    "/api/v1/courses/9/modules/2/items": [
        {"type": "Assignment", "title": "HW 3", "content_id": 1, "content_details": {"locked_for_user": True}},
    ],
    "/api/v1/courses/9/files": [
        {"id": 81, "display_name": "Lecture 1.txt", "size": len(LECTURE), "folder_id": 5},
        {"id": 82, "display_name": "Formula sheet.txt", "size": len(FORMULAS), "folder_id": 6},
    ],
    "/api/v1/courses/9/folders": [{"id": 5, "full_name": "course files"}, {"id": 6, "full_name": "course files/Exams"}],
    "/api/v1/courses/9/pages": [{"title": "Week 1 notes", "url": "week-1-notes"}, {"title": "Office hours", "url": "office-hours"}],
    "/api/v1/courses/10/modules": [],
    "/api/v1/courses/10/pages": [{"title": "Lab rules", "url": "lab-rules"}],
}
ANNOUNCEMENTS = [
    {"context_code": "course_9", "title": "Exam moved", "posted_at": "2026-09-20T10:00:00Z",
     "message": "<p>The midterm is now on <b>Friday</b>.</p>"},
    {"context_code": "course_10", "title": "Lab safety", "posted_at": "2026-09-22T10:00:00Z", "message": "Goggles."},
]


def handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["Authorization"] == "Bearer tok"
    path = request.url.path
    if path.endswith("/courses"):
        return httpx.Response(200, json=COURSES)
    if path.endswith("/announcements"):
        codes = request.url.params.get_list("context_codes[]")
        return httpx.Response(200, json=[a for a in ANNOUNCEMENTS if a["context_code"] in codes])
    if path == "/api/v1/courses/9/assignments/1":
        assert request.url.params.get("include[]") == "submission"
        return httpx.Response(200, json=HW3)
    if path == "/api/v1/courses/10/discussion_topics/6":
        return httpx.Response(200, json={
            "title": "Lab debate", "message": "<p>Post by Friday.</p>",
            "assignment": {"due_at": "2026-09-26T06:59:00Z", "points_possible": 5},
            "attachments": [{"display_name": "prompt.txt", "size": 12, "url": "https://canvas.test/files/80/download"}],
        })
    if path in LISTS:
        return httpx.Response(200, json=LISTS[path])
    if path == "/api/v1/courses/10/files":
        return httpx.Response(403, json={})  # this class hides Files from students
    if path == "/api/v1/courses/9":
        assert request.url.params.get("include[]") == "syllabus_body"
        return httpx.Response(200, json={"id": 9, "name": "MATH 2A Calculus",
                                         "syllabus_body": "<p>Late work loses 10% a day.</p>"})
    if path == "/api/v1/courses/9/pages/week-1-notes":
        return httpx.Response(200, json={"title": "Week 1 notes",
                                         "body": '<p>See the <a href="/courses/9/files/82">formula sheet</a>.</p>'})
    if path == "/api/v1/courses/9/module_item_sequence":
        current = MODULE_ITEMS.get(request.url.params["asset_id"])
        return httpx.Response(200, json={"items": [{"current": current}] if current else []})
    if path.removeprefix("/api/v1/files/") in FILES:
        return httpx.Response(200, json=FILES[path.removeprefix("/api/v1/files/")])
    if path == "/api/v1/files/78":
        return httpx.Response(401, json={"status": "unauthorized"})  # locked for the student, token still fine
    if path in DOWNLOADS:
        return httpx.Response(200, content=DOWNLOADS[path])
    if path.startswith("/api/v1/courses/") or path.startswith("/api/v1/files/"):
        return httpx.Response(404, json={})
    if request.url.params.get("page") == "2":
        return httpx.Response(200, json=[
            {"plannable_type": "quiz", "plannable_id": 2, "context_name": "CHEM 1A", "course_id": 10,
             "plannable": {"title": "Quiz 1", "due_at": iso(3)}, "submissions": {"submitted": False}},
            {"plannable_type": "discussion_topic", "plannable_id": 6, "context_name": "CHEM 1A", "course_id": 10,
             "plannable": {"title": "Lab debate", "todo_date": iso(4)}, "submissions": {"submitted": False}},
        ])
    assert request.url.params.get_list("context_codes[]") == ["course_9", "course_10"]
    return httpx.Response(
        200,
        json=[
            {"plannable_type": "assignment", "plannable_id": 1, "context_name": "MATH 2A Calculus",
             "plannable": {"title": "HW 3", "due_at": iso(1)}, "submissions": {"submitted": True},
             "html_url": "/courses/9/assignments/1"},
            {"plannable_type": "assignment", "plannable_id": 3, "context_name": "MATH 2A Calculus",
             "plannable": {"title": "HW 2", "due_at": iso(-2)}, "submissions": {"submitted": False, "missing": True},
             "html_url": "/courses/9/assignments/3"},
            {"plannable_type": "assignment", "plannable_id": 5, "context_name": "CHEM 1A",
             "plannable": {"title": "Reading log", "due_at": iso(2)}, "submissions": False,
             "planner_override": {"marked_complete": True}},
            {"plannable_type": "announcement", "plannable_id": 4, "context_name": "X", "plannable": {"title": "Hi"}},
        ],
        headers={"Link": '<https://canvas.test/api/v1/planner/items?page=2>; rel="next"'},
    )


def text(result) -> str:
    return "\n".join(c.text for c in result.content)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("CANVAS_URL", raising=False)
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    return Home(tmp_path / "canvas")


def run(home: Home, calls, transport=handler):
    """Runs `calls(call)` against the server over a real MCP client; call(name, **args) gives the result."""
    client = CanvasClient("https://canvas.test", "tok", TZ, transport=httpx.MockTransport(transport))

    async def go():
        async with Client(build(home, client)) as c:
            async def call(name, **args):
                return await c.call_tool(name, args)
            return await calls(call)

    return asyncio.run(go())


def runs(home: Home, calls, transport=handler):
    """Like run, but call(name, **args) gives just the result's text."""
    async def text_calls(call):
        async def call_text(name, **args):
            return text(await call(name, **args))
        return await calls(call_text)
    return run(home, text_calls, transport)


def test_tools_are_read_only(home):
    async def go():
        async with Client(build(home)) as c:
            return (await c.list_tools()).tools
    tools = asyncio.run(go())
    assert {t.name for t in tools} == {"canvas_assignments", "canvas_course", "canvas_open", "read_canvas_file",
                                       "canvas_grades", "canvas_announcements", "canvas_status", "canvas_inbox",
                                       "canvas_message", "canvas_events", "canvas_feedback"}
    assert all(t.annotations.read_only_hint for t in tools)
    assert [t.name for t in tools if not t.annotations.open_world_hint] == ["read_canvas_file"]


def test_knows_no_other_add_ons():
    """Add-ons never name each other: what Canvas feeds goes out as events, and the agent's rules decide."""
    import daimon_canvas
    from pathlib import Path
    for path in Path(daimon_canvas.__file__).parent.glob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        # Canvas's own calendar_events API is fine; another add-on's package or tools are not
        assert not any(w in text for w in ("daimon_calendar", "calendar_add_event", "calendar__", "daimon_messages",
                                           "send_message", "messages__")), path.name


def test_canvas_trouble_is_plain_text(home):
    async def calls(call):
        return await call("canvas_grades")
    result = run(home, calls, transport=lambda request: httpx.Response(500, json={}))
    assert result.is_error and "Canvas answered 500 for /api/v1/courses." in text(result)


def test_not_set_up_says_how(home):
    async def go():
        async with Client(build(home)) as c:
            return await c.call_tool("canvas_assignments", {})
    result = asyncio.run(go())
    assert result.is_error and "daimon-canvas login" in text(result)


def test_assignments(home):
    async def calls(call):
        out = await call("canvas_assignments", days_ahead=7)
        assert out.index("Missing:") < out.index("HW 2") < out.index("Due in the next 7 days")
        assert "HW 2 (missing) | id assignment:9:3" in out
        assert "Quiz 1 | id quiz:10:2" in out and "Lab debate | id discussion:10:6" in out
        assert "HW 3" not in out and "Reading log" not in out and "2 more due in that time already submitted" in out
        out = await call("canvas_assignments", days_ahead=7, course="calc")
        assert "Quiz 1" not in out and "HW 2" in out
    runs(home, calls)


def test_announcements(home):
    async def calls(call):
        out = await call("canvas_announcements", days_back=7)
        assert out.index("Lab safety") < out.index("Exam moved") and "The midterm is now on Friday" in out
        out = await call("canvas_announcements", course="chem")
        assert "Lab safety" in out and "Exam moved" not in out
        assert "No announcements" in await call("canvas_announcements", course="history")
    runs(home, calls)


@pytest.mark.parametrize("ref, expected", [
    ("assignment:9:1", ("assignment", "9", "1")),
    ("[id quiz:10:2]", ("quiz", "10", "2")),
    ("https://canvas.test/courses/10/discussion_topics/6?x=1", ("discussion", "10", "6")),
    ("file:77", ("file", "", "77")),
    ("syllabus:9", ("syllabus", "9", "")),
    ("https://canvas.test/courses/9/modules/items/500", ("module_item", "9", "500")),
])
def test_parse_ref(ref, expected):
    assert parse_ref(ref) == expected
    with pytest.raises(CanvasError):
        parse_ref("HW 3")


def test_open_assignment_saves_details_and_files(home):
    async def calls(call):
        out = await call("canvas_open", item="assignment:9:1")
        assert out.startswith("HW 3 (assignment, MATH 2A Calculus)")
        assert "Due: Thu Sep 24 23:59" in out and "Points: 20" in out and "Status: not submitted" in out
        assert "Do problems 1-10." in out and "Template (https://docs.google.com/t)" in out
        assert "Rubric:\n- Correctness (15 pts): Right answers" in out
        assert "- MATH 2A Calculus/HW 3/handout.txt" in out
        assert "Couldn't get: answers.pdf (not accessible)" in out  # a locked file doesn't sink the rest
        assert "files/79" not in out  # inline images aren't attachments

        assert await call("read_canvas_file", path="MATH 2A Calculus/HW 3/handout.txt") == HANDOUT.decode()
        details = await call("read_canvas_file", path="MATH 2A Calculus/HW 3/HW 3.md")
        assert "Submit as: online upload" in details and "File types: pdf" in details
        assert "isn't in the Canvas downloads folder" in await call("read_canvas_file", path="../config.yaml")
    runs(home, calls)


def test_open_by_title_link_or_ambiguous(home):
    async def calls(call):
        out = await call("canvas_open", item="lab debate")
        assert out.startswith("Lab debate (discussion, CHEM 1A)") and "Points: 5" in out and "Post by Friday." in out
        out = await call("canvas_open", item="calc hw", download_files=False)
        assert out.startswith("Several match") and "id assignment:9:1" in out and "id assignment:9:3" in out
        out = await call("canvas_open", item="https://canvas.test/courses/9/assignments/1", download_files=False)
        assert "Attached files: handout.txt, answers.pdf (not downloaded)" in out
        assert "Nothing on Canvas matches" in await call("canvas_open", item="history essay")
        assert "couldn't find" in await call("canvas_open", item="assignment:9:404")
    runs(home, calls)


def test_course(home):
    async def calls(call):
        out = await call("canvas_course", course="calc")
        assert out.startswith("MATH 2A Calculus (course 9). Syllabus: id syllabus:9")
        assert "Week 1:\n  Read first\n- Week 1 notes [page] | id page:9:week-1-notes" in out
        assert "Week 2 (locked):\n- HW 3 [assignment] (locked) | id assignment:9:1" in out
        out = await call("canvas_course", course="MATH 2A", search="formula")
        assert "Files:\n- Exams/Formula sheet.txt (18 B) | id file:9:82" in out and "Week 1" not in out
        out = await call("canvas_course", course="chem")
        assert "- Lab rules [page] | id page:10:lab-rules" in out and "Files:" not in out
        assert "No active classes match 'bio'" in await call("canvas_course", course="bio")
    runs(home, calls)


def test_long_course_list_is_saved(home):
    home.config_path.write_text(home.config_path.read_text().replace("read_chars: 6000", "read_chars: 1000"))
    many = {"/api/v1/courses/9/pages": [{"title": f"Page {n} " + "x" * 40, "url": f"p{n}"} for n in range(40)]}

    async def calls(call):
        out = await call("canvas_course", course="calc", search="page")
        assert len(out) <= 1000 and "MATH 2A Calculus/contents-page.md" in out
        page = await call("read_canvas_file", path="MATH 2A Calculus/contents-page.md")
        assert "Page 39" not in page and page.endswith("(continues at line 16 of 43)")
        assert "Page 39" in await call("read_canvas_file", path="MATH 2A Calculus/contents-page.md", start_line=40)
    runs(home, calls, lambda r: httpx.Response(200, json=many[r.url.path]) if r.url.path in many else handler(r))


def test_open_files_and_module_items(home):
    async def calls(call):
        out = await call("canvas_open", item="file:9:82")
        assert out.startswith("Formula sheet.txt (18 B), saved to MATH 2A Calculus/files/Formula sheet.txt.")
        assert out.endswith("d/dx sin x = cos x")
        assert "d/dx sin x" in await call("canvas_open", item="https://canvas.test/courses/9/modules/items/500")
        assert await call("canvas_open", item="https://canvas.test/courses/9/modules/items/501") == \
            "'Desmos' is a link to https://desmos.com."
        assert "Limits and continuity." in await call("canvas_open", item="lecture 1")
        out = await call("canvas_open", item="syllabus:9")
        assert out.startswith("Syllabus (syllabus, MATH 2A Calculus)") and "Late work loses 10% a day." in out
    runs(home, calls)


def test_status_and_grades(home):
    def with_me(request):
        if request.url.path == "/api/v1/users/self":
            return httpx.Response(200, json={"name": "Sam Student"})
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json=[{**COURSES[0], "enrollments": [
                {"type": "student", "computed_current_score": 91.5, "computed_current_grade": "A-"}]}])
        return handler(request)

    async def calls(call):
        status = await call("canvas_status")
        assert status.startswith("Connected to https://canvas.test as Sam Student.") and "MATH 2A Calculus" in status
        assert await call("canvas_grades") == "MATH 2A Calculus: 91.5% (A-)"
    runs(home, calls, with_me)


def test_server_errors_reach_the_model(home):
    async def calls(call):
        result = await call("canvas_grades")
        assert result.is_error and "Canvas answered 500" in text(result)
    run(home, calls, lambda r: httpx.Response(500, json={}))


def test_save_login_keeps_comments(home):
    home.save_login("https://school.instructure.com/", "secret")
    assert home.config().url == "https://school.instructure.com" and home.token() == "secret"
    assert "# Your school's Canvas" in home.config_path.read_text()



INBOX = [{"id": 300, "subject": "Lab partner", "context_name": "CHEM 1A", "workflow_state": "unread",
          "participants": [{"id": 1, "name": "Sam"}, {"id": 3, "name": "Ms. Rivera"}],
          "last_message": "Please pair with Jo.", "last_message_at": "2026-09-22T17:00:00Z"}]
THREAD = {"id": 300, "subject": "Lab partner", "context_name": "CHEM 1A",
          "participants": [{"id": 1, "name": "Sam"}, {"id": 3, "name": "Ms. Rivera"}],
          "messages": [{"id": 902, "author_id": 3, "created_at": "2026-09-22T17:00:00Z", "body": "Please pair with Jo.",
                        "attachments": [{"display_name": "pairs.pdf"}]},
                       {"id": 901, "author_id": 1, "created_at": "2026-09-22T16:00:00Z", "body": "Who is my partner?"}]}


def more(request: httpx.Request) -> httpx.Response:
    """The fake Canvas, plus the inbox, class events and the activity stream."""
    path = request.url.path
    if path == "/api/v1/conversations":
        return httpx.Response(200, json=INBOX if request.url.params.get("scope") != "unread" else [])
    if path == "/api/v1/conversations/300":
        assert request.url.params["auto_mark_as_read"] == "false"
        return httpx.Response(200, json=THREAD)
    if path == "/api/v1/conversations/9":
        return httpx.Response(404, json={})
    if path == "/api/v1/calendar_events":
        assert request.url.params.get_list("context_codes[]") == ["course_10"]
        return httpx.Response(200, json=[{"id": 50, "context_code": "course_10", "title": "Midterm",
                                          "start_at": iso(5), "location_name": "Room 101",
                                          "description": "<p>Bring a calculator.</p>"}])
    if path == "/api/v1/users/self":
        return httpx.Response(200, json={"id": 1, "name": "Sam"})
    if path == "/api/v1/users/self/activity_stream":
        return httpx.Response(200, json=[
            {"type": "Announcement", "title": "Hi"},
            {"type": "Submission", "course_id": 9, "assignment_id": 1, "assignment": {"name": "HW 3"},
             "submission_comments": [
                 {"id": 71, "author_id": 2, "author_name": "Prof. Ada", "comment": "Nice work on #3.",
                  "created_at": "2026-09-23T17:00:00Z"},
                 {"id": 72, "author_id": 1, "author_name": "Sam", "comment": "Thanks!",
                  "created_at": "2026-09-23T18:00:00Z"}]}])
    return handler(request)


def test_inbox_events_and_feedback(home):
    async def calls(call):
        inbox = await call("canvas_inbox")
        assert "Lab partner (unread) | with Sam, Ms. Rivera | id 300" in inbox and "Please pair with Jo." in inbox
        assert await call("canvas_inbox", unread_only=True) == "No unread Canvas messages."
        thread = await call("canvas_message", conversation="300")
        assert thread.startswith("Lab partner (CHEM 1A)")
        assert thread.index("Who is my partner?") < thread.index("Ms. Rivera:\nPlease pair with Jo.")
        assert "Attached: pairs.pdf" in thread
        assert "canvas_inbox lists them" in await call("canvas_message", conversation="9")
        events = await call("canvas_events", course="chem")
        assert "| CHEM 1A | Midterm | Room 101" in events and "Bring a calculator." in events
        feedback = await call("canvas_feedback")
        assert "MATH 2A Calculus | HW 3 | Prof. Ada: Nice work on #3. | id assignment:9:1" in feedback
        assert "Thanks!" not in feedback
    runs(home, calls, transport=more)


def test_token_never_printed(monkeypatch, capsys, tmp_path):
    from daimon_canvas import cli
    monkeypatch.setattr(cli, "secret_input", lambda prompt: "7~abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setattr(cli, "check", lambda home, url, token: "Sam, 3 active classes")

    class FakeHome:
        root = tmp_path

        def save_login(self, url, token):
            self.saved = (url, token)
    home = FakeHome()
    cli.login(home, "school.instructure.com")
    out = capsys.readouterr().out
    assert "7~abcdefghijklmnopqrstuvwxyz" not in out and "7~ab…wxyz" in out
    assert home.saved == ("https://school.instructure.com", "7~abcdefghijklmnopqrstuvwxyz")
