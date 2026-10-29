"""All the user's calendars as one: every connected account, short event ids, free time, and `source` keys.

A caller (the agent, or anything else calling the MCP tools) can tag an event with a source: any key it chooses,
like sms:<chat>:<message> or canvas:assignment:9:1. Adding with a source that's already on the calendar changes
that event instead of adding a second one, and update/delete can find an event by its source. The key is kept in
ids.json and also written as the last line of the event's notes, so it survives a lost ids.json.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx

from .caldav import CalDAV, IcsFeed
from .config import Home
from .google import Google
from .microsoft import Microsoft
from .model import (CalendarError, CalendarInfo, Draft, Event, Missing, Provider, as_date, as_datetime, overlaps,
                    with_source)

KINDS: dict[str, type[Provider]] = {"google": Google, "microsoft": Microsoft, "caldav": CalDAV, "ics": IcsFeed}


@dataclass
class Result:
    verb: str   # Added, Updated, Unchanged
    event: Event


def short_key(account: str, calendar_id: str, event_id: str) -> str:
    digest = hashlib.sha1(f"{account}\n{calendar_id}\n{event_id}".encode()).digest()
    return base64.b32encode(digest).decode()[:7].lower()


class Calendars:
    def __init__(self, home: Home, transport: httpx.AsyncBaseTransport | None = None):
        self.home = home
        self.cfg = home.config()
        self.tz = self.cfg.tz()
        self.providers: dict[str, Provider] = {}
        for name, data in home.accounts().items():
            cls = KINDS.get(data.get("kind", ""))
            if cls:
                self.providers[name] = cls(name, data, lambda d, n=name: home.save_account(n, d), self.tz, transport)
        self._calendars: list[CalendarInfo] | None = None
        self._account_problems: list[str] = []
        self.problems: list[str] = []  # accounts and calendars that couldn't be read on the last listing

    async def aclose(self) -> None:
        for p in self.providers.values():
            await p.aclose()

    # ---- ids.json ----------------------------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        if self.home.ids_path.exists():
            state = json.loads(self.home.ids_path.read_text(encoding="utf-8"))
        else:
            state = {}
        return {"keys": state.get("keys", {}), "sources": state.get("sources", {})}

    def _write_state(self, state: dict[str, Any]) -> None:
        tmp = self.home.ids_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.home.ids_path)

    def remember(self, events: list[Event]) -> None:
        """Gives each event its short key, and keeps the key → provider id mapping (and any sources) on disk."""
        state, changed = self._state(), False
        for e in events:
            e.key = short_key(e.account, e.calendar_id, e.id)
            if state["keys"].get(e.key) != [e.account, e.calendar_id, e.id]:
                state["keys"][e.key] = [e.account, e.calendar_id, e.id]
                changed = True
            if e.source and state["sources"].get(e.source) != e.key:
                state["sources"][e.source] = e.key
                changed = True
        if changed:
            self._write_state(state)

    def forget(self, key: str) -> None:
        state = self._state()
        state["keys"].pop(key, None)
        state["sources"] = {s: k for s, k in state["sources"].items() if k != key}
        self._write_state(state)

    def locate(self, event: str = "", source: str = "") -> tuple[Provider, CalendarInfo, str, str]:
        """(provider, calendar, provider's event id, short key) for a short id or a source key."""
        state = self._state()
        key = event.strip().lower() if event.strip() else state["sources"].get(source.strip(), "")
        if not key:
            raise Missing(f"No event has the source {source!r}." if source else "Give an event id or a source.")
        if key not in state["keys"]:
            raise Missing(f"No event with the id {key!r}. Look it up again with calendar_events.")
        account, cal_id, event_id = state["keys"][key]
        if account not in self.providers:
            raise CalendarError(f"The account {account!r} isn't connected any more.")
        cal = CalendarInfo(cal_id, cal_id, account=account)
        return self.providers[account], cal, event_id, key

    # ---- calendars ---------------------------------------------------------------------------------
    async def calendars(self, include_hidden: bool = False) -> list[CalendarInfo]:
        if self._calendars is None:
            if not self.providers:
                raise CalendarError("No calendar is connected yet. The user runs `daimon-calendar add` once.")
            found, self._account_problems = [], []
            results = await asyncio.gather(*(p.calendars() for p in self.providers.values()), return_exceptions=True)
            for (name, p), got in zip(self.providers.items(), results):
                if isinstance(got, BaseException):
                    self._account_problems.append(f"{name}: {problem(got)}")
                    continue
                for c in got:
                    c.account = name
                    c.writable = c.writable and not p.read_only
                found += got
            if not found and self._account_problems:
                raise CalendarError("Couldn't reach any calendar. " + "; ".join(self._account_problems))
            self._calendars = found
        if include_hidden:
            return self._calendars
        return [c for c in self._calendars if not self._hidden(c)]

    def _hidden(self, c: CalendarInfo) -> bool:
        hidden = {h.lower() for h in self.cfg.hidden}
        return c.name.lower() in hidden or f"{c.account}/{c.name}".lower() in hidden

    @staticmethod
    def label(c: CalendarInfo) -> str:
        return f"{c.account}/{c.name}"

    async def find_calendar(self, name: str) -> CalendarInfo:
        cals = await self.calendars(include_hidden=True)
        want = name.strip().lower()
        for test in (lambda c: self.label(c).lower() == want, lambda c: c.name.lower() == want,
                     lambda c: c.id == name, lambda c: c.account.lower() == want and c.primary,
                     lambda c: want in self.label(c).lower()):
            hits = [c for c in cals if test(c)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise CalendarError(f"{name!r} could be " + ", ".join(self.label(c) for c in hits) + ". Say which.")
        raise CalendarError(f"No calendar called {name!r}. There are: " + ", ".join(self.label(c) for c in cals))

    async def writable_calendar(self, name: str = "") -> CalendarInfo:
        name = name or self.cfg.default_calendar
        if name:
            cal = await self.find_calendar(name)
            if not cal.writable:
                raise CalendarError(f"{self.label(cal)} is read-only.")
            return cal
        cals = [c for c in await self.calendars(include_hidden=True) if c.writable]
        if not cals:
            raise CalendarError("None of the connected calendars can be written to.")
        return next((c for c in cals if c.primary), cals[0])

    # ---- reading -----------------------------------------------------------------------------------
    async def events(self, start: datetime, end: datetime, calendar: str = "", query: str = "") -> list[Event]:
        cals = [await self.find_calendar(calendar)] if calendar else await self.calendars()

        async def one(c: CalendarInfo) -> list[Event]:
            got = await self.providers[c.account].events(c, start, end)
            for e in got:
                e.account, e.calendar = c.account, c.name
            return got

        results = await asyncio.gather(*(one(c) for c in cals), return_exceptions=True)
        self.problems = list(self._account_problems)
        out: list[Event] = []
        for c, got in zip(cals, results):
            if isinstance(got, BaseException):
                self.problems.append(f"{self.label(c)}: {problem(got)}")
            else:
                out += [e for e in got if overlaps(e, start, end, self.tz)]
        if query:
            words = query.lower().split()
            out = [e for e in out if all(w in f"{e.title} {e.location} {e.description}".lower() for w in words)]
        out.sort(key=lambda e: (as_datetime(e.start, self.tz), not e.all_day, e.title))
        self.remember(out)
        return out

    async def free_time(self, start: datetime, end: datetime, minutes: int, calendar: str = "") \
            -> list[tuple[datetime, datetime]]:
        """Gaps of at least `minutes` inside the configured part of each day. All-day and 'free' events don't block."""
        busy = sorted((as_datetime(e.start, self.tz), as_datetime(e.end, self.tz))
                      for e in await self.events(start, end, calendar) if e.busy and not e.all_day)
        day_start, day_end = time.fromisoformat(self.cfg.day_start), time.fromisoformat(self.cfg.day_end)
        need, slots = timedelta(minutes=max(5, minutes)), []
        day = start.date()
        while day <= end.date():
            lo = max(start, datetime.combine(day, day_start, tzinfo=self.tz))
            hi = min(end, datetime.combine(day, day_end, tzinfo=self.tz))
            for b0, b1 in busy:
                if b1 <= lo or b0 >= hi:
                    continue
                if b0 - lo >= need:
                    slots.append((lo, b0))
                lo = max(lo, b1)
            if hi - lo >= need:
                slots.append((lo, hi))
            day += timedelta(days=1)
        return slots

    # ---- writing -----------------------------------------------------------------------------------
    def complete(self, draft: Draft, base: Event | None = None) -> Draft:
        """Fills in what a draft implies: all-day from a date-only start, an end from the start, and when changing
        an existing event's start alone, the same length as before."""
        d = replace(draft)
        if d.all_day is None and d.start is not None:
            d.all_day = not isinstance(d.start, datetime)
        if d.all_day is None and d.end is not None:
            d.all_day = not isinstance(d.end, datetime)
        if d.all_day is None and base is not None and (d.start is not None or d.end is not None):
            d.all_day = base.all_day
        if d.all_day and d.start is not None:
            d.start = as_date(d.start, self.tz)
        if d.all_day and d.end is not None:
            d.end = as_date(d.end, self.tz)
        if d.start is not None and d.end is None:
            if base is not None:
                d.end = d.start + (as_datetime(base.end, self.tz) - as_datetime(base.start, self.tz)) \
                    if not d.all_day else d.start + (as_date(base.end, self.tz) - as_date(base.start, self.tz))
            else:
                d.end = d.start + (timedelta(days=1) if d.all_day else timedelta(minutes=self.cfg.default_minutes))
        if d.all_day and d.start is not None and d.end is not None and d.end <= d.start:
            d.end = d.start + timedelta(days=1)  # "the 30th to the 30th" means the one day
        if d.start is not None and d.end is not None and \
                as_datetime(d.end, self.tz) < as_datetime(d.start, self.tz):
            raise CalendarError("The end is before the start.")
        return d

    async def add(self, draft: Draft, calendar: str = "", source: str = "") -> Result:
        if not (draft.title or "").strip():
            raise CalendarError("A new event needs a title.")
        if draft.start is None:
            raise CalendarError("A new event needs a start (like 2026-10-30T15:00, or 2026-10-30 for all day).")
        if source:
            try:
                return await self.update(draft, source=source)
            except Missing:
                pass  # never added, or deleted since: add it afresh
        cal = await self.writable_calendar(calendar)
        d = self.complete(draft)
        d.description = with_source(d.description or "", source) if source else d.description
        event = await self.providers[cal.account].create(cal, d)
        event.account, event.calendar = cal.account, cal.name
        self.remember([event])
        return Result("Added", event)

    async def update(self, draft: Draft, event: str = "", source: str = "") -> Result:
        provider, cal, event_id, key = self.locate(event, source)
        try:
            current = await provider.get(cal, event_id)
        except Missing:
            self.forget(key)
            raise
        d = self.complete(draft, current)
        keep = source or current.source
        if d.description is not None or keep != current.source:
            d.description = with_source(d.description if d.description is not None else current.description, keep)
        if same(current, d, self.tz):
            verb, updated = "Unchanged", current
        else:
            verb, updated = "Updated", await provider.update(cal, event_id, d)
        cal = next((c for c in await self.calendars(include_hidden=True)
                    if c.account == cal.account and c.id == cal.id), cal)
        updated.account, updated.calendar = cal.account, cal.name
        self.remember([updated])
        return Result(verb, updated)

    async def delete(self, event: str = "", source: str = "") -> Event | None:
        """Deletes it; returns what it was (None if it was already gone)."""
        provider, cal, event_id, key = self.locate(event, source)
        try:
            current = await provider.get(cal, event_id)
        except Missing:
            self.forget(key)
            return None
        await provider.delete(cal, event_id)
        self.forget(key)
        return current


def same(e: Event, d: Draft, tz) -> bool:
    """Whether writing the draft would change nothing the draft sets."""
    def moment(v: datetime | date) -> Any:
        return as_date(v, tz) if not isinstance(v, datetime) else as_datetime(v, tz)
    checks = [
        d.title is None or d.title == e.title,
        d.location is None or d.location == e.location,
        d.description is None or d.description.strip() == e.description.strip(),
        d.all_day is None or d.all_day == e.all_day,
        d.start is None or moment(d.start) == moment(e.start),
        d.end is None or moment(d.end) == moment(e.end),
        d.attendees is None, d.repeat is None, d.reminder_minutes is None,  # not compared: always written
    ]
    return all(checks)


def problem(e: BaseException) -> str:
    if isinstance(e, CalendarError):
        return str(e)
    if isinstance(e, httpx.HTTPStatusError):
        return f"the server answered {e.response.status_code}"
    if isinstance(e, httpx.HTTPError):
        return f"couldn't reach it ({type(e).__name__})"
    return f"{type(e).__name__}: {e}"

