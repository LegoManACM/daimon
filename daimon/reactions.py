"""Reactions: standing rules that turn an event into a tool call, with no model in between.

Add-ons never know about each other. Each one exposes tools (MCP) and reports what it notices as events: an inbox
item whose frontmatter has `event: <addon>.<name>` and a flat `data:` dict. A reaction is a file in reactions/
that says which events it wants and which tool to call with what, filled in from the event's data:

    ---
    on: messages.watch_hit             # event name; globs and lists ok
    when: {watcher: plans}             # data conditions: a value, a glob, a list (any of), or "*" (present)
    call: calendar__calendar_add_event # any tool you have, as you see it named
    args:                              # {field} = required, {field?} = optional (an arg that's only an
      title: "{title}"                 #   optional field is left out when it's empty), {a.0} = list item
      start: "{start}"
      end: "{end?}"
      source: "sms:{chat_id}:{message_ids.0}"
    then: notify                       # after it worked: quiet | notify (one line) | tell (say it) | main (hand on)
    on_error: main                     # if a field is missing or the tool fails: main | tell | quiet
    pass: false                        # true: the event also goes on to its usual place (main / curator)
    ---
    Why this exists, for whoever reads it later.

The tool call goes through the consent gate like any other, so an `ask` tool still asks the user (who sees which
reaction wants it). Reactions can't edit your own files. Every event is logged (.cache/events.jsonl) with what
handled it, which `recent_events` shows; a successful reaction emits `reaction.done` so reactions can chain.
Routines emit `routine.<name>` when they fire.
"""
from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .home import Home, split_frontmatter

THEN = ("quiet", "notify", "tell", "main")
ON_ERROR = ("main", "tell", "quiet")
MAX_DEPTH = 4  # reaction.done chains
LOG_KEEP = 500
FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)(\?)?(?:\|([a-z]+))?\}")


@dataclass
class Event:
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""          # the human-readable body: what main reads if it ends up there
    depth: int = 0

    @classmethod
    def from_inbox(cls, meta: dict[str, Any], body: str) -> "Event":
        name = str(meta.get("event") or f"inbox.{meta.get('source') or 'item'}")
        data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
        extra = {k: v for k, v in meta.items() if k not in ("event", "data", "for")}
        return cls(name, {**extra, **data}, body)


@dataclass
class Reaction:
    name: str
    on: list[str]
    call: str
    args: dict[str, Any] = field(default_factory=dict)
    when: dict[str, Any] = field(default_factory=dict)
    then: str = "notify"
    on_error: str = "main"
    passes: bool = False
    enabled: bool = True
    notes: str = ""

    def matches(self, event: Event) -> bool:
        if not self.enabled or not any(fnmatch.fnmatchcase(event.name, p) for p in self.on):
            return False
        for key, want in self.when.items():
            have = lookup(event.data, key)
            wants = want if isinstance(want, list) else [want]
            if have is None or not any(_same(have, w) for w in wants):
                return False
        return True

    def render(self, data: dict[str, Any]) -> dict[str, Any]:
        """The tool's arguments, filled in from the event. Raises ValueError naming a missing field."""
        return {k: v for k, v in ((k, fill(v, data)) for k, v in self.args.items()) if v is not _OMIT}

    def describe(self) -> str:
        cond = f" when {json.dumps(self.when)}" if self.when else ""
        return f"{self.name}: on {', '.join(self.on)}{cond} → {self.call}" + ("" if self.enabled else " (disabled)")


def _same(have: Any, want: Any) -> bool:
    if want == "*":
        return have not in ("", None, [], {})
    if isinstance(have, list):
        return any(_same(h, want) for h in have)
    return have == want or fnmatch.fnmatchcase(str(have).lower(), str(want).lower())


def lookup(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


_OMIT = object()


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def fill(value: Any, data: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in ((k, fill(v, data)) for k, v in value.items()) if v is not _OMIT}
    if isinstance(value, list):
        return [v for v in (fill(v, data) for v in value) if v is not _OMIT]
    if not isinstance(value, str):
        return value
    whole = FIELD.fullmatch(value)
    if whole:  # just a field: keep its type (a list stays a list) unless a filter makes it text
        got = lookup(data, whole.group(1))
        if _empty(got):
            if whole.group(2):
                return _OMIT
            raise ValueError(f"the event has no {whole.group(1)}")
        return _filter(got, whole.group(3)) if whole.group(3) else got

    def sub(m: re.Match) -> str:
        got = lookup(data, m.group(1))
        if _empty(got):
            if m.group(2):
                return ""
            raise ValueError(f"the event has no {m.group(1)}")
        return _filter(got, m.group(3) or "text")
    return FIELD.sub(sub, value)


def _text(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


FILTERS = {
    "text": _text,
    "slug": lambda v: re.sub(r"[^a-z0-9]+", "-", _text(v).lower()).strip("-"),  # safe in paths and ids
    "lower": lambda v: _text(v).lower(),
    "upper": lambda v: _text(v).upper(),
    "date": lambda v: _text(v)[:10],              # an ISO date-time's date
    "time": lambda v: _text(v)[11:16],            # and its HH:MM
    "first": lambda v: _text(v[0] if isinstance(v, list) and v else v),
    "join": lambda v: ", ".join(map(_text, v)) if isinstance(v, list) else _text(v),
    "short": lambda v: _text(v) if len(_text(v)) <= 200 else _text(v)[:199] + "…",
}


def _filter(value: Any, name: str) -> str:
    if name not in FILTERS:
        raise ValueError(f"no filter |{name}; there are: {', '.join(FILTERS)}")
    return FILTERS[name](value)


def parse(name: str, text: str) -> Reaction:
    meta, body = split_frontmatter(text)
    on = meta.get("on", meta.get(True))  # YAML 1.1 reads a bare `on:` key as true
    on = [str(o) for o in (on if isinstance(on, list) else [on] if on else [])]
    r = Reaction(name=name, on=on, call=str(meta.get("call") or ""), args=meta.get("args") or {},
                 when=meta.get("when") or {}, then=str(meta.get("then") or "notify"),
                 on_error=str(meta.get("on_error") or "main"), passes=bool(meta.get("pass", False)),
                 enabled=meta.get("enabled", True) is not False, notes=body.strip())
    problems = []
    if not r.on:
        problems.append("`on` is missing: which events (e.g. messages.watch_hit)")
    if not r.call:
        problems.append("`call` is missing: which tool to call")
    if not isinstance(r.args, dict) or not isinstance(r.when, dict):
        problems.append("`args` and `when` must be mappings")
    if r.then not in THEN:
        problems.append(f"`then` must be one of {', '.join(THEN)}")
    if r.on_error not in ON_ERROR:
        problems.append(f"`on_error` must be one of {', '.join(ON_ERROR)}")
    if problems:
        raise ValueError(f"reactions/{name}.md: " + "; ".join(problems))
    return r


def load(home: Home) -> tuple[list[Reaction], list[str]]:
    """All reactions, plus problems with files that couldn't be read (reported, never fatal)."""
    out, errors = [], []
    for p in sorted((home.root / "reactions").glob("*.md")):
        try:
            out.append(parse(p.stem, p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e) if str(e).startswith("reactions/") else f"reactions/{p.name}: {e}")
    return out, errors


# ---- the event log ---------------------------------------------------------------------------------------

def _log_path(home: Home) -> Path:
    return home.cache / "events.jsonl"


def record(home: Home, event: Event, handled: list[str]) -> None:
    home.cache.mkdir(exist_ok=True)
    path = _log_path(home)
    row = {"ts": time.time(), "event": event.name, "data": _clip(event.data), "handled": handled}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) > LOG_KEEP * 2:
        path.write_text("\n".join(lines[-LOG_KEEP:]) + "\n", encoding="utf-8")


def _clip(data: Any, limit: int = 300) -> Any:
    if isinstance(data, dict):
        return {k: _clip(v, limit) for k, v in data.items()}
    if isinstance(data, list):
        return [_clip(v, limit) for v in data[:20]]
    if isinstance(data, str) and len(data) > limit:
        return data[:limit] + "…"
    return data


def recent(home: Home, limit: int = 10, pattern: str = "*") -> list[dict[str, Any]]:
    path = _log_path(home)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if fnmatch.fnmatchcase(r["event"], pattern)][-limit:]
