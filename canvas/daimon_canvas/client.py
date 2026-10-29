"""The Canvas REST API, as the student sees it: planner items, courses, modules, pages, files, announcements.

Everything here only reads.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

import httpx
from mcp.server.mcpserver.exceptions import ToolError

USER_AGENT = "daimon-canvas/0.1"


class CanvasError(ToolError):
    """Something to tell the model as is: a bad id, a locked item, an expired token. The MCP server passes a
    ToolError's text on to the client."""


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


WORK_TYPES = {"assignment", "quiz", "discussion_topic", "wiki_page"}
COURSES_TTL = 3600  # the active course list barely changes; cache it so every lookup isn't two requests
# Planner type -> the short kind used in ids like "quiz:10:2", and the API collection it lives in.
KINDS = {"assignment": "assignment", "quiz": "quiz", "discussion_topic": "discussion", "wiki_page": "page"}
ENDPOINTS = {"assignment": "assignments", "quiz": "quizzes", "discussion": "discussion_topics", "page": "pages"}
URL_KINDS = {v: k for k, v in ENDPOINTS.items()}
# Module item type -> the kind used in ids. Links and external tools have no id: they aren't Canvas content.
ITEM_KINDS = {"Assignment": "assignment", "Quiz": "quiz", "Discussion": "discussion", "Page": "page", "File": "file",
              "ExternalUrl": "link", "ExternalTool": "tool", "SubHeader": "header"}


@dataclass
class Assignment:
    id: str
    title: str
    course: str
    due: datetime | None
    kind: str
    submitted: bool
    missing: bool
    url: str
    done: bool = False  # submitted, graded, or ticked off in the Canvas planner
    course_id: str = ""
    item_id: str = ""
    points: float | None = None

    @property
    def ref(self) -> str:
        """The id the model passes back to canvas_open."""
        return f"{KINDS.get(self.kind, self.kind)}:{self.course_id}:{self.item_id}"


@dataclass
class CanvasFile:
    name: str
    size: int
    url: str
    error: str = ""  # set when the file is listed but can't be downloaded
    id: str = ""
    folder: str = ""  # where it sits in the course's Files, e.g. "Lectures/Week 1"


@dataclass
class AssignmentDetails:
    kind: str
    title: str
    course: str
    url: str
    facts: list[str]  # due date, points, status... one short line each
    text: str  # the instructions, with links kept as "text (url)"
    rubric: list[str] = field(default_factory=list)
    files: list[CanvasFile] = field(default_factory=list)


@dataclass
class ModuleItem:
    title: str
    kind: str  # assignment, quiz, discussion, page, file, link, tool or header
    ref: str  # the id for canvas_open; empty for links, tools and headers
    url: str = ""  # where a link points
    locked: bool = False


@dataclass
class Module:
    name: str
    items: list[ModuleItem]
    locked: bool = False


def item_ref(it: dict, course_id: str) -> str:
    """The canvas_open id of a module item, or "" when it isn't Canvas content."""
    kind = ITEM_KINDS.get(it.get("type", ""))
    if kind == "page":
        return f"page:{course_id}:{it['page_url']}" if it.get("page_url") else ""
    if kind in ("assignment", "quiz", "discussion", "file") and it.get("content_id"):
        return f"{kind}:{course_id}:{it['content_id']}"
    return ""


def parse_module_item(it: dict, course_id: str) -> ModuleItem:
    return ModuleItem(
        title=" ".join((it.get("title") or "(untitled)").split()),
        kind=ITEM_KINDS.get(it.get("type", ""), "other"),
        ref=item_ref(it, course_id),
        url=it.get("external_url") or "",
        locked=bool((it.get("content_details") or {}).get("locked_for_user")),
    )


def course_name(c: dict) -> str:
    return " ".join((c.get("name") or c.get("course_code") or f"course {c.get('id')}").split())


def has_words(query: str, text: str) -> bool:
    text = text.lower()
    return all(w in text for w in query.lower().split())


def parse_ref(ref: str) -> tuple[str, str, str]:
    """(kind, course_id, item_id) from an id like 'quiz:10:2' or a Canvas link. Raises CanvasError if it's neither.

    Besides the work kinds there's file (course_id may be ""), syllabus (no item_id) and module_item, a link to a
    module entry that still has to be looked up."""
    ref = ref.strip().strip("[]()").removeprefix("id").strip(" :")
    m = re.search(r"/courses/(\d+)/assignments/syllabus", ref) or re.fullmatch(r"syllabus\s*:\s*(\d+)", ref, re.I)
    if m:
        return "syllabus", m.group(1), ""
    m = re.search(r"/courses/(\d+)/modules/items/(\d+)", ref)
    if m:
        return "module_item", m.group(1), m.group(2)
    m = re.search(r"(?:/courses/(\d+))?/files/(\d+)", ref) or re.fullmatch(r"file\s*:\s*()(\d+)", ref, re.I)
    if m:
        return "file", m.group(1) or "", m.group(2)
    m = re.search(r"/courses/(\d+)/(assignments|quizzes|discussion_topics|pages)/([^/?#\s]+)", ref)
    if m:
        return URL_KINDS[m.group(2)], m.group(1), m.group(3)
    m = re.fullmatch(r"(?:(assignment|quiz|discussion|page|file)\s*:\s*)?(\d+)\s*:\s*([\w-]+)", ref, re.I)
    if m:
        return (m.group(1) or "assignment").lower(), m.group(2), m.group(3)
    raise CanvasError(f"'{ref}' isn't an assignment id.")


def html_to_text(body: str, base_url: str = "") -> str:
    """Canvas HTML as plain text, keeping where each link goes."""

    def link(m: re.Match) -> str:
        href, label = html.unescape(m.group(1)), strip_html(m.group(2))
        if href.startswith("/"):
            href = base_url + href
        return label if not href or href in label else f"{label} ({href})" if label else href

    body = re.sub(r'(?is)<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', link, body or "")
    return strip_html(body)


def file_links(body: str) -> dict[str, str]:
    """Canvas files linked from a description, id -> link text (not inline images, which are only decoration)."""
    links: dict[str, str] = {}
    for m in re.finditer(r'(?is)<a\b[^>]*?href="[^"]*?/files/(\d+)[^>]*>(.*?)</a>', body or ""):
        links.setdefault(m.group(1), strip_html(m.group(2)))
    return links


def parse_planner(items: list[dict], tz: tzinfo, base_url: str = "") -> list[Assignment]:
    out = []
    for it in items:
        kind = it.get("plannable_type", "")
        if kind not in WORK_TYPES:
            continue
        p = it.get("plannable") or {}
        due_raw = p.get("due_at") or p.get("todo_date") or it.get("plannable_date")
        due = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(tz) if due_raw else None
        subs = it.get("submissions") if isinstance(it.get("submissions"), dict) else {}
        override = it.get("planner_override") or {}
        url = it.get("html_url") or ""
        course_id = it.get("course_id") or (re.search(r"/courses/(\d+)", url) or [None, ""])[1]
        out.append(
            Assignment(
                id=f"{kind}:{it.get('plannable_id')}",
                title=p.get("title") or p.get("name") or "(untitled)",
                course=" ".join((it.get("context_name") or "").split()),
                due=due,
                kind=kind,
                submitted=bool(subs.get("submitted")),
                missing=bool(subs.get("missing")),
                url=base_url + url if url.startswith("/") else url,
                done=bool(subs.get("submitted") or subs.get("graded") or override.get("marked_complete")),
                course_id=str(course_id or ""),
                item_id=str(it.get("plannable_id") or ""),
                points=p.get("points_possible"),
            )
        )
    out.sort(key=lambda a: a.due or datetime.max.replace(tzinfo=timezone.utc))
    return out


def format_assignment(a: Assignment, show_id: bool = False) -> str:
    when = f"{a.due:%a %m-%d %H:%M}" if a.due else "no due date"
    status = " (done)" if a.done else " (missing)" if a.missing else ""
    if show_id and a.course_id:
        return f"{when} | {a.course} | {a.title}{status} | id {a.ref}"
    kind = "" if a.kind == "assignment" else f" [{a.kind.replace('_topic', '')}]"
    return f"{when} | {a.course} | {a.title}{kind}{status}"


class CanvasClient:
    def __init__(self, base_url: str, token: str, tz: tzinfo, transport: httpx.AsyncBaseTransport | None = None,
                 max_file_mb: int = 25):
        self.base = base_url.rstrip("/")
        self.tz = tz
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self.http = httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {token}"},
                                      timeout=30.0, follow_redirects=True, transport=transport)
        self._courses: tuple[float, list[dict]] | None = None

    async def _get_all(self, path: str, params: dict | None = None, max_pages: int = 10, quiet: bool = False):
        """Every page of a list. With quiet, a list the student can't see (a hidden Files tab...) gives None."""
        url: str | None = path if path.startswith("http") else f"{self.base}/api/v1/{path}"
        out: list = []
        for _ in range(max_pages):
            r = await self.http.get(url, params=params)
            if r.status_code == 401 and (not quiet or "www-authenticate" in r.headers):
                raise CanvasError("Canvas rejected the API token (401). It may have expired.")
            if quiet and r.status_code in (401, 403, 404):
                return None
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return data
            out.extend(data)
            url = r.links.get("next", {}).get("url")
            if not url:
                break
            params = None  # the next link already carries the query
        return out

    async def assignments(self, start: datetime, end: datetime) -> list[Assignment]:
        """Planner items (assignments, quizzes, discussions...) from active courses only."""
        codes = [f"course_{c['id']}" for c in await self.active_courses()]
        if not codes:
            return []
        items = await self._get_all(
            "planner/items",
            {
                "start_date": start.astimezone(timezone.utc).isoformat(),
                "end_date": end.astimezone(timezone.utc).isoformat(),
                "context_codes[]": codes,
                "per_page": 100,
            },
        )
        return parse_planner(items, self.tz, self.base)

    async def graded(self, since: datetime) -> list[dict]:
        """The student's submissions graded since `since` in active courses, with their assignment, newest first.
        A grade Canvas hasn't released to the student has no score or grade, so it's left out."""
        out = []
        for c in await self.active_courses():
            cid = str(c["id"])
            subs = await self._get_all(f"courses/{cid}/students/submissions", {
                "student_ids[]": "self", "include[]": "assignment", "workflow_state": "graded",
                "graded_since": since.astimezone(timezone.utc).isoformat(), "per_page": 100,
            }, quiet=True) or []
            for s in subs:
                a = s.get("assignment") or {}
                if s.get("score") is None and not s.get("grade"):
                    continue
                graded_at = s.get("graded_at")
                url = a.get("html_url") or ""
                out.append({
                    "course": course_name(c), "course_id": cid,
                    "assignment_id": f"assignment:{cid}:{s.get('assignment_id') or a.get('id')}",
                    "title": " ".join((a.get("name") or "(untitled)").split()),
                    "score": s.get("score"), "grade": s.get("grade") or "", "points": a.get("points_possible"),
                    "url": self.base + url if url.startswith("/") else url,
                    "graded_at": datetime.fromisoformat(graded_at.replace("Z", "+00:00")).astimezone(self.tz)
                    if graded_at else None,
                })
        return sorted(out, key=lambda g: g["graded_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    async def courses(self) -> list[dict]:
        courses = await self._get_all("courses", {"enrollment_state": "active", "include[]": "total_scores", "per_page": 100})
        courses = [c for c in courses if isinstance(c, dict) and "id" in c and not c.get("access_restricted_by_date")]
        self._courses = (time.monotonic(), courses)
        return courses

    async def active_courses(self) -> list[dict]:
        if self._courses and time.monotonic() - self._courses[0] < COURSES_TTL:
            return self._courses[1]
        return await self.courses()

    async def announcements(self, days_back: int, course: str = "") -> list[dict]:
        names = {f"course_{c['id']}": " ".join(c.get("name", "").split()) for c in await self.active_courses()}
        if course:
            names = {k: v for k, v in names.items() if course.lower() in v.lower()}
        if not names:
            return []
        start = datetime.now(self.tz) - timedelta(days=days_back)
        items = await self._get_all(
            "announcements",
            {"context_codes[]": list(names), "start_date": start.date().isoformat(), "per_page": 50},
        )
        items = sorted(items, key=lambda a: a.get("posted_at") or "", reverse=True)
        return [
            {
                "course": names.get(a.get("context_code"), ""),
                "course_id": str(a.get("context_code") or "").removeprefix("course_"),
                "id": str(a.get("id") or ""),
                "title": a.get("title", ""),
                "author": (a.get("author") or {}).get("display_name") or a.get("user_name") or "",
                "posted": a.get("posted_at") or "",
                "url": a.get("html_url") or a.get("url") or "",
                "text": strip_html(a.get("message") or ""),
            }
            for a in items
        ]

    def when(self, raw: str | None) -> datetime | None:
        """A Canvas timestamp (UTC, 'Z') as local time."""
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(self.tz) if raw else None

    def link(self, url: str) -> str:
        return self.base + url if url.startswith("/") else url

    async def conversations(self, unread_only: bool = False) -> list[dict]:
        """The Canvas inbox, newest first: one entry per conversation. Listing doesn't mark anything read."""
        params = {"per_page": 50} | ({"scope": "unread"} if unread_only else {})
        convs = await self._get_all("conversations", params, max_pages=1, quiet=True) or []
        return [{
            "id": str(c.get("id")), "subject": " ".join((c.get("subject") or "(no subject)").split()),
            "course": " ".join((c.get("context_name") or "").split()),
            "with": [p.get("name", "") for p in c.get("participants") or []],
            "last_message": strip_html(c.get("last_message") or ""),
            "last_at": self.when(c.get("last_message_at")),
            "last_mine_at": self.when(c.get("last_authored_message_at")),
            "unread": c.get("workflow_state") == "unread", "count": c.get("message_count") or 0,
        } for c in convs if isinstance(c, dict) and c.get("id")]

    async def conversation(self, conversation_id: str) -> dict:
        """One conversation with its messages, newest first. It stays unread on Canvas."""
        c = await self._get_one(f"conversations/{conversation_id}", {"auto_mark_as_read": "false"})
        if c is None:
            raise CanvasError(f"No Canvas conversation {conversation_id}. canvas_inbox lists them.")
        names = {str(p.get("id")): p.get("name", "") for p in c.get("participants") or []}
        return {
            "id": str(c.get("id")), "subject": " ".join((c.get("subject") or "(no subject)").split()),
            "course": " ".join((c.get("context_name") or "").split()),
            "messages": [{
                "id": str(m.get("id")), "author_id": str(m.get("author_id")),
                "from": names.get(str(m.get("author_id")), "someone"), "at": self.when(m.get("created_at")),
                "text": strip_html(m.get("body") or ""),
                "files": [a.get("display_name", "") for a in m.get("attachments") or []],
            } for m in c.get("messages") or []],
        }

    async def course_events(self, start: datetime, end: datetime, course: str = "") -> list[dict]:
        """Events the classes put on their Canvas calendars (lectures, exams, field trips), not assignments."""
        names = {f"course_{c['id']}": course_name(c) for c in await self.active_courses()}
        if course:
            names = {k: v for k, v in names.items() if course.lower() in v.lower()}
        codes, out = list(names), []
        for i in range(0, len(codes), 10):  # Canvas takes at most 10 contexts per request
            items = await self._get_all("calendar_events", {
                "type": "event", "context_codes[]": codes[i:i + 10], "per_page": 100,
                "start_date": start.astimezone(timezone.utc).isoformat(),
                "end_date": end.astimezone(timezone.utc).isoformat(),
            }, quiet=True) or []
            for e in items:
                if e.get("workflow_state") == "deleted" or not e.get("start_at"):
                    continue
                out.append({
                    "id": str(e.get("id")), "course": names.get(e.get("context_code"), ""),
                    "course_id": str(e.get("context_code") or "").removeprefix("course_"),
                    "title": " ".join((e.get("title") or "(untitled)").split()),
                    "start": self.when(e.get("start_at")), "end": self.when(e.get("end_at")),
                    "all_day": bool(e.get("all_day")),
                    "location": ", ".join(x for x in (e.get("location_name"), e.get("location_address")) if x),
                    "url": self.link(e.get("html_url") or ""), "text": strip_html(e.get("description") or ""),
                })
        return sorted(out, key=lambda e: e["start"])

    async def feedback(self, me_id: str) -> list[dict]:
        """Recent comments other people (teachers, TAs) left on the student's submissions, newest first."""
        names = {str(c["id"]): course_name(c) for c in await self.active_courses()}
        items = await self._get_all("users/self/activity_stream", {"only_active_courses": "true", "per_page": 50},
                                    max_pages=2, quiet=True) or []
        out = []
        for it in items:
            if not isinstance(it, dict) or it.get("type") != "Submission":
                continue
            cid = str(it.get("course_id") or "")
            a = it.get("assignment") or {}
            aid = it.get("assignment_id") or a.get("id")
            for c in it.get("submission_comments") or []:
                if str(c.get("author_id")) == me_id:
                    continue
                out.append({
                    "id": str(c.get("id")), "course": names.get(cid, ""), "course_id": cid,
                    "assignment_id": f"assignment:{cid}:{aid}",
                    "title": " ".join((a.get("name") or it.get("title") or "(untitled)").split()),
                    "from": c.get("author_name") or (c.get("author") or {}).get("display_name") or "",
                    "at": self.when(c.get("created_at")), "text": strip_html(c.get("comment") or ""),
                    "url": self.link(it.get("html_url") or a.get("html_url") or ""),
                })
        return sorted(out, key=lambda c: c["at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    async def module_states(self) -> list[dict]:
        """Every module in the active classes with whether it's open to the student yet."""
        out = []
        for c in await self.active_courses():
            cid = str(c["id"])
            for m in await self._get_all(f"courses/{cid}/modules", {"per_page": 100}, quiet=True) or []:
                out.append({
                    "id": str(m.get("id")), "course": course_name(c), "course_id": cid,
                    "module": " ".join((m.get("name") or "(untitled)").split()), "state": m.get("state") or "",
                    "items": m.get("items_count"), "url": f"{self.base}/courses/{cid}/modules/{m.get('id')}",
                })
        return out

    async def find(self, query: str) -> list[Assignment]:
        """Recent and upcoming work whose course + title contains every word of the query; an exact title wins."""
        now = datetime.now(self.tz)
        items = await self.assignments(now - timedelta(days=30), now + timedelta(days=90))
        q = query.lower().strip()
        exact = [a for a in items if a.title.lower() == q]
        if exact:
            return exact
        words = q.split()
        return [a for a in items if all(w in f"{a.course} {a.title}".lower() for w in words)]

    async def details(self, kind: str, course_id: str, item_id: str) -> AssignmentDetails:
        endpoint = ENDPOINTS.get(kind)
        if not endpoint:
            raise CanvasError(f"Unknown kind '{kind}'.")
        params = {"include[]": "submission"} if kind == "assignment" else None
        d = await self._get_one(f"courses/{course_id}/{endpoint}/{item_id}", params)
        if d is None:
            raise CanvasError("Canvas couldn't find that, or it isn't open to you yet.")
        course = await self.course_name(course_id)
        body = d.get("description") or d.get("message") or d.get("body") or ""
        graded = d.get("assignment") or {}  # a graded discussion carries its assignment
        facts = []
        due = d.get("due_at") or graded.get("due_at")
        if due:
            facts.append(f"Due: {datetime.fromisoformat(due.replace('Z', '+00:00')).astimezone(self.tz):%a %b %d %H:%M}")
        points = d.get("points_possible") or graded.get("points_possible")
        if points:
            facts.append(f"Points: {points:g}")
        if d.get("submission_types") and d["submission_types"] != ["none"]:
            facts.append("Submit as: " + ", ".join(t.replace("_", " ") for t in d["submission_types"]))
        if d.get("allowed_extensions"):
            facts.append("File types: " + ", ".join(d["allowed_extensions"]))
        if kind == "quiz":
            if d.get("question_count"):
                facts.append(f"Questions: {d['question_count']}")
            if d.get("time_limit"):
                facts.append(f"Time limit: {d['time_limit']} min")
            if d.get("allowed_attempts"):
                facts.append("Attempts: " + ("unlimited" if d["allowed_attempts"] == -1 else str(d["allowed_attempts"])))
        sub = d.get("submission") or {}
        if sub:
            state = sub.get("workflow_state", "")
            status = "not submitted" if state == "unsubmitted" else state.replace("_", " ")
            if sub.get("grade") is not None:
                status += f", grade {sub['grade']}"
            if sub.get("missing"):
                status += ", missing"
            elif sub.get("late"):
                status += ", late"
            facts.append(f"Status: {status}")
        if d.get("locked_for_user") and d.get("lock_explanation"):
            facts.append("Locked: " + strip_html(d["lock_explanation"]))
        rubric = [
            f"{r.get('description', '')} ({r.get('points', 0):g} pts)"
            + (f": {strip_html(r['long_description'])}" if r.get("long_description") else "")
            for r in d.get("rubric") or []
        ]
        files = [
            CanvasFile(a.get("display_name") or a.get("filename") or "file", a.get("size") or 0, a.get("url") or "")
            for a in d.get("attachments") or []
        ]
        for fid, label in file_links(body).items():
            files.append(await self.file_info(fid, label))
        return AssignmentDetails(
            kind=kind,
            title=d.get("name") or d.get("title") or "(untitled)",
            course=course,
            url=d.get("html_url") or f"{self.base}/courses/{course_id}/{endpoint}/{item_id}",
            facts=facts,
            text=html_to_text(body, self.base),
            rubric=rubric,
            files=files,
        )

    async def course_name(self, course_id: str) -> str:
        return next((course_name(c) for c in await self.active_courses() if str(c["id"]) == str(course_id)), "")

    async def course(self, query: str) -> dict:
        """The active course named by an id or words from its name or code."""
        courses = await self.active_courses()
        q = query.strip().lower()
        exact = [c for c in courses if q in (str(c["id"]), course_name(c).lower(), (c.get("course_code") or "").lower())]
        hits = exact or [c for c in courses if has_words(q, f"{course_name(c)} {c.get('course_code', '')}")]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(course_name(c) for c in hits or courses) or "none"
        raise CanvasError(f"{'Several' if hits else 'No'} active classes match '{query}'. Classes: {names}.")

    async def modules(self, course_id: str) -> list[Module]:
        mods = await self._get_all(
            f"courses/{course_id}/modules", {"include[]": ["items", "content_details"], "per_page": 50}, quiet=True
        )
        out = []
        for m in mods or []:
            items = m.get("items")
            if items is None and m.get("items_url"):  # Canvas leaves the items out of very big modules
                items = await self._get_all(m["items_url"], {"include[]": "content_details", "per_page": 100}, quiet=True)
            out.append(
                Module(
                    name=" ".join((m.get("name") or "(untitled)").split()),
                    items=[parse_module_item(it, course_id) for it in items or []],
                    locked=m.get("state") == "locked",
                )
            )
        return out

    async def course_files(self, course_id: str) -> list[CanvasFile] | None:
        """Everything in the course's Files, or None when the class hides Files from students."""
        files = await self._get_all(f"courses/{course_id}/files", {"per_page": 100, "sort": "name"}, quiet=True)
        if files is None:
            return None
        folders = await self._get_all(f"courses/{course_id}/folders", {"per_page": 100}, quiet=True) or []
        paths = {f.get("id"): (f.get("full_name") or "").removeprefix("course files").strip("/") for f in folders}
        return [
            CanvasFile(
                f.get("display_name") or f.get("filename") or "file",
                f.get("size") or 0,
                f.get("url") or "",
                error="locked" if f.get("locked_for_user") else "",
                id=str(f.get("id", "")),
                folder=paths.get(f.get("folder_id"), ""),
            )
            for f in files
        ]

    async def pages(self, course_id: str) -> list[ModuleItem]:
        pages = await self._get_all(f"courses/{course_id}/pages", {"per_page": 100, "sort": "title"}, quiet=True)
        return [ModuleItem(p.get("title") or "(untitled)", "page", f"page:{course_id}:{p['url']}")
                for p in pages or [] if p.get("url")]

    async def syllabus(self, course_id: str) -> AssignmentDetails:
        c = await self._get_one(f"courses/{course_id}", {"include[]": "syllabus_body"})
        if c is None:
            raise CanvasError("Canvas couldn't find that class, or it isn't open to you.")
        body = c.get("syllabus_body") or ""
        return AssignmentDetails(
            kind="syllabus",
            title="Syllabus",
            course=course_name(c),
            url=f"{self.base}/courses/{course_id}/assignments/syllabus",
            facts=[],
            text=html_to_text(body, self.base),
            files=[await self.file_info(fid, label) for fid, label in file_links(body).items()],
        )

    async def module_item(self, course_id: str, item_id: str) -> ModuleItem:
        """What a /modules/items/N link points at."""
        seq = await self._get_one(
            f"courses/{course_id}/module_item_sequence", {"asset_type": "ModuleItem", "asset_id": item_id}
        )
        current = next((i["current"] for i in (seq or {}).get("items") or [] if i.get("current")), None)
        if not current:
            raise CanvasError("Canvas couldn't find that module item, or it isn't open to you yet.")
        return parse_module_item(current, course_id)

    async def find_in_modules(self, query: str) -> list[tuple[str, ModuleItem]]:
        """(course, item) for module content in any active class whose title holds every word; an exact title wins."""
        found = []
        for c in await self.active_courses():
            name = course_name(c)
            for m in await self.modules(str(c["id"])):
                found += [(name, i) for i in m.items if i.ref and has_words(query, f"{name} {i.title}")]
        exact = [f for f in found if f[1].title.lower() == query.lower().strip()]
        return exact or found

    async def file_info(self, file_id: str, label: str = "") -> CanvasFile:
        f = await self._get_one(f"files/{file_id}")
        if f is None:
            return CanvasFile(label or f"file {file_id}", 0, "", error="not accessible")
        name = f.get("display_name") or f.get("filename") or label or f"file {file_id}"
        if f.get("locked_for_user"):
            return CanvasFile(name, f.get("size") or 0, "", error="locked")
        return CanvasFile(name, f.get("size") or 0, f.get("url") or "")

    async def download(self, f: CanvasFile) -> bytes:
        if not f.url:
            raise CanvasError(f.error or "no download link")
        if f.size > self.max_file_bytes:
            raise CanvasError(f"too big ({f.size // (1024 * 1024)} MB)")
        r = await self.http.get(f.url)
        r.raise_for_status()
        if len(r.content) > self.max_file_bytes:
            raise CanvasError("too big")
        return r.content

    async def me(self) -> dict:
        """The token's user, to check a login."""
        me = await self._get_one("users/self")
        if me is None:
            raise CanvasError("Canvas didn't accept the token.")
        return me

    async def _get_one(self, path: str, params: dict | None = None) -> dict | None:
        """One object, or None when it's missing or off limits to the student (locked files, unpublished pages)."""
        r = await self.http.get(f"{self.base}/api/v1/{path}", params=params)
        if r.status_code == 401 and "www-authenticate" in r.headers:  # a bad token, not a locked item
            raise CanvasError("Canvas rejected the API token (401). It may have expired.")
        if r.status_code in (401, 403, 404):
            return None
        r.raise_for_status()
        return r.json()
