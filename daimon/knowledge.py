"""The knowledge graph: an Open Knowledge Format (OKF) bundle in knowledge/, plus a rebuildable SQLite index.

OKF: a folder of markdown files with YAML frontmatter (`type` is the only required field; title,
description, tags, timestamp and custom fields are optional), linked with ordinary markdown links, with
optional index.md entry points and a log.md history. We add two custom fields: `updated` and `sources`
(where facts came from, e.g. "user", "email:...", "sms").

The files are the source of truth and live in the agent's git repo. The index (.cache/kg.sqlite) holds
full-text search, the link graph and optional embeddings, refreshed incrementally from file mtimes. Delete it
any time.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from .gate import Tool
from .home import Home, join_frontmatter, split_frontmatter
from .schedule import now

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+\.md)(?:#[^)]*)?\)")
STOP = set("a an and are as at be but by do does for from has have how i in is it its me my of on or our so "
           "that the their them then there they this to was we were what when where which who why will with you "
           "your about can could should would please tell know".split())
SKIP_CONTEXT = {"index", "log"}


class Knowledge:
    def __init__(self, home: Home):
        self.home = home
        self.root = home.root / "knowledge"
        self._lock = asyncio.Lock()  # one index refresh at a time (workers search concurrently)

    # ---- index ---------------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        self.home.cache.mkdir(exist_ok=True)
        db = sqlite3.connect(self.home.cache / "kg.sqlite")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, type TEXT, title TEXT, description TEXT,
                tags TEXT, body TEXT, mtime REAL, vec TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path UNINDEXED, title, description, tags, body,
                tokenize = 'porter unicode61');
            CREATE TABLE IF NOT EXISTS links (src TEXT, dst TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        return db

    def embedding_model(self) -> str | None:
        model = str(self.home.config().get("knowledge", {}).get("embeddings", "none") or "none")
        return None if model.lower() == "none" else model

    def note_files(self) -> dict[str, float]:
        if not self.root.exists():
            return {}
        return {p.relative_to(self.root).as_posix(): p.stat().st_mtime for p in self.root.rglob("*.md")
                if p.name != "log.md"}

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh()

    async def _refresh(self) -> None:
        db = self._db()
        try:
            files = self.note_files()
            known = dict(db.execute("SELECT path, mtime FROM notes"))
            for path in set(known) - set(files):
                self._drop(db, path)
            for path, mtime in files.items():
                if known.get(path) != mtime:
                    self._index(db, path, mtime)
            await self._embed(db)
            db.commit()
        finally:
            db.close()

    def _drop(self, db: sqlite3.Connection, path: str) -> None:
        db.execute("DELETE FROM notes WHERE path = ?", (path,))
        db.execute("DELETE FROM fts WHERE path = ?", (path,))
        db.execute("DELETE FROM links WHERE src = ?", (path,))

    def _index(self, db: sqlite3.Connection, path: str, mtime: float) -> None:
        self._drop(db, path)
        meta, body = split_frontmatter((self.root / path).read_text(encoding="utf-8"))
        tags = meta.get("tags") or []
        tags = " ".join(tags) if isinstance(tags, list) else str(tags)
        row = (path, str(meta.get("type", "")), str(meta.get("title") or Path(path).stem),
               str(meta.get("description", "")), tags, body)
        db.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", (*row, mtime))
        db.execute("INSERT INTO fts VALUES (?, ?, ?, ?, ?)", (path, *row[2:]))
        base = Path(path).parent
        for target in {m.group(1) for m in LINK.finditer(body)}:
            if "://" in target:
                continue
            resolved = (self.root / base / target).resolve()
            if self.root.resolve() in resolved.parents:
                db.execute("INSERT INTO links VALUES (?, ?)", (path, resolved.relative_to(self.root.resolve()).as_posix()))

    async def _embed(self, db: sqlite3.Connection) -> None:
        model = self.embedding_model()
        if db.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone() != (model,):
            db.execute("UPDATE notes SET vec = NULL")  # the model changed: old vectors aren't comparable
            db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (model,))
        if not model:
            return
        todo = db.execute("SELECT path, title, description, body FROM notes WHERE vec IS NULL").fetchall()
        import litellm
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            texts = [f"{t}\n{d}\n{b[:2000]}" for _p, t, d, b in batch]
            resp = await litellm.aembedding(model=model, input=texts)
            for (path, *_), item in zip(batch, resp.data):
                db.execute("UPDATE notes SET vec = ? WHERE path = ?", (json.dumps(item["embedding"]), path))

    # ---- reading -------------------------------------------------------------------------------------
    async def search(self, query: str, limit: int = 8, type: str | None = None) -> list[dict[str, Any]]:
        await self.refresh()
        db = self._db()
        try:
            ranked: dict[str, float] = {}
            words = [w for w in re.findall(r"\w+", query.lower()) if w not in STOP and len(w) > 1]
            if words:
                match = " OR ".join(f'"{w}"' for w in words[:30])
                rows = db.execute("SELECT path FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT 30", (match,))
                for rank, (path,) in enumerate(rows):
                    ranked[path] = ranked.get(path, 0) + 1 / (60 + rank)
            model = self.embedding_model()
            if model:
                import litellm
                qvec = (await litellm.aembedding(model=model, input=[query])).data[0]["embedding"]
                scored = [(cosine(qvec, json.loads(v)), p) for p, v in
                          db.execute("SELECT path, vec FROM notes WHERE vec IS NOT NULL")]
                for rank, (_s, path) in enumerate(sorted(scored, reverse=True)[:30]):
                    ranked[path] = ranked.get(path, 0) + 1 / (60 + rank)
            out = []
            for path in sorted(ranked, key=ranked.get, reverse=True):
                row = db.execute("SELECT type, title, description, body FROM notes WHERE path = ?", (path,)).fetchone()
                if row and (type is None or row[0] == type):
                    out.append({"path": path, "type": row[0], "title": row[1], "description": row[2],
                                "snippet": " ".join(row[3].split())[:200]})
                if len(out) >= limit:
                    break
            return out
        finally:
            db.close()

    def neighbors(self, paths: list[str]) -> list[str]:
        db = self._db()
        try:
            found: list[str] = []
            for p in paths:
                for (other,) in db.execute("SELECT dst FROM links WHERE src = ? UNION SELECT src FROM links WHERE dst = ?",
                                           (p, p)):
                    if other not in paths and other not in found and (self.root / other).exists():
                        found.append(other)
            return found
        finally:
            db.close()

    def path(self, rel: str) -> Path:
        rel = rel.replace("\\", "/").lstrip("/").removeprefix("knowledge/")
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents or p.suffix != ".md":
            raise ValueError(f"{rel} isn't a note path inside knowledge/ (notes end in .md)")
        return p

    def read(self, rel: str) -> str:
        p = self.path(rel)
        if not p.exists():
            return f"No note at {rel}."
        rel = p.relative_to(self.root.resolve()).as_posix()
        db = self._db()
        try:
            backlinks = [s for (s,) in db.execute("SELECT src FROM links WHERE dst = ?", (rel,))]
        finally:
            db.close()
        text = p.read_text(encoding="utf-8")
        return text + (f"\n\n(Linked from: {', '.join(backlinks)})" if backlinks else "")

    def count(self) -> int:
        return len([p for p in self.note_files() if not p.endswith("index.md")])

    async def context_for(self, text: str, max_chars: int) -> str:
        """Notes relevant to `text`, plus their direct links, trimmed to fit a prompt."""
        if not text.strip() or max_chars <= 0:
            return ""
        hits = [h for h in await self.search(text, limit=5) if h["type"] not in SKIP_CONTEXT]
        paths = [h["path"] for h in hits]
        paths += self.neighbors(paths)[:3]
        parts, used = [], 0
        for rel in paths:
            meta, body = split_frontmatter((self.root / rel).read_text(encoding="utf-8"))
            if str(meta.get("type")) in SKIP_CONTEXT:
                continue
            chunk = f"## {rel}\n{body.strip()[:1500]}"
            if used + len(chunk) > max_chars:
                break
            parts.append(chunk)
            used += len(chunk)
        return "\n\n".join(parts)

    # ---- writing -------------------------------------------------------------------------------------
    def write(self, rel: str, meta: dict[str, Any], body: str, reason: str, source: str | None = None) -> str:
        p = self.path(rel)
        if not meta.get("type"):
            raise ValueError("every note needs a type (person, project, preference, event, place, topic, ...)")
        old = split_frontmatter(p.read_text(encoding="utf-8"))[0] if p.exists() else {}
        sources = list(old.get("sources") or [])
        if source and source not in sources:
            sources = (sources + [source])[-10:]
        meta = {**{k: v for k, v in meta.items() if v not in (None, "", [])}, "updated": now().isoformat(timespec="minutes")}
        if sources:
            meta["sources"] = sources
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(join_frontmatter(meta, body.strip() + "\n"), encoding="utf-8")
        rel = p.relative_to(self.root.resolve()).as_posix()
        log = self.root / "log.md"
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else ["# Knowledge log", ""]
        lines.append(f"- {now():%Y-%m-%d %H:%M} {'updated' if old else 'created'} [{rel}]({rel}): {reason}"
                     + (f" (source: {source})" if source else ""))
        log.write_text("\n".join(lines[:2] + lines[2:][-500:]) + "\n", encoding="utf-8")
        ref = self.home.commit(f"knowledge: {reason}", [f"knowledge/{rel}", "knowledge/log.md"])
        return f"Saved knowledge/{rel}" + (f" (change {ref})" if ref else "")


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def tools(kb: Knowledge) -> list[Tool]:
    STR = {"type": "string"}

    def level(_args: dict[str, Any]) -> str:
        return "ask" if kb.home.config().get("self_edit") == "all" else "auto"

    async def kg_search(args: dict[str, Any]) -> str:
        hits = await kb.search(args["query"], int(args.get("limit") or 8), args.get("type"))
        return "\n".join(f"- {h['path']} [{h['type']}] {h['title']}: {h['description'] or h['snippet']}"
                         for h in hits) or "No matching notes."

    async def kg_read(args: dict[str, Any]) -> str:
        await kb.refresh()
        return kb.read(args["path"])

    async def kg_list(args: dict[str, Any]) -> str:
        folder = kb.root / str(args.get("folder") or "").strip("/")
        if not folder.exists():
            return "No such folder."
        lines = []
        for p in sorted(folder.rglob("*.md")):
            if p.name == "log.md":
                continue
            meta = split_frontmatter(p.read_text(encoding="utf-8"))[0]
            lines.append(f"- {p.relative_to(kb.root).as_posix()} [{meta.get('type', '?')}] {meta.get('title', '')}")
        return "\n".join(lines) or "Empty."

    async def kg_write(args: dict[str, Any]) -> str:
        meta = {"type": args["type"], "title": args.get("title"), "description": args.get("description"),
                "tags": args.get("tags")}
        return kb.write(args["path"], meta, args["body"], args["reason"], args.get("source"))

    note_path = {"type": "string", "description": "Path inside knowledge/, e.g. people/sam.md"}
    return [
        Tool("kg_search", "Search the knowledge graph (notes about the user, people, projects, preferences...).",
             {"type": "object", "properties": {"query": STR, "type": STR, "limit": {"type": "integer"}},
              "required": ["query"]}, kg_search, lambda _a: "auto"),
        Tool("kg_read", "Read a knowledge note (shows which notes link to it).",
             {"type": "object", "properties": {"path": note_path}, "required": ["path"]}, kg_read,
             lambda _a: "auto"),
        Tool("kg_list", "List notes in a knowledge folder (default: all).",
             {"type": "object", "properties": {"folder": STR}}, kg_list, lambda _a: "auto"),
        Tool("kg_write",
             "Create or replace a knowledge note. Read it first if it exists and keep what's still true. Link "
             "related notes with markdown links, e.g. [Sam](../people/sam.md).",
             {"type": "object", "properties": {
                 "path": note_path, "type": {"type": "string", "description": "person, project, preference, "
                                             "event, place, topic, ..."},
                 "title": STR, "description": {"type": "string", "description": "One line: what this note is"},
                 "tags": {"type": "array", "items": STR}, "body": STR,
                 "source": {"type": "string", "description": "Where this came from: user, email:<id>, sms, ..."},
                 "reason": STR},
              "required": ["path", "type", "title", "body", "reason"]}, kg_write, level),
    ]
