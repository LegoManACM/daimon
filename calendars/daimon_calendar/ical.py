"""iCalendar (.ics) in and out, for CalDAV servers and subscribed feeds. Repeats are expanded here."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, tzinfo

import icalendar
import recurring_ical_events

from .model import Draft, Event, as_date, as_datetime


def parse(text: str | bytes, calendar_id: str, start: datetime, end: datetime, tz: tzinfo,
          event_id: str = "") -> list[Event]:
    """The events of an .ics document that fall between start and end, repeats expanded.
    event_id names them all (a CalDAV href); without it each gets its UID."""
    cal = icalendar.Calendar.from_ical(text)
    # The expanded occurrences all carry a RECURRENCE-ID, so whether one repeats is read off the original.
    repeating = {str(c.get("UID", "")) for c in cal.walk("VEVENT") if {"RRULE", "RDATE", "RECURRENCE-ID"} & set(c)}
    out = []
    for v in recurring_ical_events.of(cal, skip_bad_series=True).between(start, end):
        if str(v.get("STATUS", "")).upper() == "CANCELLED":
            continue
        e = to_event(v, calendar_id, event_id or str(v.get("UID", "")), tz)
        e.recurring = str(v.get("UID", "")) in repeating
        out.append(e)
    return out


def to_event(v: icalendar.Event, calendar_id: str, event_id: str, tz: tzinfo) -> Event:
    s = v.decoded("DTSTART")
    all_day = not isinstance(s, datetime)
    if "DTEND" in v:
        e = v.decoded("DTEND")
    elif "DURATION" in v:
        e = s + v.decoded("DURATION")
    else:
        e = s + (timedelta(days=1) if all_day else timedelta())
    if not all_day:
        s, e = as_datetime(s, tz), as_datetime(e, tz)
    attendees = v.get("ATTENDEE") or []
    attendees = attendees if isinstance(attendees, list) else [attendees]
    status = str(v.get("STATUS", "")).lower()
    return Event(
        id=event_id, calendar_id=calendar_id, title=str(v.get("SUMMARY", "")) or "(no title)", start=s, end=e,
        all_day=all_day, location=str(v.get("LOCATION", "")), description=str(v.get("DESCRIPTION", "")),
        url=str(v.get("URL", "")),
        attendees=[a.params.get("CN") or str(a).removeprefix("mailto:").removeprefix("MAILTO:") for a in attendees],
        organizer=str(v.get("ORGANIZER", "")).removeprefix("mailto:").removeprefix("MAILTO:"),
        recurring="RRULE" in v or "RECURRENCE-ID" in v,
        busy=str(v.get("TRANSP", "OPAQUE")).upper() != "TRANSPARENT",
        status="tentative" if status == "tentative" else "")


def new_document(draft: Draft, tz: tzinfo) -> tuple[str, bytes]:
    """A fresh VCALENDAR with one VEVENT. Returns (uid, ics)."""
    uid = f"{uuid.uuid4()}@daimon"
    ev = icalendar.Event()
    ev.add("UID", uid)
    ev.add("DTSTAMP", datetime.now(tz))
    apply(ev, draft, tz)
    return uid, wrap(ev)


def wrap(ev: icalendar.Event) -> bytes:
    cal = icalendar.Calendar()
    cal.add("PRODID", "-//Daimon//daimon-calendar//EN")
    cal.add("VERSION", "2.0")
    cal.add_component(ev)
    return cal.to_ical()


def master(cal: icalendar.Calendar) -> icalendar.Event:
    """The series (or the only event) in a CalDAV resource: the VEVENT without a RECURRENCE-ID."""
    events = [c for c in cal.walk("VEVENT")]
    return next((c for c in events if "RECURRENCE-ID" not in c), events[0])


def apply(ev: icalendar.Event, d: Draft, tz: tzinfo) -> None:
    """Writes a draft's fields onto a VEVENT, leaving the ones it doesn't set."""
    def put(key: str, value) -> None:
        ev.pop(key, None)
        if value not in (None, "", []):
            ev.add(key, value)

    if d.title is not None:
        put("SUMMARY", d.title)
    if d.location is not None:
        put("LOCATION", d.location)
    if d.description is not None:
        put("DESCRIPTION", d.description)
    for key, value in (("DTSTART", d.start), ("DTEND", d.end)):
        if value is not None:
            put(key, as_date(value, tz) if d.all_day else as_datetime(value, tz))
    if d.start is not None or d.end is not None:
        ev.pop("DURATION", None)
    if d.attendees is not None:
        ev.pop("ATTENDEE", None)
        for a in d.attendees:
            ev.add("ATTENDEE", f"mailto:{a}")
    if d.repeat:
        ev.pop("RRULE", None)
        ev.add("RRULE", icalendar.vRecur.from_ical(d.repeat.removeprefix("RRULE:")))
    if d.reminder_minutes is not None:
        ev.subcomponents = [c for c in ev.subcomponents if c.name != "VALARM"]
        alarm = icalendar.Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", "Reminder")
        alarm.add("TRIGGER", timedelta(minutes=-d.reminder_minutes))
        ev.add_component(alarm)
    ev.pop("DTSTAMP", None)
    ev.add("DTSTAMP", datetime.now(tz))
    ev.pop("LAST-MODIFIED", None)
    ev.add("LAST-MODIFIED", datetime.now(tz))


def is_date_only(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)
