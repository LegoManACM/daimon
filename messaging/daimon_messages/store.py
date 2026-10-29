"""messages.db: every stored message, the chats they belong to, and the phone's contacts.

Written only by the running service; the MCP server and the CLI read it (WAL lets them read while it writes).
Times are stored as UTC ISO strings, so they sort and compare as text.

`seq` numbers messages in the order they were stored. Consumers that process the stream (the message graph,
later) keep a cursor with feed()/ack() instead of re-reading everything.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .filters import digits

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    is_group INTEGER NOT NULL DEFAULT 0,
    participants TEXT NOT NULL DEFAULT '[]',  -- [{id, name, number, me}]
    kind TEXT NOT NULL DEFAULT '',            -- SMS | RCS | ...
    read_only INTEGER NOT NULL DEFAULT 0,
    last_ts TEXT
);
CREATE TABLE IF NOT EXISTS messages(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    chat TEXT NOT NULL,
    ts TEXT NOT NULL,                  -- UTC ISO
    sender_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    number TEXT NOT NULL DEFAULT '',
    from_me INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    media TEXT NOT NULL DEFAULT '[]',  -- [{name, mime, size}]: noted, never downloaded
    status TEXT NOT NULL DEFAULT '',
    backfill INTEGER NOT NULL DEFAULT 0,
    stored TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat, ts);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(body, sender, content='messages', content_rowid='seq',
                                                           tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body, sender) VALUES (new.seq, new.body, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, sender) VALUES ('delete', old.seq, old.body, old.sender);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF body, sender ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, sender) VALUES ('delete', old.seq, old.body, old.sender);
    INSERT INTO messages_fts(rowid, body, sender) VALUES (new.seq, new.body, new.sender);
END;
CREATE TABLE IF NOT EXISTS contacts(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    number TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(ts_ms: int | float) -> str:
    return datetime.fromtimestamp(int(ts_ms or 0) / 1000, timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def kv_get(self, key: str, default: str = "") -> str:
        rows = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))

    # ---- chats ----

    def upsert_chat(self, cid: str, name: str, is_group: bool, participants: list[dict], kind: str = "",
                    read_only: bool = False) -> None:
        self.execute(
            "INSERT INTO chats(id, name, is_group, participants, kind, read_only) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, is_group = excluded.is_group, "
            "participants = excluded.participants, kind = excluded.kind, read_only = excluded.read_only",
            (cid, name or "", int(bool(is_group)), json.dumps(participants, ensure_ascii=False), kind or "",
             int(bool(read_only))),
        )

    @staticmethod
    def _chat(row: sqlite3.Row) -> dict:
        return dict(row) | {"participants": json.loads(row["participants"] or "[]")}

    def chat(self, cid: str) -> dict | None:
        rows = self.query("SELECT * FROM chats WHERE id = ?", (cid,))
        return self._chat(rows[0]) if rows else None

    def chat_name(self, cid: str, chat: dict | None = None) -> str:
        c = chat if chat is not None else self.chat(cid)
        if c is None:
            return "a chat"
        if c["name"]:
            return c["name"]
        others = [p.get("name") or p.get("number") for p in c["participants"] if not p.get("me")]
        return ", ".join(o for o in others if o) or "a chat"

    def chats(self, query: str = "", since: str = "", limit: int = 20) -> list[dict]:
        """Chats newest first, each with its message count and newest message."""
        where, params = [], []
        if query.strip():
            q = f"%{query.strip().lower()}%"
            d = digits(query)
            where.append("(LOWER(c.name) LIKE ? OR LOWER(c.participants) LIKE ?"
                         + (" OR c.participants LIKE ?" if len(d) >= 4 else "") + ")")
            params += [q, q] + ([f"%{d[-10:]}%"] if len(d) >= 4 else [])
        if since:
            where.append("c.last_ts >= ?")
            params.append(since)
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat = c.id) AS n FROM chats c"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY COALESCE(c.last_ts, '') DESC LIMIT ?")
        out = []
        for row in self.query(sql, (*params, limit)):
            chat = self._chat(row)
            last = self.query("SELECT * FROM messages WHERE chat = ? ORDER BY ts DESC, seq DESC LIMIT 1", (chat["id"],))
            chat["last"] = dict(last[0]) if last else None
            out.append(chat)
        return out

    def resolve_chat(self, ref: str) -> tuple[dict | None, list[dict]]:
        """A chat from its ID, its name, or a person in it. Returns (the chat, []) or (None, candidates)."""
        ref = ref.strip()
        if not ref:
            return None, []
        if chat := self.chat(ref):
            return chat, []
        candidates = self.chats(query=ref, limit=8)
        if not candidates:
            return None, []
        exact = [c for c in candidates if self.chat_name(c["id"], c).lower() == ref.lower()]
        if len(exact) == 1:
            return exact[0], []
        direct = [c for c in candidates if not c["is_group"]]
        if len(candidates) == 1:
            return candidates[0], []
        if len(direct) == 1 and not exact:
            return direct[0], []  # "Sam" means the chat with Sam, not every group Sam is in
        return None, exact or candidates

    # ---- messages ----

    def add(self, msg: dict) -> bool:
        """Stores one message (a sidecar event). One seen again (a status change, a backfill overlap) only updates
        its body and status. Returns True if it's new."""
        ts = iso(msg.get("ts_ms") or 0)
        body = (msg.get("text") or "").strip()
        media = [m for m in msg.get("media") or [] if isinstance(m, dict)]
        cur = self.execute(
            "INSERT INTO messages(id, chat, ts, sender_id, sender, number, from_me, body, media, status, backfill, "
            "stored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
            (str(msg["id"]), str(msg.get("conv", "")), ts, str(msg.get("sender", "")), msg.get("name", "") or "",
             msg.get("number", "") or "", int(bool(msg.get("from_me"))), body, json.dumps(media),
             str(msg.get("status", "")), int(bool(msg.get("backfill"))), utcnow()),
        )
        if cur.rowcount:
            self.execute("UPDATE chats SET last_ts = MAX(COALESCE(last_ts, ''), ?) WHERE id = ?",
                         (ts, str(msg.get("conv", ""))))
            return True
        self.execute("UPDATE messages SET body = CASE WHEN ? != '' THEN ? ELSE body END, status = ? WHERE id = ?",
                     (body, body, str(msg.get("status", "")), str(msg["id"])))
        return False

    def delete(self, message_id: str) -> None:
        self.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def last_ts(self) -> datetime | None:
        rows = self.query("SELECT MAX(ts) AS ts FROM messages")
        return datetime.fromisoformat(rows[0]["ts"]) if rows and rows[0]["ts"] else None

    def get(self, message_id: str) -> dict | None:
        rows = self.query("SELECT * FROM messages WHERE id = ?", (message_id,))
        return dict(rows[0]) if rows else None

    def search(self, query: str = "", person: str = "", chat: str = "", since: str = "", until: str = "",
               from_me: bool | None = None, limit: int = 10) -> list[dict]:
        """Newest first. query is full-text (words, "a phrase", prefix*); person matches a sender or a chat's
        people by name or number; since/until are UTC ISO."""
        where, params = [], []
        sql = "SELECT m.* FROM messages m LEFT JOIN chats c ON c.id = m.chat"
        if query.strip():
            sql += " JOIN messages_fts f ON f.rowid = m.seq"
            where.append("messages_fts MATCH ?")
            params.append(fts_query(query))
        if person.strip():
            p = f"%{person.strip().lower()}%"
            d = digits(person)
            where.append("(LOWER(m.sender) LIKE ? OR LOWER(c.name) LIKE ? OR LOWER(c.participants) LIKE ?"
                         + (" OR m.number LIKE ?" if len(d) >= 4 else "") + ")")
            params += [p, p, p] + ([f"%{d[-10:]}%"] if len(d) >= 4 else [])
        if chat:
            where.append("m.chat = ?")
            params.append(chat)
        if since:
            where.append("m.ts >= ?")
            params.append(since)
        if until:
            where.append("m.ts < ?")
            params.append(until)
        if from_me is not None:
            where.append("m.from_me = ?")
            params.append(int(from_me))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.ts DESC, m.seq DESC LIMIT ?"
        return [dict(r) for r in self.query(sql, (*params, limit))]

    def history(self, chat: str, before: str = "", after: str = "", limit: int = 30) -> list[dict]:
        """A chat's messages in order. With after, the first `limit` from then; otherwise the last `limit`
        (before `before`, if given)."""
        if after:
            rows = self.query("SELECT * FROM messages WHERE chat = ? AND ts >= ? AND (? = '' OR ts < ?) "
                              "ORDER BY ts, seq LIMIT ?", (chat, after, before, before, limit))
            return [dict(r) for r in rows]
        rows = self.query("SELECT * FROM messages WHERE chat = ? AND (? = '' OR ts < ?) ORDER BY ts DESC, seq DESC "
                          "LIMIT ?", (chat, before, before, limit))
        return [dict(r) for r in reversed(rows)]

    def around(self, message_id: str, before: int = 5, after: int = 5) -> list[dict]:
        m = self.get(message_id)
        if m is None:
            return []
        older = self.query("SELECT * FROM messages WHERE chat = ? AND (ts < ? OR (ts = ? AND seq < ?)) "
                           "ORDER BY ts DESC, seq DESC LIMIT ?", (m["chat"], m["ts"], m["ts"], m["seq"], before))
        newer = self.query("SELECT * FROM messages WHERE chat = ? AND (ts > ? OR (ts = ? AND seq > ?)) "
                           "ORDER BY ts, seq LIMIT ?", (m["chat"], m["ts"], m["ts"], m["seq"], after))
        return [dict(r) for r in reversed(older)] + [m] + [dict(r) for r in newer]

    def counts(self) -> dict:
        r = self.query("SELECT COUNT(*) AS n, COUNT(DISTINCT chat) AS chats, MIN(ts) AS first, MAX(ts) AS last "
                       "FROM messages")[0]
        return dict(r)

    def prune(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(timespec="seconds")
        return self.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount

    def delete_before(self, cutoff: str) -> int:
        return self.execute("DELETE FROM messages WHERE ts < ?", (cutoff,)).rowcount

    def wipe(self) -> None:
        """Every message, chat and contact: what "unpair --wipe" does."""
        with self._lock:
            for table in ("messages", "chats", "contacts", "kv"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")

    # ---- the stream, for consumers ----

    def feed(self, consumer: str, limit: int = 200) -> list[dict]:
        """Messages stored since `consumer` last acked, oldest first. Call ack() with the last seq handled."""
        cursor = int(self.kv_get(f"cursor:{consumer}", "0") or 0)
        rows = self.query("SELECT * FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", (cursor, limit))
        return [dict(r) for r in rows]

    def ack(self, consumer: str, seq: int) -> None:
        self.kv_set(f"cursor:{consumer}", str(int(seq)))

    # ---- people ----

    def set_contacts(self, contacts: list[dict]) -> int:
        rows = [(str(c.get("id") or f"{c.get('name', '')}|{c.get('number', '')}"), c.get("name", "") or "",
                 c.get("number", "") or "") for c in contacts if c.get("name") or c.get("number")]
        with self._lock:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM contacts")
            self.conn.executemany("INSERT OR REPLACE INTO contacts(id, name, number) VALUES (?, ?, ?)", rows)
            self.conn.execute("COMMIT")
        return len(rows)

    def people(self, query: str, limit: int = 10) -> list[dict]:
        """People matching a name or number: from the phone's contacts and from chat participants, each with the
        chats they're in and when you last talked."""
        q = query.strip().lower()
        d = digits(query)
        found: dict[str, dict] = {}

        def key(name: str, number: str) -> str:
            return digits(number)[-10:] or name.lower()

        def matches(name: str, number: str) -> bool:
            return bool((q and q in (name or "").lower()) or (len(d) >= 4 and d[-10:] in digits(number)))

        for c in self.query("SELECT name, number FROM contacts"):
            if matches(c["name"], c["number"]):
                found.setdefault(key(c["name"], c["number"]), {"name": c["name"], "numbers": set(), "chats": {}})
                found[key(c["name"], c["number"])]["numbers"].add(c["number"])
        for row in self.query("SELECT * FROM chats ORDER BY COALESCE(last_ts, '') DESC"):
            chat = self._chat(row)
            for p in chat["participants"]:
                if p.get("me") or not matches(p.get("name", ""), p.get("number", "")):
                    continue
                person = found.setdefault(key(p.get("name", ""), p.get("number", "")),
                                          {"name": p.get("name", ""), "numbers": set(), "chats": {}})
                person["name"] = person["name"] or p.get("name", "")
                if p.get("number"):
                    person["numbers"].add(p["number"])
                person["chats"][chat["id"]] = {"id": chat["id"], "name": self.chat_name(chat["id"], chat),
                                               "group": bool(chat["is_group"]), "last_ts": chat["last_ts"]}
        people = []
        for person in found.values():
            chats = sorted(person["chats"].values(), key=lambda c: c["last_ts"] or "", reverse=True)
            people.append({"name": person["name"], "numbers": sorted(n for n in person["numbers"] if n),
                           "chats": chats, "last_ts": chats[0]["last_ts"] if chats else None})
        people.sort(key=lambda p: p["last_ts"] or "", reverse=True)
        return people[:limit]


def fts_query(text: str) -> str:
    """Words as a safe FTS5 query: quoted phrases kept, every other word quoted, a trailing * kept as a prefix
    search. Words are ANDed."""
    parts = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', text):
        if phrase:
            parts.append('"' + phrase.replace('"', "") + '"')
            continue
        star = word.endswith("*")
        w = re.sub(r"[^\w']", "", word)
        if w:
            parts.append(f'"{w}"' + ("*" if star else ""))
    return " ".join(parts) or '""'
