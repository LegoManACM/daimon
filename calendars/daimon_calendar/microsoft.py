"""Outlook / Microsoft 365 calendars, through Microsoft Graph with the user's own app registration."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .model import CalendarError, CalendarInfo, Missing, Draft, Event, as_date, as_datetime
from .oauth import OAuthProvider, device_code_sign_in

API = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "offline_access User.Read Calendars.ReadWrite"
PREFER = 'outlook.timezone="UTC", outlook.body-content-type="text"'
DAYS = {"MO": "monday", "TU": "tuesday", "WE": "wednesday", "TH": "thursday", "FR": "friday", "SA": "saturday",
        "SU": "sunday"}
INDEX = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}


class Microsoft(OAuthProvider):
    kind = "microsoft"

    @property
    def token_url(self) -> str:  # type: ignore[override]
        return f"{LOGIN}/{self.data.get('tenant', 'common')}/oauth2/v2.0/token"

    def token_params(self) -> dict[str, str]:
        return {"scope": SCOPE}

    async def _json(self, method: str, url: str, **kw: Any) -> Any:
        r = await self.request(method, url if url.startswith("http") else API + url,
                               headers={"Prefer": PREFER}, **kw)
        if r.status_code == 404:
            raise Missing("Outlook says that event or calendar doesn't exist (any more).")
        if r.status_code == 403:
            raise CalendarError(f"Outlook refused: {_reason(r)}")
        r.raise_for_status()
        return r.json() if r.content else None

    async def _pages(self, url: str, params: dict[str, Any] | None = None) -> list[dict]:
        out = []
        body = await self._json("GET", url, params=params)
        while True:
            out += body.get("value", [])
            if not body.get("@odata.nextLink"):
                return out
            body = await self._json("GET", body["@odata.nextLink"])

    async def whoami(self) -> str:
        me = await self._json("GET", "/me")
        return me.get("mail") or me.get("userPrincipalName") or self.name

    async def calendars(self) -> list[CalendarInfo]:
        return [CalendarInfo(c["id"], c.get("name") or "Calendar", writable=bool(c.get("canEdit", True)),
                             primary=bool(c.get("isDefaultCalendar")))
                for c in await self._pages("/me/calendars")]

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> list[Event]:
        utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
        items = await self._pages(f"/me/calendars/{cal.id}/calendarView",
                                  {"startDateTime": utc(start), "endDateTime": utc(end), "$top": 500})
        return [self._event(cal.id, e) for e in items if not e.get("isCancelled")]

    async def get(self, cal: CalendarInfo, event_id: str) -> Event:
        return self._event(cal.id, await self._json("GET", f"/me/events/{event_id}"))

    async def create(self, cal: CalendarInfo, draft: Draft) -> Event:
        return self._event(cal.id, await self._json("POST", f"/me/calendars/{cal.id}/events", json=self._body(draft)))

    async def update(self, cal: CalendarInfo, event_id: str, draft: Draft) -> Event:
        return self._event(cal.id, await self._json("PATCH", f"/me/events/{event_id}", json=self._body(draft)))

    async def delete(self, cal: CalendarInfo, event_id: str) -> None:
        await self._json("DELETE", f"/me/events/{event_id}")

    # ---- mapping -----------------------------------------------------------------------------------
    def _when(self, value: datetime | date, all_day: bool) -> dict[str, str]:
        if all_day:  # midnight to midnight, in a zone that keeps it on that date
            return {"dateTime": as_date(value, self.tz).isoformat() + "T00:00:00", "timeZone": "UTC"}
        return {"dateTime": as_datetime(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}

    def _body(self, d: Draft) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if d.title is not None:
            body["subject"] = d.title
        if d.location is not None:
            body["location"] = {"displayName": d.location}
        if d.description is not None:
            body["body"] = {"contentType": "text", "content": d.description}
        all_day = bool(d.all_day)
        if d.all_day is not None:
            body["isAllDay"] = all_day
        if d.start is not None:
            body["start"] = self._when(d.start, all_day)
        if d.end is not None:
            body["end"] = self._when(d.end, all_day)
        if d.attendees is not None:
            body["attendees"] = [{"emailAddress": {"address": a}, "type": "required"} for a in d.attendees]
        if d.repeat:
            if d.start is None:
                raise CalendarError("Give the start time again along with the repeat rule.")
            body["recurrence"] = recurrence(d.repeat, as_date(d.start, self.tz))
        if d.reminder_minutes is not None:
            body["isReminderOn"] = True
            body["reminderMinutesBeforeStart"] = d.reminder_minutes
        return body

    def _event(self, cal_id: str, e: dict[str, Any]) -> Event:
        all_day = bool(e.get("isAllDay"))

        def when(w: dict[str, str]) -> datetime | date:
            dt = datetime.fromisoformat(w["dateTime"][:19]).replace(tzinfo=timezone.utc)  # Prefer: UTC
            return dt.date() if all_day else dt.astimezone(self.tz)
        start = when(e["start"])
        end = when(e.get("end") or e["start"])
        if all_day and end <= start:
            end = start + timedelta(days=1)
        attendees = [(a.get("emailAddress") or {}) for a in e.get("attendees", [])]
        return Event(
            id=e["id"], calendar_id=cal_id, title=e.get("subject") or "(no title)", start=start, end=end,
            all_day=all_day, location=(e.get("location") or {}).get("displayName", ""),
            description=((e.get("body") or {}).get("content") or e.get("bodyPreview") or "").strip(),
            url=e.get("webLink", ""), attendees=[a.get("name") or a.get("address", "") for a in attendees],
            organizer=((e.get("organizer") or {}).get("emailAddress") or {}).get("address", ""),
            recurring=e.get("type") in ("occurrence", "exception"),
            busy=e.get("showAs") not in ("free", "workingElsewhere"),
            status="tentative" if e.get("showAs") == "tentative" else "")


def recurrence(rule: str, start: date) -> dict[str, Any]:
    """An RRULE as Graph's patternedRecurrence. Covers daily, weekly, monthly (by date or 2nd Tuesday) and yearly."""
    parts = dict(p.split("=", 1) for p in rule.removeprefix("RRULE:").upper().split(";") if "=" in p)
    freq, interval = parts.get("FREQ", ""), int(parts.get("INTERVAL", 1))
    pattern: dict[str, Any] = {"interval": interval}
    byday = [d for d in parts.get("BYDAY", "").split(",") if d]
    if freq == "DAILY":
        pattern["type"] = "daily"
    elif freq == "WEEKLY":
        days = [DAYS[d[-2:]] for d in byday] or [list(DAYS.values())[start.weekday()]]
        pattern |= {"type": "weekly", "daysOfWeek": days, "firstDayOfWeek": "sunday"}
    elif freq == "MONTHLY" and byday:
        n = int(byday[0][:-2] or parts.get("BYSETPOS", 1))
        if n not in INDEX:
            raise CalendarError(f"Outlook can't repeat on that day of the month ({rule}).")
        pattern |= {"type": "relativeMonthly", "daysOfWeek": [DAYS[byday[0][-2:]]], "index": INDEX[n]}
    elif freq == "MONTHLY":
        pattern |= {"type": "absoluteMonthly", "dayOfMonth": int(parts.get("BYMONTHDAY", start.day))}
    elif freq == "YEARLY":
        pattern |= {"type": "absoluteYearly", "dayOfMonth": start.day, "month": start.month}
    else:
        raise CalendarError(f"Can't repeat with {rule!r}: use FREQ=DAILY, WEEKLY, MONTHLY or YEARLY.")
    rng: dict[str, Any] = {"startDate": start.isoformat()}
    if "COUNT" in parts:
        rng |= {"type": "numbered", "numberOfOccurrences": int(parts["COUNT"])}
    elif "UNTIL" in parts:
        until = parts["UNTIL"][:8]
        rng |= {"type": "endDate", "endDate": f"{until[:4]}-{until[4:6]}-{until[6:8]}"}
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


def _reason(r) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}"


def sign_in(client_id: str, tenant: str = "common", say=print) -> dict[str, Any]:
    """The account data for a new Microsoft account: the app registration and its token."""
    token = device_code_sign_in(f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode", f"{LOGIN}/{tenant}/oauth2/v2.0/token",
                                client_id, SCOPE, say=say)
    return {"kind": "microsoft", "client_id": client_id, "tenant": tenant, "token": token}
