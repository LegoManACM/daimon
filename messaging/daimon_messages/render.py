"""Turning stored messages into short text for the agent, and the agent's time words into UTC."""
from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta, timezone, tzinfo

RELATIVE = re.compile(r"^(\d+)\s*(m|min|h|hr|d|w)\w*$", re.I)
UNITS = {"m": "minutes", "min": "minutes", "h": "hours", "hr": "hours", "d": "days", "w": "weeks"}


def parse_when(text: str, tz: tzinfo, now: datetime | None = None) -> str:
    """'' | today | yesterday | 3d | 12h | 2w | 30m | 2026-10-20 | 2026-10-20 18:30 (local) -> UTC ISO, or ''.
    Raises ValueError on anything else."""
    text = (text or "").strip().lower()
    if not text:
        return ""
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    if text in ("today", "yesterday"):
        day = now.date() - timedelta(days=1 if text == "yesterday" else 0)
        start = datetime.combine(day, time(), tz)
    elif m := RELATIVE.match(text):
        start = now - timedelta(**{UNITS[m.group(2).lower()]: int(m.group(1))})
    else:
        try:
            start = datetime.fromisoformat(text.replace("t", "T").replace("z", "+00:00"))
        except ValueError:
            raise ValueError(f"Couldn't read the time {text!r}. Use e.g. 2026-10-20, 2026-10-20 18:30, today, "
                             "yesterday, 3d or 12h.") from None
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def local(ts: str | None, tz: tzinfo, with_date: bool = True) -> str:
    if not ts:
        return "never"
    dt = datetime.fromisoformat(ts).astimezone(tz)
    return dt.strftime("%a %b %d %H:%M" if with_date else "%H:%M")


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def body(m: dict, limit: int) -> str:
    text = clip(m.get("body") or "", limit)
    media = json.loads(m.get("media") or "[]") if isinstance(m.get("media"), str) else m.get("media") or []
    if media:
        kinds = ", ".join(sorted({(x.get("mime") or "file").split("/")[0] for x in media}))
        text = (text + " " if text else "") + f"[{len(media)} attachment{'s' if len(media) > 1 else ''}: {kinds}]"
    return text or "(empty)"


def who(m: dict) -> str:
    return "Me" if m.get("from_me") else (m.get("sender") or m.get("number") or "Them")


def line(m: dict, tz: tzinfo, limit: int = 300, with_id: bool = False) -> str:
    tail = f"  (id {m['id']})" if with_id else ""
    return f"[{local(m['ts'], tz)}] {who(m)}: {body(m, limit)}{tail}"
