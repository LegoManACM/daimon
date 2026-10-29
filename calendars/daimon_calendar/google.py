"""Google Calendar, through its REST API (v3) with the user's own OAuth client."""
from __future__ import annotations

import urllib.parse
from datetime import date, datetime
from typing import Any

from .model import CalendarError, CalendarInfo, Missing, Draft, Event, as_datetime
from .oauth import OAuthProvider, loopback_sign_in

API = "https://www.googleapis.com/calendar/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/calendar"


def tz_name(tz) -> str:
    return getattr(tz, "key", "") or ""


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


class Google(OAuthProvider):
    kind = "google"
    token_url = TOKEN_URL

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.request(method, API + path, **kw)
        if r.status_code == 404:
            raise Missing("Google Calendar says that event or calendar doesn't exist (any more).")
        if r.status_code == 403:
            raise CalendarError(f"Google Calendar refused: {_reason(r)}")
        r.raise_for_status()
        return r.json() if r.content else None

    async def _pages(self, path: str, params: dict[str, Any]) -> list[dict]:
        out, token = [], None
        while True:
            body = await self._json("GET", path, params={**params, **({"pageToken": token} if token else {})})
            out += body.get("items", [])
            token = body.get("nextPageToken")
            if not token:
                return out

    async def calendars(self) -> list[CalendarInfo]:
        items = await self._pages("/users/me/calendarList", {"maxResults": 250})
        return [CalendarInfo(c["id"], c.get("summaryOverride") or c.get("summary") or c["id"],
                             writable=c.get("accessRole") in ("owner", "writer"), primary=bool(c.get("primary")))
                for c in items if not c.get("deleted")]

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> list[Event]:
        items = await self._pages(f"/calendars/{q(cal.id)}/events", {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": 2500})
        return [self._event(cal.id, e) for e in items if e.get("status") != "cancelled"]

    async def get(self, cal: CalendarInfo, event_id: str) -> Event:
        e = await self._json("GET", f"/calendars/{q(cal.id)}/events/{q(event_id)}")
        if e.get("status") == "cancelled":
            raise Missing("That event was deleted.")
        return self._event(cal.id, e)

    async def create(self, cal: CalendarInfo, draft: Draft) -> Event:
        body = await self._json("POST", f"/calendars/{q(cal.id)}/events", json=self._body(draft))
        return self._event(cal.id, body)

    async def update(self, cal: CalendarInfo, event_id: str, draft: Draft) -> Event:
        body = await self._json("PATCH", f"/calendars/{q(cal.id)}/events/{q(event_id)}", json=self._body(draft))
        return self._event(cal.id, body)

    async def delete(self, cal: CalendarInfo, event_id: str) -> None:
        await self._json("DELETE", f"/calendars/{q(cal.id)}/events/{q(event_id)}")

    # ---- mapping -----------------------------------------------------------------------------------
    def _when(self, value: datetime | date, all_day: bool, repeating: bool) -> dict[str, str]:
        if all_day:
            return {"date": (value.date() if isinstance(value, datetime) else value).isoformat()}
        out = {"dateTime": as_datetime(value, self.tz).isoformat()}
        if repeating and tz_name(self.tz):  # repeats follow a named zone through daylight saving
            out["timeZone"] = tz_name(self.tz)
        return out

    def _body(self, d: Draft) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if d.title is not None:
            body["summary"] = d.title
        if d.location is not None:
            body["location"] = d.location
        if d.description is not None:
            body["description"] = d.description
        all_day = bool(d.all_day)
        if d.start is not None:
            body["start"] = self._when(d.start, all_day, bool(d.repeat))
        if d.end is not None:
            body["end"] = self._when(d.end, all_day, bool(d.repeat))
        if d.attendees is not None:
            body["attendees"] = [{"email": a} for a in d.attendees]
        if d.repeat:
            body["recurrence"] = ["RRULE:" + d.repeat.removeprefix("RRULE:")]
        if d.reminder_minutes is not None:
            body["reminders"] = {"useDefault": False,
                                 "overrides": [{"method": "popup", "minutes": d.reminder_minutes}]}
        return body

    def _event(self, cal_id: str, e: dict[str, Any]) -> Event:
        def when(w: dict[str, str]) -> datetime | date:
            if "date" in w:
                return date.fromisoformat(w["date"])
            return datetime.fromisoformat(w["dateTime"].replace("Z", "+00:00")).astimezone(self.tz)
        start, end = e.get("start", {}), e.get("end", {})
        return Event(
            id=e["id"], calendar_id=cal_id, title=e.get("summary") or "(no title)",
            start=when(start), end=when(end or start), all_day="date" in start,
            location=e.get("location", ""), description=e.get("description", ""), url=e.get("htmlLink", ""),
            attendees=[a.get("displayName") or a.get("email", "") for a in e.get("attendees", []) if not a.get("self")],
            organizer=(e.get("organizer") or {}).get("email", ""), recurring=bool(e.get("recurringEventId")),
            busy=e.get("transparency") != "transparent", status="tentative" if e.get("status") == "tentative" else "")


def _reason(r) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}"


def sign_in(client_id: str, client_secret: str, say=print) -> dict[str, Any]:
    """The account data for a new Google account: the client and its token."""
    token = loopback_sign_in(AUTH_URL, TOKEN_URL, client_id, client_secret, SCOPE,
                             extra={"access_type": "offline", "prompt": "consent"}, say=say)
    return {"kind": "google", "client_id": client_id, "client_secret": client_secret, "token": token}
