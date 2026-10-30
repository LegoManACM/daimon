"""The terminal channel for `daimon chat` and `daimon ask`.

What the agent says prints above the prompt (Markdown, via rich). Under the prompt, a status bar (prompt_toolkit's
bottom toolbar) shows live what's happening: one line per model call or tool in flight, for you and every worker
and reaction in the background, with its phase, speed and the end of what it's writing, then a summary line. The
runner feeds it through `progress(who, state)` (see progress.py).

While chatting, approvals queue up and the next line you type answers the oldest one, so background workers never
fight you for the prompt. In one-shot mode it just asks inline.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .gateway import OnMessage, Prompt

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ACTIVE = ("waiting", "thinking", "writing", "calling", "tool")
PHASES = {"waiting": "waiting for the model", "thinking": "thinking", "writing": "writing",
          "calling": "choosing a tool", "tool": "running"}
EVENTS = {  # kind: (icon, rich style)
    "tool": ("→", "dim"), "edit": ("✎", "cyan"), "notify": ("•", "dim"), "compact": ("≡", "dim"),
    "thinking": ("…", "dim italic"), "task": ("▸", "green"), "routine": ("⟳", "green"), "error": ("!", "red"),
}


def console() -> Console:
    """A console on whatever sys.stdout is right now: while the prompt is up that's prompt_toolkit's proxy, which
    prints above the prompt and status bar."""
    width = shutil.get_terminal_size((100, 24)).columns
    return Console(file=sys.stdout, force_terminal=sys.__stdout__.isatty(), width=width, highlight=False,
                   soft_wrap=False)


def short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1] if model else ""


def short_who(who: str) -> str:
    name, _, task = who.partition(" · ")
    return f"{name} ·{task[-6:]}" if task else name  # task ids are long; the end tells them apart


def speed(state: dict[str, Any]) -> str:
    bits = []
    if state.get("tps"):
        bits.append(f"{state['tps']:.0f} tok/s")
    if state.get("tokens"):
        bits.append(f"{state['tokens']} tok")
    return "  ".join(bits)


def stats(state: dict[str, Any]) -> str:
    """The line under a reply: model, size, speed, latency, cost."""
    bits = [short_model(state.get("model", ""))]
    if state.get("tokens"):
        bits.append(f"{state['tokens']} tokens")
    if state.get("tps"):
        bits.append(f"{state['tps']:.0f} tok/s")
    if state.get("ttft") is not None:
        bits.append(f"first token {state['ttft']:.1f}s")
    if state.get("seconds"):
        bits.append(f"{state['seconds']:.1f}s")
    if state.get("usd"):
        bits.append(f"${state['usd']:.4f}")
    return " · ".join(b for b in bits if b)


@dataclass
class Pending:
    prompt: Prompt
    answer: asyncio.Future


class Terminal:
    name = "terminal"

    def __init__(self, interactive: bool):
        self.interactive = interactive
        self.pending: list[Pending] = []
        self.on_message: OnMessage | None = None
        self.activity: dict[str, dict[str, Any]] = {}  # who -> live state, while something is in flight
        self.turn: dict[str, Any] = {}                 # totals for main's current turn, shown under its reply
        self.summary: Callable[[], str] = lambda: ""   # the status bar's last line (open tasks, spend), from chat()

    async def connect(self, on_message: OnMessage) -> None:
        self.on_message = on_message

    async def close(self) -> None:
        pass

    # ---- what the agent says -------------------------------------------------------------------------
    async def send(self, text: str) -> None:
        out = console()
        if text.startswith("Model call failed:"):
            out.print(Text(text, style="red"))
            return
        out.print()
        out.print(Text("◆ daimon", style="bold magenta"))
        out.print(Markdown(text))
        if self.turn:
            out.print(Text(stats(self.turn), style="dim"))
            self.turn = {}

    def event(self, kind: str, text: str) -> None:
        icon, style = EVENTS.get(kind, ("·", "dim"))
        if kind == "thinking":  # what the model said alongside its tool calls
            text = " ".join(text.split())
            text = text if len(text) < 300 else text[:299] + "…"
        console().print(Text(f"{icon} {text}", style=style))

    # ---- live progress -------------------------------------------------------------------------------
    def progress(self, who: str, state: dict[str, Any]) -> None:
        phase = state.get("phase")
        if phase in ("idle",):
            self.activity.pop(who, None)
            return
        if phase == "done":
            if who == "main":  # add up the turn's model calls for the line under the reply
                t = self.turn
                t["model"] = state.get("model", "")
                t["tokens"] = t.get("tokens", 0) + (state.get("tokens") or 0)
                t["seconds"] = t.get("seconds", 0.0) + (state.get("seconds") or 0.0)
                t["usd"] = t.get("usd", 0.0) + (state.get("usd") or 0.0)
                t.setdefault("ttft", state.get("ttft"))
                if state.get("tps"):
                    t["tps"] = state["tps"]
            self.activity.pop(who, None)
            return
        prev = self.activity.get(who, {})
        if phase == "waiting" and prev.get("phase") == "tool":
            state = {**state, "started": time.monotonic()}  # back to the model after a tool
        self.activity[who] = {**({"model": prev.get("model")} if prev.get("model") else {}), **state}

    def status_lines(self, width: int | None = None) -> list[tuple[str, str]]:
        """The status bar as (style, text) lines: one per thing in flight, then the summary."""
        width = width or shutil.get_terminal_size((100, 24)).columns
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        lines = []
        order = sorted(self.activity, key=lambda w: (w != "main", w.startswith("reaction:"), w))  # you, workers, reactions
        for who in order:
            s = self.activity[who]
            phase = s.get("phase", "")
            if phase not in ACTIVE:
                continue
            elapsed = time.monotonic() - (s.get("started") or time.monotonic())
            what = f"{PHASES[phase]} {s.get('tool', '')}".strip() if phase == "tool" else PHASES[phase]
            parts = [f"{frame} {short_who(who):<14}", short_model(s.get("model", "")), what, speed(s), f"{elapsed:.0f}s"]
            line = "  ".join(p for p in parts if p)
            tail = " ".join((s.get("tail") or "").split())
            if tail and phase in ("writing", "thinking"):
                room = width - len(line) - 5
                if room > 12:
                    line += "  │ " + ("…" + tail[-room + 1:] if len(tail) > room else tail)
            style = "class:status.main" if who == "main" else "class:status.worker"
            lines.append((style, line[:width - 1]))
        if self.pending:
            p = self.pending[0].prompt
            more = f" (+{len(self.pending) - 1} more)" if len(self.pending) > 1 else ""
            lines.append(("class:status.ask", f"⏸ waiting for your answer: {p.title} ({short_who(p.who)}){more}"
                          [:width - 1]))
        busy = sum(1 for w in self.activity if w != "main")
        info = [f"{busy} working in the background" if busy else "", self.summary()]
        idle = "● ready" if "main" not in self.activity else "● working"
        lines.append(("class:status.info", "  ·  ".join(b for b in [idle, *info] if b)[:width - 1]))
        return lines

    def toolbar(self):
        from prompt_toolkit.formatted_text import FormattedText
        out = []
        for i, (style, text) in enumerate(self.status_lines()):
            out.append((style, ("\n" if i else "") + text))
        return FormattedText(out)

    # ---- approvals -----------------------------------------------------------------------------------
    async def prompt(self, prompt: Prompt) -> str:
        if not self.interactive:
            self._show(prompt)
            return await asyncio.to_thread(input, "> ")
        p = Pending(prompt, asyncio.get_running_loop().create_future())
        self.pending.append(p)
        if len(self.pending) == 1:
            self._show(prompt)
        try:
            return await p.answer
        except asyncio.CancelledError:  # answered on another channel
            if p in self.pending:
                first = self.pending[0] is p
                self.pending.remove(p)
                console().print(Text(f"({prompt.title}: answered elsewhere)", style="dim"))
                if first and self.pending:
                    self._show(self.pending[0].prompt)
            raise

    @staticmethod
    def _show(p: Prompt) -> None:
        details = Table.grid(padding=(0, 1))
        details.add_column(style="dim", no_wrap=True)
        details.add_column()
        for k, v in p.details.items():
            details.add_row(k, v)
        options = " / ".join(f"[{v}]{label[len(v):].lower()}" if label.lower().startswith(v) else f"[{v}] {label}"
                             for v, label in p.options)
        footer = Text(options + (" / or type a reply" if p.free_text else ""), style="yellow")
        console().print(Panel(Group(details, footer) if p.details else footer, border_style="yellow",
                              title=f"[bold yellow]Approve?[/] {p.title}", title_align="left",
                              subtitle=f"[dim]{p.who}[/]", subtitle_align="right"))

    def answer_oldest(self, line: str) -> None:
        p = self.pending.pop(0)
        p.answer.set_result(line)
        if self.pending:
            self._show(self.pending[0].prompt)


STYLE = {
    "bottom-toolbar": "noreverse bg:default",
    "status.main": "fg:ansimagenta",
    "status.worker": "fg:ansicyan",
    "status.ask": "fg:ansiyellow bold",
    "status.info": "fg:ansibrightblack",
    "prompt": "fg:ansigreen bold",
}


async def read_lines(terminal: Terminal | None = None) -> AsyncIterator[str]:
    """Lines you type. prompt_toolkit keeps the prompt and status bar in place while output prints above them."""
    if sys.stdin.isatty():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        session = PromptSession(bottom_toolbar=terminal.toolbar if terminal else None, refresh_interval=0.15,
                                style=Style.from_dict(STYLE))
        with patch_stdout(raw=True):
            while True:
                try:
                    yield await session.prompt_async([("class:prompt", "you› ")])
                except (EOFError, KeyboardInterrupt):
                    return
    else:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            yield line.rstrip("\n")


def status_table(agent) -> Table:
    """/status: what's running now, and the open tasks."""
    table = Table(box=None, show_header=True, header_style="bold", pad_edge=False)
    for col in ("", "worker", "task", "status", "when"):
        table.add_column(col)
    for t in agent.tasks.all(("running", "waiting", "queued", "proposed")):
        icon = {"running": "▸", "waiting": "⏸", "proposed": "?"}.get(t.status, "·")
        table.add_row(icon, t.worker, t.title, t.status, str(t.meta.get("run_at") or t.meta.get("due") or "")[5:16])
    return table
