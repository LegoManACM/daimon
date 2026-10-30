"""CONTEXT.md: what the agent is shown at the start of each turn, on top of its standing prompt.

The system prompt (SELF.md, POLICY.md, MEMORY.md, the worker file, guides, runtime facts) stays the same from step
to step, so providers can reuse it (prompt caching; a local model keeps it loaded). Anything that changes on its
own (the time, open tasks, today's calendar, recent texts, knowledge relevant to this message) goes here instead:
CONTEXT.md is rendered once when a turn starts and put in front of the message that started it.

CONTEXT.md is markdown with {tags}. A tag is replaced by what it names; a line that's just a tag disappears when
the tag comes out empty, and so does a heading left with nothing under it. <!-- comments --> are dropped.

    {now}                                   the date and time
    {tasks}  {projects}                     open tasks, active projects
    {knowledge}                             notes relevant to this message (from the knowledge folder)
    {spend}                                 model spend today
    {jobs}                                  background jobs still running
    {events:messages.*}                     recent add-on events (a name or glob), newest last
    {file:notes/today.md}                   a file in the home folder (knowledge notes included)
    {tool:calendar__calendar_events start=today days=2}
                                            a tool's result: any tool the agent has whose consent level is auto
                                            (read-only lookups; nothing that asks or changes things)

Options go after a |:  {tool:canvas__canvas_assignments | every=30m max=1500}
    every=10m   reuse the last result for this long (s, m, h) instead of fetching it every turn
    max=2000    cut the result to this many characters (default 4000)
    n=5         how many items, for {events}

Workers get their own: `context: <file>` in a worker's frontmatter names one (relative to the home folder);
without it they get WORKER_DEFAULT. A tag that fails says so in place ("(calendar__calendar_events failed: ...)")
and never stops the turn.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import reactions

if TYPE_CHECKING:
    from .gate import Tool
    from .runner import Runner

TAG = re.compile(r"\{([a-z_]+)(?::([^{}|]*))?(?:\|([^{}]*))?\}")
COMMENT = re.compile(r"<!--.*?-->", re.S)
DEFAULT_MAX = 4000
TAG_TIMEOUT_S = 60

MAIN_DEFAULT = "{now}\n\n## Open tasks\n{tasks}\n\n## Active projects\n{projects}\n\n## Relevant knowledge\n{knowledge}\n"
WORKER_DEFAULT = "{now}\n\n## Relevant knowledge\n{knowledge}\n"


def now_line() -> str:
    return f"Now: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M (UTC%z)')}"


def stamp(ts: float) -> str:
    """How a message's time is shown to the model."""
    return datetime.fromtimestamp(ts).astimezone().strftime("[%a %b %d %H:%M]")


def _seconds(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", text.strip())
    if not m:
        raise ValueError(f"every={text}: use a number with s, m, h or d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _options(text: str | None) -> dict[str, str]:
    out = {}
    for part in shlex.split(text or ""):
        key, eq, value = part.partition("=")
        if not eq:
            raise ValueError(f"option {part!r} needs a value, like max=2000")
        out[key.strip()] = value.strip()
    return out


def _value(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def tool_args(text: str) -> tuple[str, dict[str, Any]]:
    """`name key=value key2="two words" n=3` → (name, args). Values that read as JSON (numbers, true, lists)
    keep their type."""
    parts = shlex.split(text)
    if not parts:
        raise ValueError("{tool:...} needs a tool name")
    args = {}
    for part in parts[1:]:
        key, eq, value = part.partition("=")
        if not eq:
            raise ValueError(f"tool argument {part!r} should be key=value")
        args[key] = _value(value)
    return parts[0], args


def _heading(line: str) -> int:
    m = re.match(r"(#{1,6})\s", line)
    return len(m.group(1)) if m else 0


def tidy(lines: list[tuple[str, bool]]) -> str:
    """Drop the file's own headings that have nothing under them, and squeeze blank lines. `lines` are (text,
    written in the file); headings that came out of a tag (a note's title) are content, never dropped."""
    out: list[str] = []
    for i, (line, own) in enumerate(lines):
        level = _heading(line) if own else 0
        if level:
            has_content = False
            for nxt, nxt_own in lines[i + 1:]:
                if nxt_own and 0 < _heading(nxt) <= level:
                    break
                if nxt.strip() and not (nxt_own and _heading(nxt)):
                    has_content = True
                    break
            if not has_content:
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


class Renderer:
    """Renders a context file for a turn. Holds the `every=` cache, so one Renderer lives as long as the runner."""

    def __init__(self, runner: "Runner"):
        self.runner = runner
        self.cache: dict[str, tuple[float, str]] = {}

    def source(self, worker_meta: dict[str, Any] | None = None, main: bool = True) -> str:
        home = self.runner.home
        name = (worker_meta or {}).get("context")
        if name:
            try:
                p = home.agent_path(str(name))
                text = p.read_text(encoding="utf-8") if p.is_file() else ""
            except ValueError:
                text = ""
            if not text:
                return f"(context file {name} not found)\n\n" + (MAIN_DEFAULT if main else WORKER_DEFAULT)
            return text
        if main:
            return home.read("CONTEXT.md") or MAIN_DEFAULT
        return WORKER_DEFAULT

    async def render(self, text: str, about: str, tools: dict[str, "Tool"], who: str = "main") -> str:
        """Fill in the tags. `about` is the message or task the turn is for (what {knowledge} searches)."""
        text = COMMENT.sub("", text)
        tags = {m.group(0): m for m in TAG.finditer(text)}
        values = await asyncio.gather(*(self._tag(m, about, tools, who) for m in tags.values()))
        filled = dict(zip(tags, values))
        lines: list[tuple[str, bool]] = []
        for line in text.splitlines():
            if not TAG.search(line):
                lines.append((line, True))
                continue
            new = TAG.sub(lambda m: filled.get(m.group(0), m.group(0)), line)
            if not new.strip():
                continue  # a line that was only tags, all empty
            lines += [(part, False) for part in new.splitlines()]
        return tidy(lines)

    async def _tag(self, m: re.Match, about: str, tools: dict[str, "Tool"], who: str) -> str:
        name, arg, opts_text = m.group(1), (m.group(2) or "").strip(), m.group(3)
        try:
            opts = _options(opts_text)
            limit = int(opts.get("max", DEFAULT_MAX))
            every = _seconds(opts["every"]) if "every" in opts else 0
            key = m.group(0)
            if every and key in self.cache and self.cache[key][0] > time.monotonic():
                return self.cache[key][1]
            value = await asyncio.wait_for(self._value(name, arg, opts, about, tools, who), TAG_TIMEOUT_S)
            value = (value or "").strip()
            if len(value) > limit:
                value = value[:limit] + f"\n…[cut at {limit} chars]"
            if every:
                self.cache[key] = (time.monotonic() + every, value)
            return value
        except asyncio.TimeoutError:
            return f"({m.group(0)} took over {TAG_TIMEOUT_S}s and was skipped)"
        except Exception as e:  # noqa: BLE001 - a broken tag must never stop the turn
            return f"({m.group(0)} failed: {e})"

    async def _value(self, name: str, arg: str, opts: dict[str, str], about: str, tools: dict[str, "Tool"],
                     who: str) -> str:
        r = self.runner
        if name == "now":
            return now_line()
        if name == "tasks":
            from .tasks import OPEN, summary
            return summary(r.tasks.all(OPEN))
        if name == "projects":
            from .projects import summary
            return summary(r.projects.all(("active",)))
        if name == "knowledge":
            return await r.retrieve(about) if about.strip() else ""
        if name == "spend":
            return f"Model spend today: ${r.spend_today():.4f}"
        if name == "jobs":
            return r.jobs.describe(who)
        if name == "events":
            rows = reactions.recent(r.home, int(opts.get("n", 5)), arg or "*")
            return "\n".join(f"- {datetime.fromtimestamp(x['ts']):%m-%d %H:%M} {x['event']}: "
                             f"{json.dumps(x['data'], ensure_ascii=False, default=str)}" for x in rows)
        if name == "file":
            if not arg:
                raise ValueError("{file:...} needs a path")
            # The same checks as the agent's own file tools: nothing outside its files, no internal state.
            p = r.kb.path(arg) if arg.startswith("knowledge/") else r.home.agent_path(arg)
            return p.read_text(encoding="utf-8") if p.is_file() else f"({arg} doesn't exist)"
        if name == "tool":
            tool_name, args = tool_args(arg)
            tool = tools.get(tool_name)
            if tool is None:
                raise ValueError(f"no tool named {tool_name}")
            if tool.level(args) != "auto":
                raise ValueError(f"{tool_name} isn't an auto tool, so it can't run as context")
            result = await tool.run(args)
            if result.startswith("Error:"):
                raise ValueError(result[6:].strip())
            return result
        raise ValueError("unknown tag; there are now, tasks, projects, knowledge, spend, jobs, events, file, tool")
