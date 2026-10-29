"""The inbox drop folder: how other processes (a messaging bridge, an email curator, a script) feed the agent.

Write a markdown file into $DAIMON_HOME/inbox/, or call drop(). Frontmatter is optional:
  source:   where it came from (sms, email, calendar, ...)
  from:     who it's from
  title:    a short subject
  for:      "main" to hand it straight to the main agent (something needing attention now);
            otherwise it goes to the curator worker, which files what's worth keeping into knowledge/
Send curated facts or messages worth knowing, not raw firehoses.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home import Home, join_frontmatter, split_frontmatter
from .schedule import now

SETTLE_SECONDS = 2  # ignore files modified this recently: the writer may not be finished


@dataclass
class InboxItem:
    path: Path
    meta: dict[str, Any]
    body: str

    def describe(self, limit: int = 4000) -> str:
        head = ", ".join(f"{k}: {v}" for k, v in self.meta.items() if k in ("source", "from", "title", "received"))
        body = self.body.strip()
        return f"### {self.path.name}" + (f" ({head})" if head else "") + "\n" + \
            (body if len(body) <= limit else body[:limit] + "\n…[truncated]")


def folder(home: Home) -> Path:
    return home.root / "inbox"


def drop(home: Home, text: str, **meta: Any) -> Path:
    """Add an item to the inbox. Usable from other Python code; `daimon inbox add` wraps it."""
    folder(home).mkdir(parents=True, exist_ok=True)
    meta = {k: v for k, v in meta.items() if v} | {"received": now().isoformat(timespec="minutes")}
    slug = re.sub(r"[^a-z0-9]+", "-", str(meta.get("title") or meta.get("source") or "item").lower()).strip("-")[:30]
    path = folder(home) / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 10**6:06d}-{slug or 'item'}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(join_frontmatter(meta, text), encoding="utf-8")
    tmp.replace(path)  # atomic: the agent never sees a half-written file
    return path


def take(home: Home, limit: int = 20) -> list[InboxItem]:
    """Claim settled items: they're moved to inbox/processed/ and returned."""
    src = folder(home)
    if not src.exists():
        return []
    cutoff = time.time() - SETTLE_SECONDS
    files = [p for p in sorted(src.iterdir()) if p.is_file() and p.suffix in (".md", ".txt")
             and p.stat().st_mtime < cutoff][:limit]
    done = src / "processed" / f"{now():%Y-%m-%d}"
    items = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, body = split_frontmatter(text) if p.suffix == ".md" else ({}, text)
        done.mkdir(parents=True, exist_ok=True)
        target = done / p.name
        p.replace(target)
        items.append(InboxItem(target, meta, body))
    return items
