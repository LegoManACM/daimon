"""What every calendar provider shares: events, calendars, the Provider interface, and time parsing.

A provider (Google, Microsoft, CalDAV, an .ics feed...) only knows how to list its calendars and list, create,
change and delete events in one of them. Everything the agent sees (short ids, merging accounts, free time,
de-duplicating events other add-ons create) is built on top of that in service.py, the same for all of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.mcpserver.exceptions import ToolError

USER_AGENT = "daimon-calendar/0.1"
SOURCE_PREFIX = "daimon-source: "  # the last line of the notes of an event another add-on asked for


class CalendarError(ToolError):
    """Something to tell the caller as is: a bad time, an unknown calendar, an expired sign-in. The MCP server passes
    a ToolError's text on to the client."""


class Missing(CalendarError):
    """The event (or calendar) isn't there any more."""


@dataclass
class CalendarInfo:
    id: str            # the provider's own id (CalDAV: the calendar's URL)
    name: str
    writable: bool = True
    primary: bool = False
    account: str = ""  # filled in by the service


@dataclass
class Event:
    id: str                    # the provider's own id (CalDAV: the event's href)
    calendar_id: str
    title: str
    start: datetime | date     # aware datetimes, or dates for all-day events (end exclusive)
    end: datetime | date
    all_day: bool = False
    location: str = ""
    description: str = ""
    url: str = ""
    attendees: list[str] = field(default_factory=list)
    organizer: str = ""
    recurring: bool = False    # an occurrence of a repeating event
    busy: bool = True          # False: marked free/transparent
    status: str = ""           # "", tentative, cancelled
    # set by the service:
    account: str = ""
    calendar: str = ""
    key: str = ""              # the short id the agent uses

    @property
    def source(self) -> str:
        return source_of(self.description)


@dataclass
class Draft:
    """A new event, or the changes to one (None = leave as it is)."""
    title: str | None = None
    start: datetime | date | None = None
    end: datetime | date | None = None
    all_day: bool | None = None
    location: str | None = None
    description: str | None = None
    attendees: list[str] | None = None
    repeat: str | None = None          # an RRULE, like FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10
    reminder_minutes: int | None = None


Save = Callable[[dict[str, Any]], None]


class Provider:
    """One connected account. Subclasses set `kind` and implement the async methods."""
    kind = ""
    read_only = False

    def __init__(self, name: str, data: dict[str, Any], save: Save, tz: tzinfo,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.data = data          # what's saved in accounts/<name>.json
        self._save = save
        self.tz = tz
        self.http = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True,
                                      transport=transport)

    def save(self) -> None:
        self._save(self.data)

    async def whoami(self) -> str:
        return self.data.get("user", "") or self.name

    async def calendars(self) -> list[CalendarInfo]:
        raise NotImplementedError

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> list[Event]:
        raise NotImplementedError

    async def get(self, cal: CalendarInfo, event_id: str) -> Event:
        """One event by its id (for a repeating one, the series). Raises Missing if it's gone."""
        raise CalendarError(f"{self.name} can't look up single events.")

    async def create(self, cal: CalendarInfo, draft: Draft) -> Event:
        raise CalendarError(f"{self.name} is read-only.")

    async def update(self, cal: CalendarInfo, event_id: str, draft: Draft) -> Event:
        raise CalendarError(f"{self.name} is read-only.")

    async def delete(self, cal: CalendarInfo, event_id: str) -> None:
        raise CalendarError(f"{self.name} is read-only.")

    async def aclose(self) -> None:
        await self.http.aclose()


# ---- the source marker -----------------------------------------------------------------------------
def with_source(description: str, source: str) -> str:
    """Notes with a last line saying which add-on item the event is for, so it's never added twice."""
    body = "\n".join(line for line in (description or "").splitlines() if not line.startswith(SOURCE_PREFIX)).rstrip()
    if not source:
        return body
    return (body + "\n\n" if body else "") + SOURCE_PREFIX + source.strip()


def source_of(description: str) -> str:
    for line in reversed((description or "").splitlines()):
        if line.startswith(SOURCE_PREFIX):
            return line[len(SOURCE_PREFIX):].strip()
    return ""


def without_source(description: str) -> str:
    return with_source(description, "")


# ---- times -----------------------------------------------------------------------------------------
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text: str, tz: tzinfo, now: datetime | None = None) -> datetime | date:
    """A time the model gives: 2026-10-30, 2026-10-30T15:00, 2026-10-30 15:00-07:00, now, today, tomorrow,
    friday (the next one), or +3d / +2h / +1w from now. A date comes back as a date, everything else as an aware
    datetime in tz (naive times are tz's)."""
    raw = (text or "").strip()
    s = raw.lower()
    now = now or datetime.now(tz)
    if not s:
        raise CalendarError("No time given.")
    if s == "now":
        return now
    if s == "today":
        return now.date()
    if s == "tomorrow":
        return now.date() + timedelta(days=1)
    if len(s) >= 3 and any(w.startswith(s) for w in WEEKDAYS):
        target = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(s))
        return now.date() + timedelta(days=(target - now.weekday()) % 7 or 7)
    if m := re.fullmatch(r"\+(\d+)\s*([mhdw])", s):
        n, unit = int(m.group(1)), m.group(2)
        return now + {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n),
                      "w": timedelta(weeks=n)}[unit]
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return date.fromisoformat(s)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise CalendarError(f"Can't read the time {raw!r}. Use 2026-10-30 or 2026-10-30T15:00 (local time), "
                            "today, tomorrow, a weekday, or +2h / +3d.") from None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def as_datetime(value: datetime | date, tz: tzinfo) -> datetime:
    """Dates are midnight in tz; datetimes are moved into tz."""
    if isinstance(value, datetime):
        return value.astimezone(tz) if value.tzinfo else value.replace(tzinfo=tz)
    return datetime.combine(value, time(), tzinfo=tz)


def as_date(value: datetime | date, tz: tzinfo) -> date:
    return as_datetime(value, tz).date() if isinstance(value, datetime) else value


def overlaps(e: Event, start: datetime, end: datetime, tz: tzinfo) -> bool:
    return as_datetime(e.start, tz) < end and as_datetime(e.end, tz) > start


def describe_time(e: Event, tz: tzinfo) -> str:
    if e.all_day:
        first, last = as_date(e.start, tz), as_date(e.end, tz) - timedelta(days=1)
        if last <= first:
            return f"{first:%a %Y-%m-%d} all day"
        return f"{first:%a %Y-%m-%d} – {last:%a %Y-%m-%d} all day"
    s, t = as_datetime(e.start, tz), as_datetime(e.end, tz)
    if s.date() == t.date() or (t - s <= timedelta(hours=24) and t.time() == time()):
        return f"{s:%a %Y-%m-%d %H:%M}–{t:%H:%M}"
    return f"{s:%a %Y-%m-%d %H:%M} – {t:%a %Y-%m-%d %H:%M}"
