"""The Canvas MCP server (`daimon-canvas mcp`, stdio): homework, classes, grades and announcements, read-only.

Everything goes straight to the Canvas API with the user's access token; nothing is stored except what
canvas_open downloads into files/, which read_canvas_file pages through.
"""
from __future__ import annotations

import functools
from datetime import datetime, timedelta

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .client import (CanvasClient, CanvasError, CanvasFile, ModuleItem, course_name, format_assignment, has_words,
                     parse_ref)
from .config import Home
from .files import Files, folder_name, human_size, safe_name

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)

INSTRUCTIONS = """The user's Canvas (their school's learning system): homework due, their classes' modules, pages and
files, grades and announcements. Read-only: nothing here submits or changes anything on Canvas.
Start with canvas_assignments for what's due, or canvas_course to browse a class; both give ids for canvas_open.
canvas_open saves attachments and files to the downloads folder; read_canvas_file reads them, PDFs and Word too."""


def clip(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - 1)].rstrip() + "…"


def friendly(fn):
    """Network trouble and Canvas server errors as a message for the model, not a crash."""
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            raise CanvasError(f"Canvas answered {e.response.status_code} for {e.request.url.path}.") from e
        except httpx.HTTPError as e:
            raise CanvasError(f"Couldn't reach Canvas ({type(e).__name__}). Try again in a bit.") from e
    return run


def build(home: Home, client: CanvasClient | None = None) -> MCPServer:
    cfg = home.config()
    files = Files(home.files)
    budget = max(1000, int(cfg.read_chars))
    server = MCPServer("canvas", instructions=INSTRUCTIONS + f"\nThe downloads folder is {files.root}.")
    clients: list[CanvasClient] = [client] if client else []

    def canvas() -> CanvasClient:
        if not clients:
            token = home.token()
            if not cfg.url or not token:
                raise CanvasError("Canvas isn't set up yet. The user runs `daimon-canvas login` once.")
            clients.append(CanvasClient(cfg.url, token, cfg.tz(), max_file_mb=cfg.max_file_mb))
        return clients[0]

    def item_line(i: ModuleItem) -> str:
        if i.kind == "header":
            return f"  {i.title}"
        lock = " (locked)" if i.locked else ""
        return f"- {i.title} [{i.kind}]{lock} | " + (f"id {i.ref}" if i.ref else i.url or "only opens inside Canvas")

    def file_line(f: CanvasFile, course_id: str) -> str:
        lock = f" ({f.error})" if f.error else ""
        return f"- {f.folder + '/' if f.folder else ''}{f.name} ({human_size(f.size)}){lock} | id file:{course_id}:{f.id}"

    @server.tool(annotations=READ)
    @friendly
    async def canvas_assignments(days_ahead: int = 7, course: str = "") -> str:
        """Canvas homework still to do in active classes: overdue work, and assignments, quizzes and discussions due
        in the next days_ahead days that aren't submitted yet. course filters by class name (e.g. calc). Each has an
        id for canvas_open."""
        c = canvas()
        now = datetime.now(c.tz)
        days = max(0, min(days_ahead, 60))
        end = now.replace(hour=23, minute=59) + timedelta(days=days)
        items = await c.assignments(now - timedelta(days=7), end)
        if course:
            items = [a for a in items if course.lower() in a.course.lower()]
        missing = [a for a in items if a.due and a.due < now and a.missing and not a.done]
        upcoming = [a for a in items if a.due and a.due >= now and not a.done]
        done = sum(1 for a in items if a.due and a.due >= now and a.done)

        lines = []
        if missing:
            lines += ["Missing:", *(format_assignment(a, show_id=True) for a in missing)]
        scope = f" for '{course}'" if course else ""
        if upcoming:
            lines += [f"Due in the next {days} days{scope}:", *(format_assignment(a, show_id=True) for a in upcoming)]
        else:
            lines.append(f"Nothing left to submit in the next {days} days{scope}.")
        if done:
            lines.append(f"({done} more due in that time already submitted.)")
        return "\n".join(lines)

    @server.tool(annotations=READ)
    @friendly
    async def canvas_course(course: str, search: str = "") -> str:
        """Browse one Canvas class: its modules and everything in them (pages, files, assignments, links), plus the
        syllabus. course: the class by name or code (calc, CHEM 1A). search: words to find a module, page or file
        by name (week 3, lab); it also looks through the class's Pages and Files. Each has an id for canvas_open."""
        c = canvas()
        found = await c.course(course)
        cid, name, q = str(found["id"]), course_name(found), search.strip()
        modules = await c.modules(cid)
        lines = [f"{name} (course {cid}). Syllabus: id syllabus:{cid}"]
        for m in modules:
            # A search that names the module keeps all of it; otherwise only the matching items.
            items = m.items if not q or has_words(q, m.name) else [i for i in m.items if has_words(q, i.title)]
            if q and not items:
                continue
            lines += ["", m.name + (" (locked):" if m.locked else ":"), *([item_line(i) for i in items] or ["(empty)"])]

        # Pages and Files hold what the modules don't link to; look there when searching or when there are no modules.
        if q or not modules:
            in_modules = {i.ref for m in modules for i in m.items}
            pages = [i for i in await c.pages(cid) if i.ref not in in_modules and (not q or has_words(q, i.title))]
            course_files = [
                f for f in await c.course_files(cid) or []
                if f"file:{cid}:{f.id}" not in in_modules and (not q or has_words(q, f"{f.folder} {f.name}"))
            ]
            if pages:
                lines += ["", "Pages:", *(item_line(i) for i in pages)]
            if course_files:
                lines += ["", "Files:", *(file_line(f, cid) for f in course_files)]
        if len(lines) == 1:
            lines.append(f"Nothing in this class matches '{q}'." if q else "Nothing posted in its modules, pages or files.")
        elif not q:
            lines += ["", "(Search to find pages and files outside the modules.)"]

        text = "\n".join(lines)
        if len(text) <= budget:
            return text
        path = files.write(f"{folder_name(name)}/contents{'-' + folder_name(q) if q else ''}.md", text)
        note = f"\n(Cut short. The full list is in {path}: read_canvas_file reads it. Or search to narrow it.)"
        return text[: text.rfind("\n", 0, budget - len(note))] + note

    async def open_file(c: CanvasClient, course_id: str, file_id: str) -> str:
        f = await c.file_info(file_id)
        try:
            data = await c.download(f)
        except CanvasError as e:
            raise CanvasError(f"Can't get {f.name}: {e}")
        course = await c.course_name(course_id) if course_id else ""
        path = files.save_bytes(f"{folder_name(course) if course else 'other'}/files/{safe_name(f.name)}", data)
        head = f"{f.name} ({human_size(len(data))}), saved to {path}."
        try:
            text = files.read(path, max_chars=budget - len(head) - 60)
        except CanvasError as e:
            return f"{head}\n{e}"
        more = " Read on with read_canvas_file." if "\n(continues at line" in text else ""
        return f"{head}{more}\n\n{text}"

    @server.tool(annotations=READ)
    @friendly
    async def canvas_open(item: str, download_files: bool = True) -> str:
        """Open anything on Canvas in full: an assignment, quiz, discussion, page, file, module item or the syllabus.
        Gives the instructions, due date, points, status and rubric, or a file's text. Attached files are saved to
        the downloads folder for read_canvas_file.

        item: an id from canvas_assignments or canvas_course (quiz:10:2, file:9:77, syllabus:9), a Canvas link, or
        words from the title.
        """
        c = canvas()
        try:
            kind, course_id, item_id = parse_ref(item)
        except CanvasError:
            found = [(format_assignment(a, show_id=True), a.ref) for a in await c.find(item)]
            if not found:
                found = [(f"{cn} | {i.title} [{i.kind}] | id {i.ref}", i.ref) for cn, i in await c.find_in_modules(item)]
            if not found:
                raise CanvasError(f"Nothing on Canvas matches '{item}'. Browse a class with canvas_course.")
            if len(found) > 1:
                listed = "\n".join(line for line, _ in found[:8])
                return f"Several match '{item}'. Call again with one of these ids:\n{listed}"
            kind, course_id, item_id = parse_ref(found[0][1])
        if kind == "module_item":
            mi = await c.module_item(course_id, item_id)
            if not mi.ref:
                if mi.url:
                    return f"'{mi.title}' is a link to {mi.url}."
                return f"'{mi.title}' is a {mi.kind} that only opens inside Canvas, so it can't be read from here."
            kind, course_id, item_id = parse_ref(mi.ref)
        if kind == "file":
            return await open_file(c, course_id, item_id)
        d = await c.syllabus(course_id) if kind == "syllabus" else await c.details(kind, course_id, item_id)

        folder = f"{folder_name(d.course or 'course ' + course_id)}/{folder_name(d.title)}"
        saved, skipped = [], []
        for f in d.files if download_files else []:
            try:
                saved.append((files.save_bytes(f"{folder}/{safe_name(f.name)}", await c.download(f)), f.size))
            except CanvasError as e:
                skipped.append(f"{f.name} ({e})")
            except httpx.HTTPError as e:
                skipped.append(f"{f.name} (download failed: {type(e).__name__})")

        doc = [f"# {d.title}", f"{d.course} | {d.kind} | {d.url}", *d.facts, "", d.text or "(Nothing written.)"]
        if d.rubric:
            doc += ["", "## Rubric", *(f"- {r}" for r in d.rubric)]
        if d.files:
            doc += ["", "## Files", *(f"- {f.name}" + (f" ({f.error})" if f.error else "") for f in d.files)]
        details_path = files.write(f"{folder}/{folder_name(d.title)}.md", "\n".join(doc))

        head = [f"{d.title} ({d.kind}, {d.course})", d.url, *d.facts]
        tail = [""]
        if d.rubric:
            tail += ["Rubric:", *(f"- {clip(r, 300)}" for r in d.rubric)]
        if saved:
            tail.append("Files saved (read_canvas_file opens them):")
            tail += [f"- {path} ({human_size(size)})" for path, size in saved]
        elif d.files and not download_files:
            tail.append("Attached files: " + ", ".join(f.name for f in d.files) + " (not downloaded)")
        if skipped:
            tail.append("Couldn't get: " + "; ".join(skipped))
        tail.append(f"Everything above is saved in {details_path}.")
        room = budget - sum(len(line) + 1 for line in head + tail) - 20
        body = d.text or "(Nothing written on Canvas.)"
        if len(body) > room:
            body = clip(body, max(room, 500)) + f"\n(Cut short: read_canvas_file reads all of {details_path}.)"
        return "\n".join([*head, "", body, *tail])

    @server.tool(annotations=READ)
    def read_canvas_file(path: str, start_line: int = 1) -> str:
        """Read a file canvas_open or canvas_course saved (its path as they gave it), as text: PDFs, Word, slides,
        spreadsheets too. Long files come a page at a time; start_line continues where the last read stopped."""
        return files.read(path, start_line, budget)

    @server.tool(annotations=READ)
    @friendly
    async def canvas_grades() -> str:
        """The current grade in each active Canvas class."""
        lines = []
        for c in await canvas().courses():
            enrollment = next((e for e in c.get("enrollments", []) if e.get("type") == "student"), None)
            if not enrollment or not c.get("name"):
                continue
            score, grade = enrollment.get("computed_current_score"), enrollment.get("computed_current_grade")
            lines.append(f"{course_name(c)}: " + (f"{score}%" if score is not None else "no score yet")
                         + (f" ({grade})" if grade else ""))
        return "\n".join(lines) or "No active classes."

    @server.tool(annotations=READ)
    @friendly
    async def canvas_announcements(days_back: int = 7, course: str = "") -> str:
        """Recent announcements from Canvas classes, newest first. course filters by class name (e.g. chem)."""
        days = max(1, min(days_back, 60))
        items = await canvas().announcements(days, course)
        scope = f" for '{course}'" if course else ""
        if not items:
            return f"No announcements in the last {days} days{scope}."
        # A few announcements are shown in full so they can actually be read; a long list stays a skim.
        full = len(items) <= 3
        out = [f"{a['posted'][:10]} | {a['course']} | {a['title']}: {clip(a['text'], 2500 if full else 300)}"
               for a in items[:12]]
        if not full:
            out.append("(Shortened. Filter by course to read them in full.)")
        return ("\n\n" if full else "\n").join(out)

    @server.tool(annotations=READ)
    @friendly
    async def canvas_status() -> str:
        """Whether Canvas is connected: the school, the signed-in student, and the active classes."""
        c = canvas()
        me = await c.me()
        courses = await c.courses()
        names = ", ".join(course_name(x) for x in courses) or "none"
        return f"Connected to {c.base} as {me.get('name', '?')}.\nActive classes ({len(courses)}): {names}\n" \
               f"Downloads folder: {files.root}"

    return server


def main(home: Home) -> None:
    build(home).run("stdio")
