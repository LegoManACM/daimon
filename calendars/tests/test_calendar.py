"""The calendar MCP server against fake Google, Microsoft Graph, CalDAV and .ics servers, through a real MCP client."""
import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import icalendar
import pytest
from mcp import Client

from daimon_calendar.config import Home
from daimon_calendar.mcp_server import build
from daimon_calendar.microsoft import recurrence
from daimon_calendar.model import CalendarError, parse_when, source_of, with_source

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime.now(TZ).replace(second=0, microsecond=0)
TOMORROW = (NOW + timedelta(days=1)).date()


def at(day: date, hm: str) -> str:
    return f"{day.isoformat()}T{hm}"


# ---- fakes -----------------------------------------------------------------------------------------
class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.refreshes = 0

    def __call__(self, r: httpx.Request) -> httpx.Response:
        if r.url.host == "oauth2.googleapis.com":
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": "g-fresh", "expires_in": 3600})
        assert r.headers["Authorization"] == "Bearer g-fresh"
        path = r.url.path.removeprefix("/calendar/v3")
        if path == "/users/me/calendarList":
            return httpx.Response(200, json={"items": [
                {"id": "me@gmail.com", "summary": "me@gmail.com", "accessRole": "owner", "primary": True},
                {"id": "holidays", "summary": "Holidays", "accessRole": "reader"}]})
        parts = path.split("/")  # '', calendars, <cal>, events, [id]
        cal = parts[2]
        if len(parts) == 4 and r.method == "GET":
            return httpx.Response(200, json={"items": [e for e in self.events.values() if e["cal"] == cal]})
        if len(parts) == 4 and r.method == "POST":
            body = json.loads(r.content) | {"id": uuid.uuid4().hex, "cal": cal, "htmlLink": "https://cal.google/e"}
            self.events[body["id"]] = body
            return httpx.Response(200, json=body)
        e = self.events.get(parts[4])
        if e is None:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if r.method == "GET":
            return httpx.Response(200, json=e)
        if r.method == "PATCH":
            e.update(json.loads(r.content))
            return httpx.Response(200, json=e)
        if r.method == "DELETE":
            del self.events[parts[4]]
            return httpx.Response(204)
        raise AssertionError(r)


class FakeGraph:
    def __init__(self):
        self.events: dict[str, dict] = {"m1": {
            "id": "m1", "subject": "Standup", "isAllDay": False, "showAs": "busy", "type": "occurrence",
            "start": {"dateTime": (NOW + timedelta(hours=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                      "timeZone": "UTC"},
            "end": {"dateTime": (NOW + timedelta(hours=2, minutes=15)).astimezone(timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.0000000"), "timeZone": "UTC"},
            "location": {"displayName": "Teams"}, "body": {"content": "daily"}}}
        self.posted: list[dict] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        if r.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "m-fresh", "expires_in": 3600, "refresh_token": "m-r2"})
        assert r.headers["Authorization"] == "Bearer m-fresh"
        assert 'outlook.timezone="UTC"' in r.headers["Prefer"]
        path = r.url.path.removeprefix("/v1.0")
        if path == "/me":
            return httpx.Response(200, json={"mail": "me@outlook.com"})
        if path == "/me/calendars":
            return httpx.Response(200, json={"value": [{"id": "C1", "name": "Calendar", "canEdit": True,
                                                        "isDefaultCalendar": True}]})
        if path == "/me/calendars/C1/calendarView":
            return httpx.Response(200, json={"value": list(self.events.values())})
        if path == "/me/calendars/C1/events":
            body = json.loads(r.content) | {"id": f"m{len(self.events) + 1}"}
            self.posted.append(body)
            self.events[body["id"]] = body
            return httpx.Response(201, json=body)
        e = self.events.get(path.rsplit("/", 1)[-1])
        if e is None:
            return httpx.Response(404, json={"error": {"message": "gone"}})
        return httpx.Response(200, json=e)


def multistatus(*responses: str) -> httpx.Response:
    return httpx.Response(207, content=('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                                        'xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(responses)
                                        + "</d:multistatus>").encode())


def prop(href: str, inner: str) -> str:
    return f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{inner}</d:prop></d:propstat></d:response>"


class FakeDav:
    def __init__(self):
        self.files: dict[str, tuple[bytes, str]] = {}  # path -> (ics, etag)
        self.auth_ok = True

    def __call__(self, r: httpx.Request) -> httpx.Response:
        assert r.headers["Authorization"].startswith("Basic ")
        path = r.url.path
        if r.method == "PROPFIND" and r.url.host == "dav.test" and path == "/":
            return multistatus(prop("/", "<d:current-user-principal><d:href>/principals/me/</d:href>"
                                         "</d:current-user-principal>"))
        if r.method == "PROPFIND" and path == "/principals/me/":
            return multistatus(prop(path, "<c:calendar-home-set><d:href>https://p9.dav.test/home/</d:href>"
                                          "</c:calendar-home-set>"))
        if r.method == "PROPFIND" and path == "/home/":
            assert r.url.host == "p9.dav.test" and r.headers["Depth"] == "1"
            return multistatus(
                prop("/home/", "<d:resourcetype><d:collection/></d:resourcetype>"),
                prop("/home/work/", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                                    "<d:displayname>Work</d:displayname><c:supported-calendar-component-set>"
                                    "<c:comp name='VEVENT'/></c:supported-calendar-component-set>"
                                    "<d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege>"
                                    "<d:write/></d:privilege></d:current-user-privilege-set>"),
                prop("/home/todo/", "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                                    "<d:displayname>Reminders</d:displayname><c:supported-calendar-component-set>"
                                    "<c:comp name='VTODO'/></c:supported-calendar-component-set>"))
        if r.method == "REPORT":
            assert b"time-range" in r.content
            return multistatus(*(prop(p, f"<d:getetag>{etag}</d:getetag><c:calendar-data>"
                                         f"{ics.decode().replace('&', '&amp;').replace('<', '&lt;')}</c:calendar-data>")
                                 for p, (ics, etag) in self.files.items() if p.startswith(path)))
        if r.method == "PUT":
            if r.headers.get("If-None-Match") == "*" and path in self.files:
                return httpx.Response(412)
            if "If-Match" in r.headers and self.files[path][1] != r.headers["If-Match"]:
                return httpx.Response(412)
            icalendar.Calendar.from_ical(r.content)  # must be valid
            self.files[path] = (r.content, f'"{uuid.uuid4().hex[:8]}"')
            return httpx.Response(201)
        if r.method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[path][0], headers={"ETag": self.files[path][1]})
        if r.method == "DELETE":
            return httpx.Response(204 if self.files.pop(path, None) else 404)
        raise AssertionError(f"{r.method} {r.url}")


FEED = f"""BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:School
BEGIN:VEVENT
UID:class-1
DTSTART;TZID=America/Los_Angeles:{(NOW.date() - timedelta(days=14)).strftime('%Y%m%d')}T090000
DTEND;TZID=America/Los_Angeles:{(NOW.date() - timedelta(days=14)).strftime('%Y%m%d')}T103000
RRULE:FREQ=DAILY
SUMMARY:Chem lecture
LOCATION:Hall B
END:VEVENT
END:VCALENDAR
"""


class World:
    def __init__(self):
        self.google, self.graph, self.dav = FakeGoogle(), FakeGraph(), FakeDav()

    def __call__(self, r: httpx.Request) -> httpx.Response:
        host = r.url.host
        if host in ("www.googleapis.com", "oauth2.googleapis.com"):
            return self.google(r)
        if host in ("graph.microsoft.com", "login.microsoftonline.com"):
            return self.graph(r)
        if host.endswith("dav.test"):
            return self.dav(r)
        if host == "feeds.test":
            return httpx.Response(200, content=FEED.encode())
        if host == "down.test":
            raise httpx.ConnectError("no route", request=r)
        raise AssertionError(f"unexpected {r.url}")


def make_home(tmp_path, accounts=("google",), **settings) -> Home:
    home = Home(tmp_path / "cal")
    cfg = {"timezone": "America/Los_Angeles", **settings}
    home.config_path.write_text("\n".join(f"{k}: {json.dumps(v)}" for k, v in cfg.items()), encoding="utf-8")
    expired = {"access_token": "old", "refresh_token": "r1", "expires_at": 0}
    data = {
        "google": {"kind": "google", "client_id": "cid", "client_secret": "cs", "token": expired},
        "outlook": {"kind": "microsoft", "client_id": "mid", "token": expired},
        "icloud": {"kind": "caldav", "url": "https://dav.test/", "username": "me", "password": "pw"},
        "school": {"kind": "ics", "url": "webcal://feeds.test/school.ics", "title": "School"},
        "broken": {"kind": "ics", "url": "https://down.test/x.ics"},
    }
    for name in accounts:
        home.save_account(name, data[name])
    return home


def call(home: Home, world: World, *steps: tuple[str, dict]) -> list[tuple[bool, str]]:
    async def go():
        out = []
        async with Client(build(home, httpx.MockTransport(world))) as client:
            for tool, args in steps:
                r = await client.call_tool(tool, args)
                out.append((r.is_error, "\n".join(c.text for c in r.content)))
        return out
    return asyncio.run(go())


def key_of(text: str) -> str:
    return text.split("id ")[1].split()[0].strip()


# ---- tests -----------------------------------------------------------------------------------------
def test_parse_when():
    now = datetime(2026, 10, 29, 14, 30, tzinfo=TZ)  # a Thursday
    assert parse_when("2026-10-30", TZ, now) == date(2026, 10, 30)
    assert parse_when("2026-10-30T15:00", TZ, now) == datetime(2026, 10, 30, 15, tzinfo=TZ)
    assert parse_when("2026-10-30 15:00", TZ, now) == datetime(2026, 10, 30, 15, tzinfo=TZ)
    assert parse_when("2026-10-30T22:00Z", TZ, now) == datetime(2026, 10, 30, 15, tzinfo=TZ)
    assert parse_when("tomorrow", TZ, now) == date(2026, 10, 30)
    assert parse_when("fri", TZ, now) == date(2026, 10, 30)
    assert parse_when("thursday", TZ, now) == date(2026, 11, 5)  # the next one, not today
    assert parse_when("+2h", TZ, now) == now + timedelta(hours=2)
    with pytest.raises(CalendarError, match="Can't read the time"):
        parse_when("next blue moon", TZ, now)


def test_source_marker_round_trip():
    notes = with_source("Bring the form.", "sms:12:345")
    assert source_of(notes) == "sms:12:345"
    assert with_source(notes, "sms:12:999").count("daimon-source") == 1
    assert source_of(with_source(notes, "")) == ""


def test_tools_and_annotations(tmp_path):
    async def go():
        async with Client(build(make_home(tmp_path))) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    tools = asyncio.run(go())
    read = {n for n, t in tools.items() if t.annotations.read_only_hint}
    assert read == {"calendar_events", "calendar_free_time", "calendar_list", "calendar_status"}
    assert tools["calendar_delete_event"].annotations.destructive_hint
    assert tools["calendar_add_event"].input_schema["required"] == ["title", "start"]


def test_google_add_is_idempotent_by_source(tmp_path):
    home, world = make_home(tmp_path), World()
    start = at(TOMORROW, "18:00")
    args = {"title": "Dinner with Sam", "start": start, "location": "Nopa", "source": "sms:7:991"}
    (e1, added), (e2, again), (e3, moved) = call(home, world,
        ("calendar_add_event", args),
        ("calendar_add_event", args),
        ("calendar_add_event", args | {"start": at(TOMORROW, "19:30")}))
    assert not (e1 or e2 or e3), (added, again, moved)
    assert added.startswith("Added:") and "18:00–19:00" in added and "google/me@gmail.com" in added
    assert again.startswith("Unchanged:") and key_of(again) == key_of(added)
    assert moved.startswith("Updated:") and "19:30–20:30" in moved  # keeps its hour
    assert len(world.google.events) == 1
    stored = next(iter(world.google.events.values()))
    assert stored["summary"] == "Dinner with Sam" and stored["description"].endswith("daimon-source: sms:7:991")
    assert stored["start"]["dateTime"].startswith(f"{TOMORROW.isoformat()}T19:30:00-0")
    # the refreshed token was saved; the old one was never sent
    assert home.accounts()["google"]["token"]["access_token"] == "g-fresh" and world.google.refreshes == 1


def test_google_list_update_delete(tmp_path):
    home, world = make_home(tmp_path), World()
    _, added = call(home, world, ("calendar_add_event", {"title": "Dentist", "start": at(TOMORROW, "09:00"),
                                                         "end": at(TOMORROW, "09:45"), "notes": "x-ray"}))[0]
    key = key_of(added)
    [(_, listed), (err, changed), (_, by_source_missing), (_, deleted), (_, after)] = call(home, world,
        ("calendar_events", {"start": "today", "days": 3}),
        ("calendar_update_event", {"event": key, "title": "Dentist (cleaning)", "location": "Main St"}),
        ("calendar_delete_event", {"source": "nope"}),
        ("calendar_delete_event", {"event": key}),
        ("calendar_events", {"start": "today", "days": 3}))
    assert "Dentist" in listed and f"id {key}" in listed and "Notes: x-ray" in listed
    assert not err and changed.startswith("Updated:") and "Dentist (cleaning) @ Main St" in changed
    assert "09:00–09:45" in changed
    assert "No event has the source 'nope'" in by_source_missing
    assert deleted.startswith("Deleted:") and not world.google.events
    assert "Nothing on the calendar" in after


def test_all_day_and_bad_input(tmp_path):
    home, world = make_home(tmp_path), World()
    [(_, day), (err1, no_title), (err2, bad_time), (err3, backwards), (err4, read_only)] = call(home, world,
        ("calendar_add_event", {"title": "Trip", "start": TOMORROW.isoformat()}),
        ("calendar_add_event", {"title": " ", "start": "tomorrow"}),
        ("calendar_add_event", {"title": "x", "start": "soonish"}),
        ("calendar_add_event", {"title": "x", "start": at(TOMORROW, "10:00"), "end": at(TOMORROW, "09:00")}),
        ("calendar_add_event", {"title": "x", "start": "tomorrow", "calendar": "Holidays"}))
    assert "all day" in day
    stored = next(iter(world.google.events.values()))
    assert stored["start"] == {"date": TOMORROW.isoformat()}
    assert stored["end"] == {"date": (TOMORROW + timedelta(days=1)).isoformat()}
    assert err1 and "title" in no_title
    assert err2 and "start: Can't read the time 'soonish'" in bad_time
    assert err3 and "end is before the start" in backwards
    assert err4 and "read-only" in read_only


def test_microsoft(tmp_path):
    home, world = make_home(tmp_path, accounts=("outlook",)), World()
    [(_, listed), (err, added), (_, status)] = call(home, world,
        ("calendar_events", {"start": "now", "days": 1}),
        ("calendar_add_event", {"title": "Gym", "start": at(TOMORROW, "07:00"), "repeat": "FREQ=WEEKLY;BYDAY=MO,WE",
                                "reminder_minutes": 10}),
        ("calendar_status", {}))
    standup = (NOW + timedelta(hours=2)).strftime("%H:%M")
    assert f"{standup}–" in listed and "Standup @ Teams (repeats)" in listed
    assert not err, added
    body = world.graph.posted[0]
    assert body["start"]["timeZone"] == "UTC" and body["reminderMinutesBeforeStart"] == 10
    assert body["recurrence"]["pattern"] == {"interval": 1, "type": "weekly", "daysOfWeek": ["monday", "wednesday"],
                                             "firstDayOfWeek": "sunday"}
    assert "outlook (microsoft, me@outlook.com): Calendar" in status
    assert home.accounts()["outlook"]["token"]["refresh_token"] == "m-r2"


def test_graph_recurrence_rules():
    d = date(2026, 11, 10)  # a Tuesday
    assert recurrence("FREQ=MONTHLY;BYDAY=2TU;COUNT=6", d) == {
        "pattern": {"interval": 1, "type": "relativeMonthly", "daysOfWeek": ["tuesday"], "index": "second"},
        "range": {"startDate": "2026-11-10", "type": "numbered", "numberOfOccurrences": 6}}
    assert recurrence("FREQ=DAILY;INTERVAL=2;UNTIL=20261201T000000Z", d)["range"] == {
        "startDate": "2026-11-10", "type": "endDate", "endDate": "2026-12-01"}
    assert recurrence("FREQ=YEARLY", d)["pattern"]["month"] == 11
    with pytest.raises(CalendarError):
        recurrence("FREQ=HOURLY", d)


def test_caldav_discovery_add_update_delete(tmp_path):
    home, world = make_home(tmp_path, accounts=("icloud",)), World()
    [(_, cals), (err, added)] = call(home, world,
        ("calendar_list", {}),
        ("calendar_add_event", {"title": "Book club", "start": at(TOMORROW, "19:00"), "source": "x:1",
                                "reminder_minutes": 30}))
    assert cals == "- icloud/Work (default)"  # the tasks-only list is left out
    assert home.accounts()["icloud"]["home"] == "https://p9.dav.test/home/"  # remembered
    assert not err and added.startswith("Added:")
    [(path, (ics, _))] = world.dav.files.items()
    assert path.startswith("/home/work/") and b"VALARM" in ics and b"daimon-source: x:1" in ics.replace(b"\r\n ", b"")

    [(_, listed), (err, moved), (_, gone)] = call(home, world,
        ("calendar_events", {"start": "today", "end": (TOMORROW + timedelta(days=1)).isoformat()}),
        ("calendar_update_event", {"source": "x:1", "start": at(TOMORROW, "20:00")}),
        ("calendar_delete_event", {"source": "x:1"}))
    assert "19:00–20:00 | Book club | icloud/Work" in listed
    assert not err and "20:00–21:00" in moved
    assert gone.startswith("Deleted:") and not world.dav.files


def test_ics_feed_merged_with_free_time(tmp_path):
    home, world = make_home(tmp_path, accounts=("school", "google")), World()
    tom = TOMORROW.isoformat()
    [(_, listed), (_, free), (err, added)] = call(home, world,
        ("calendar_events", {"start": tom, "end": tom}),
        ("calendar_free_time", {"start": at(TOMORROW, "08:00"), "end": at(TOMORROW, "12:00"), "minutes": 30}),
        ("calendar_add_event", {"title": "x", "start": "tomorrow", "calendar": "school/School"}))
    assert "09:00–10:30 | Chem lecture @ Hall B (repeats) | school/School" in listed
    assert f"{tom} 08:00–09:00 (60 min)" in free and f"{tom} 10:30–12:00 (90 min)" in free
    assert err and "read-only" in added


def test_one_account_down_doesnt_hide_the_rest(tmp_path):
    home, world = make_home(tmp_path, accounts=("broken", "school")), World()
    [(err, listed)] = call(home, world, ("calendar_events", {"start": "tomorrow", "days": 1}))
    assert not err and "Chem lecture" in listed and "Couldn't read broken/broken" in listed


def test_not_set_up(tmp_path):
    [(err, text)] = call(Home(tmp_path / "empty"), World(), ("calendar_events", {}))
    assert err and "daimon-calendar add" in text


def test_secrets_are_masked_after_entry(monkeypatch, capsys):
    from daimon_calendar import cli
    monkeypatch.setattr(cli, "secret_input", lambda prompt: "  hunter2-very-long-password  ")
    assert cli.ask("Password (shows as *): ", secret=True) == "hunter2-very-long-password"
    out = capsys.readouterr().out
    assert "hunter2-very-long-password" not in out and "hunt…word" in out
    assert cli.masked("short") == "*****"
