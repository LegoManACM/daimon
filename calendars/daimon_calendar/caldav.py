"""CalDAV: Apple iCloud, Fastmail, Nextcloud, Yahoo, and any other standard calendar server. Also read-only .ics
feeds (a Canvas or school calendar feed, holidays, a shared calendar's "secret address").

CalDAV signs in with a username and an app password. The calendars are found from the server's address the
standard way (current-user-principal → calendar-home-set → its calendars) and remembered in the account file.
"""
from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import ical
from .model import CalendarError, CalendarInfo, Draft, Event, Missing, Provider

DAV, CAL = "DAV:", "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CAL}

# Servers that only need a username and app password; anything else takes its address.
PRESETS = {
    "apple": ("https://caldav.icloud.com/",
              "an app-specific password from account.apple.com → Sign-In and Security → App-Specific Passwords"),
    "fastmail": ("https://caldav.fastmail.com/dav/", "an app password from Fastmail → Settings → Privacy & Security"),
    "yahoo": ("https://caldav.calendar.yahoo.com/", "an app password from Yahoo → Account security"),
}

PROPFIND_PRINCIPAL = """<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>
</d:prop></d:propfind>"""
PROPFIND_HOME = """<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>
<c:calendar-home-set/></d:prop></d:propfind>"""
PROPFIND_CALENDARS = """<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:resourcetype/><d:displayname/><c:supported-calendar-component-set/><d:current-user-privilege-set/>
</d:prop></d:propfind>"""
REPORT_RANGE = """<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:time-range start="{start}" end="{end}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"""


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CalDAV(Provider):
    kind = "caldav"

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self.http.auth = httpx.BasicAuth(self.data.get("username", ""), self.data.get("password", ""))
        self.base = self.data["url"]

    async def _dav(self, method: str, url: str, body: str = "", depth: str = "0") -> ET.Element:
        r = await self.http.request(method, urllib.parse.urljoin(self.base, url), content=body.encode(),
                                    headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        if r.status_code == 401:
            raise CalendarError(f"{self.name}: the server didn't accept the username and app password.")
        r.raise_for_status()
        return ET.fromstring(r.content)

    async def _href(self, url: str, body: str, path: str) -> str:
        found = (await self._dav("PROPFIND", url, body)).find(f".//{path}/d:href", NS)
        return found.text.strip() if found is not None and found.text else ""

    async def home(self) -> str:
        if self.data.get("home"):
            return self.data["home"]
        principal = await self._href(self.base, PROPFIND_PRINCIPAL, "d:current-user-principal")
        if not principal:  # some servers only answer at the well-known address
            principal = await self._href("/.well-known/caldav", PROPFIND_PRINCIPAL, "d:current-user-principal")
        if not principal:
            raise CalendarError(f"{self.base} doesn't look like a CalDAV server (no current-user-principal).")
        home = await self._href(principal, PROPFIND_HOME, "c:calendar-home-set")
        if not home:
            raise CalendarError(f"{self.base} didn't say where the calendars are (no calendar-home-set).")
        # The home can be on another host (iCloud sends you to pXX-caldav.icloud.com).
        self.data["home"] = urllib.parse.urljoin(urllib.parse.urljoin(self.base, principal), home)
        self.save()
        return self.data["home"]

    async def calendars(self) -> list[CalendarInfo]:
        home = await self.home()
        out = []
        for resp in (await self._dav("PROPFIND", home, PROPFIND_CALENDARS, depth="1")).findall("d:response", NS):
            prop = resp.find(".//d:prop", NS)
            if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            comps = [c.get("name") for c in prop.findall("c:supported-calendar-component-set/c:comp", NS)]
            if comps and "VEVENT" not in comps:  # a reminders/tasks list, not a calendar
                continue
            privileges = {p.tag.split("}")[-1] for p in prop.findall("d:current-user-privilege-set/d:privilege/*", NS)}
            href = urllib.parse.urljoin(home, resp.findtext("d:href", "", NS).strip())
            name = (prop.findtext("d:displayname", "", NS) or "").strip() or href.rstrip("/").rsplit("/", 1)[-1]
            writable = not privileges or bool(privileges & {"write", "write-content", "all", "bind"})
            out.append(CalendarInfo(href, name, writable=writable))
        if out and not any(c.primary for c in out):
            next((c for c in out if c.writable), out[0]).primary = True
        return out

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> list[Event]:
        root = await self._dav("REPORT", cal.id, REPORT_RANGE.format(start=utc(start), end=utc(end)), depth="1")
        out = []
        for resp in root.findall("d:response", NS):
            data = resp.findtext(".//c:calendar-data", "", NS)
            if data.strip():
                href = urllib.parse.urljoin(cal.id, resp.findtext("d:href", "", NS).strip())
                out += ical.parse(data, cal.id, start, end, self.tz, event_id=href)
        return out

    async def create(self, cal: CalendarInfo, draft: Draft) -> Event:
        uid, doc = ical.new_document(draft, self.tz)
        href = urllib.parse.urljoin(cal.id if cal.id.endswith("/") else cal.id + "/", uid.split("@")[0] + ".ics")
        r = await self.http.put(href, content=doc, headers={"If-None-Match": "*",
                                                              "Content-Type": "text/calendar; charset=utf-8"})
        r.raise_for_status()
        return self._one(cal.id, href, doc)

    async def _fetch(self, event_id: str) -> httpx.Response:
        r = await self.http.get(event_id)
        if r.status_code in (404, 410):
            raise Missing("That event isn't on the calendar any more.")
        r.raise_for_status()
        return r

    async def get(self, cal: CalendarInfo, event_id: str) -> Event:
        return self._one(cal.id, event_id, (await self._fetch(event_id)).content)

    async def update(self, cal: CalendarInfo, event_id: str, draft: Draft) -> Event:
        r = await self._fetch(event_id)
        doc = ical.icalendar.Calendar.from_ical(r.content)
        ical.apply(ical.master(doc), draft, self.tz)
        out = doc.to_ical()
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if r.headers.get("ETag"):
            headers["If-Match"] = r.headers["ETag"]
        put = await self.http.put(event_id, content=out, headers=headers)
        if put.status_code == 412:
            raise CalendarError("That event changed on the server meanwhile. Look it up again and retry.")
        put.raise_for_status()
        return self._one(cal.id, event_id, out)

    async def delete(self, cal: CalendarInfo, event_id: str) -> None:
        r = await self.http.delete(event_id)
        if r.status_code not in (404, 410):
            r.raise_for_status()

    def _one(self, cal_id: str, href: str, doc: bytes) -> Event:
        return ical.to_event(ical.master(ical.icalendar.Calendar.from_ical(doc)), cal_id, href, self.tz)


class IcsFeed(Provider):
    """A read-only calendar from an .ics address (webcal:// works too)."""
    kind = "ics"
    read_only = True

    @property
    def url(self) -> str:
        url = self.data["url"]
        return "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url

    async def calendars(self) -> list[CalendarInfo]:
        return [CalendarInfo(self.url, self.data.get("title") or self.name, writable=False)]

    async def events(self, cal: CalendarInfo, start: datetime, end: datetime) -> list[Event]:
        r = await self.http.get(self.url)
        r.raise_for_status()
        try:
            return ical.parse(r.content, cal.id, start, end, self.tz)
        except ValueError as e:
            raise CalendarError(f"{self.name}'s address didn't give a calendar file ({e}).") from e


async def check_feed(url: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The feed's own name (X-WR-CALNAME) if it's a readable calendar; raises otherwise."""
    feed = IcsFeed("feed", {"url": url}, lambda _d: None, timezone.utc, transport)
    try:
        r = await feed.http.get(feed.url)
        r.raise_for_status()
        cal = ical.icalendar.Calendar.from_ical(r.content)
        now = datetime.now(timezone.utc)
        ical.parse(r.content, "", now - timedelta(days=1), now + timedelta(days=1), timezone.utc)
        return str(cal.get("X-WR-CALNAME", ""))
    finally:
        await feed.aclose()
