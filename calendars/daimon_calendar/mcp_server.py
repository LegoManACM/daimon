"""The calendar MCP server (`daimon-calendar mcp`, stdio): every connected calendar as one.

Reading is free (readOnlyHint), adding/changing/deleting isn't, so the agent's consent gate asks by default. The
add tool is meant to be called by anything, not only a model: plain ISO times, an optional `source` key that makes
calling it twice harmless, and errors that say exactly what was wrong.
"""
from __future__ import annotations

import functools
from datetime import date, datetime, timedelta

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .config import Home
from .model import CalendarError, Draft, Event, as_datetime, describe_time, parse_when, without_source
from .service import Calendars

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """The user's calendars (Google, Outlook, iCloud and other CalDAV, subscribed .ics feeds), merged.
calendar_events lists what's on; calendar_free_time finds open slots; calendar_add_event adds (give `source` when
the event comes from something with an id, like a text or an assignment, so adding it again updates it instead of
duplicating it). Times are the user's local time unless they carry an offset."""


def friendly(fn):
    """Network trouble and server errors as a message for the caller, not a crash."""
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            raise CalendarError(f"The calendar server answered {e.response.status_code} for "
                                f"{e.request.url.host}.") from e
        except httpx.HTTPError as e:
            raise CalendarError(f"Couldn't reach the calendar server ({type(e).__name__}). Try again in a bit.") from e
    return run


def build(home: Home, transport: httpx.AsyncBaseTransport | None = None) -> MCPServer:
    server = MCPServer("calendar", instructions=INSTRUCTIONS)
    service: list[Calendars] = []

    def cals() -> Calendars:
        # Built on first use, so a calendar added with `daimon-calendar add` shows up after a restart of the server.
        if not service:
            service.append(Calendars(home, transport))
        return service[0]

    def when(text: str, field: str) -> datetime | date:
        try:
            return parse_when(text, cals().tz)
        except CalendarError as e:
            raise CalendarError(f"{field}: {e}") from None

    def until(end: str, otherwise: datetime) -> datetime:
        """The end of a listing: a date means through the end of that day."""
        if not end.strip():
            return otherwise
        raw = when(end, "end")
        return as_datetime(raw, cals().tz) + (timedelta() if isinstance(raw, datetime) else timedelta(days=1))

    def line(e: Event) -> str:
        bits = [describe_time(e, cals().tz), e.title]
        if e.location:
            bits[-1] += f" @ {e.location}"
        extra = [x for x in ("repeats" if e.recurring else "", "tentative" if e.status else "",
                             "free" if not e.busy and not e.all_day else "") if x]
        if extra:
            bits[-1] += f" ({', '.join(extra)})"
        bits += [f"{e.account}/{e.calendar}", f"id {e.key}"]
        return " | ".join(bits)

    def detail(e: Event) -> str:
        out = [line(e)]
        if e.attendees:
            out.append("With: " + ", ".join(e.attendees[:12]) + (" …" if len(e.attendees) > 12 else ""))
        notes = without_source(e.description).strip()
        if notes:
            out.append("Notes: " + (notes if len(notes) <= 400 else notes[:399] + "…"))
        if e.source:
            out.append(f"Source: {e.source}")
        return "\n".join(out)

    def notes_about_problems() -> str:
        p = cals().problems
        return ("\n(Couldn't read " + "; ".join(p) + ")") if p else ""

    def draft(title: str, start: str, end: str, all_day: bool | None, location: str, notes: str,
              attendees: list[str] | None, repeat: str, reminder_minutes: int) -> Draft:
        return Draft(
            title=title.strip() or None, start=when(start, "start") if start.strip() else None,
            end=when(end, "end") if end.strip() else None, all_day=all_day, location=location or None,
            description=notes or None, attendees=[a.strip() for a in attendees] if attendees else None,
            repeat=repeat.strip() or None, reminder_minutes=reminder_minutes if reminder_minutes >= 0 else None)

    @server.tool(annotations=READ)
    @friendly
    async def calendar_events(start: str = "today", end: str = "", days: int = 7, query: str = "",
                              calendar: str = "") -> str:
        """What's on the user's calendars, in time order, each with an id.

        start: where to begin (2026-10-30, 2026-10-30T15:00, now, today, tomorrow, friday, +2d). Default today.
        end: where to stop; empty = `days` days after start.
        query: words that must all appear in the title, place or notes.
        calendar: only this calendar (a name or account/name, see calendar_list).
        """
        c = cals()
        s = as_datetime(when(start, "start"), c.tz)
        e = until(end, s + timedelta(days=max(1, min(days, 366))))
        found = await c.events(s, e, calendar, query)
        span = f"{s:%a %Y-%m-%d %H:%M} to {e:%a %Y-%m-%d %H:%M}"
        if not found:
            return f"Nothing on the calendar from {span}" + (f" matching '{query}'" if query else "") + "." + \
                notes_about_problems()
        shown = found[:80]
        text = "\n".join(detail(x) if len(found) <= 8 else line(x) for x in shown)
        more = f"\n({len(found) - len(shown)} more: narrow the dates.)" if len(found) > len(shown) else ""
        return f"{len(found)} events from {span}:\n{text}{more}{notes_about_problems()}"

    @server.tool(annotations=READ)
    @friendly
    async def calendar_free_time(start: str = "today", end: str = "", minutes: int = 60, calendar: str = "") -> str:
        """Open slots of at least `minutes` between start and end (default: the next 7 days), within the
        user's usual hours. Busy timed events block; all-day events and ones marked free don't."""
        c = cals()
        s = max(as_datetime(when(start, "start"), c.tz), datetime.now(c.tz))
        e = until(end, s + timedelta(days=7))
        slots = await c.free_time(s, e, minutes, calendar)
        if not slots:
            return f"No free {minutes}-minute slot between {s:%a %m-%d %H:%M} and {e:%a %m-%d %H:%M}." + \
                notes_about_problems()
        lines = [f"{a:%a %Y-%m-%d %H:%M}–{b:%H:%M} ({int((b - a).total_seconds() // 60)} min)" for a, b in slots[:40]]
        return f"Free (hours {c.cfg.day_start}–{c.cfg.day_end}):\n" + "\n".join(lines) + notes_about_problems()

    @server.tool(annotations=WRITE)
    @friendly
    async def calendar_add_event(title: str, start: str, end: str = "", all_day: bool = False, location: str = "",
                                 notes: str = "", calendar: str = "", attendees: list[str] | None = None,
                                 repeat: str = "", reminder_minutes: int = -1, source: str = "") -> str:
        """Add an event to the user's calendar. Returns its id.

        start: 2026-10-30T15:00 (local time; an offset or Z is fine too), or 2026-10-30 for an all-day event.
          Also: now, today, tomorrow, friday, +2h.
        end: empty = an hour (the configured default) after start, or one day for an all-day event.
        calendar: which calendar (name or account/name, see calendar_list); empty = the default one.
        attendees: email addresses to invite. repeat: an RRULE like FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10.
        reminder_minutes: a reminder this long before (-1 = the calendar's usual).
        source: a key for where this event comes from (sms:<chat>:<message>, canvas:assignment:9:1, ...). If an
          event with that source is already on the calendar it's updated instead, so calling twice is harmless.
        """
        c = cals()
        d = draft(title, start, end, all_day or None, location, notes, attendees, repeat, reminder_minutes)
        if d.title is None:
            raise CalendarError("title: a new event needs a title.")
        if d.start is None:
            raise CalendarError("start: a new event needs a start (like 2026-10-30T15:00, or 2026-10-30 for all day).")
        r = await c.add(d, calendar, source.strip())
        return f"{r.verb}: {detail(r.event)}"

    @server.tool(annotations=WRITE)
    @friendly
    async def calendar_update_event(event: str = "", source: str = "", title: str = "", start: str = "",
                                    end: str = "", all_day: bool | None = None, location: str | None = None,
                                    notes: str | None = None, attendees: list[str] | None = None, repeat: str = "",
                                    reminder_minutes: int = -1) -> str:
        """Change an event: give its id (from calendar_events) or the source it was added with, and only what
        changes. Moving the start alone keeps its length. location/notes "" clears them. Changing one occurrence of
        a repeating iCloud/CalDAV event changes the whole series."""
        if not event.strip() and not source.strip():
            raise CalendarError("Say which event: its id (from calendar_events) or its source.")
        d = draft(title, start, end, all_day, "", "", attendees, repeat, reminder_minutes)
        d.location, d.description = location, notes
        r = await cals().update(d, event, source)
        return f"{r.verb}: {detail(r.event)}"

    @server.tool(annotations=DELETE)
    @friendly
    async def calendar_delete_event(event: str = "", source: str = "") -> str:
        """Delete an event, by its id (from calendar_events) or the source it was added with. For a repeating
        event this deletes that occurrence on Google and Outlook, and the whole series on iCloud/CalDAV."""
        if not event.strip() and not source.strip():
            raise CalendarError("Say which event: its id (from calendar_events) or its source.")
        gone = await cals().delete(event, source)
        if gone is None:
            return "It was already gone."
        return f"Deleted: {describe_time(gone, cals().tz)} {gone.title}"

    @server.tool(annotations=READ)
    @friendly
    async def calendar_list() -> str:
        """The connected calendars: which can be written to, which is the default for new events, which are hidden
        from listings."""
        c = cals()
        default = await c.writable_calendar() if any(x.writable for x in await c.calendars(True)) else None
        lines = []
        for x in await c.calendars(include_hidden=True):
            tags = [t for t in ("default" if default and x.id == default.id and x.account == default.account else "",
                                "read-only" if not x.writable else "", "hidden" if c._hidden(x) else "") if t]
            lines.append(f"- {c.label(x)}" + (f" ({', '.join(tags)})" if tags else ""))
        return "\n".join(lines) + notes_about_problems().replace("Couldn't read", "Couldn't reach")

    @server.tool(annotations=READ)
    @friendly
    async def calendar_status() -> str:
        """Whether the calendars are connected: each account, who it's signed in as, and its calendars."""
        c = cals()
        if not c.providers:
            return "No calendar is connected yet. The user runs `daimon-calendar add` once."
        cal_list = await c.calendars(include_hidden=True)
        lines = []
        for name, p in c.providers.items():
            mine = [x.name for x in cal_list if x.account == name]
            try:
                who = await p.whoami()
            except Exception:  # noqa: BLE001
                who = "?"
            lines.append(f"{name} ({p.kind}, {who}): " + (", ".join(mine) if mine else "not reachable"))
        return "\n".join(lines) + f"\nTimezone: {c.tz}" + notes_about_problems()

    return server


def main(home: Home) -> None:
    build(home).run("stdio")
