"""Live progress of model calls and tools, for surfaces that can show it (the terminal's status bar).

The runner streams each model call and feeds a Meter, which turns chunks into a small state dict and hands it to
`surface.progress(who, state)` if the surface has that method (channels that don't simply never hear about it):

  phase    waiting (sent, nothing back yet) | loading (a local model loading first) | retrying (after an error)
           | thinking (reasoning) | writing | calling (writing tool calls)
           | tool (running one) | done | idle (the turn or task is over)
  model, started, first_token (time.monotonic()), tokens (estimated while streaming, exact when done),
  tps (output tokens per second since the first one), tail (the end of what it's writing), text (the answer so far,
  without reasoning: the web page streams it), tool, usd
"""
from __future__ import annotations

import contextvars
import time
from typing import Any, Callable

# Who the current model call or tool is for ("main", "researcher · 20261029-..."): set by the runner's loop, so
# the meter can tell concurrent workers apart.
WHO: contextvars.ContextVar[str] = contextvars.ContextVar("who", default="main")

TAIL = 160
TEXT = 40_000  # how much of an answer in progress the state carries


def reporter(surface: Any) -> Callable[[str, dict], None] | None:
    return getattr(surface, "progress", None)


class Meter:
    def __init__(self, report: Callable[[str, dict], None], who: str, model: str):
        self.report, self.who = report, who
        self.state: dict[str, Any] = {"phase": "waiting", "model": model, "started": time.monotonic(),
                                      "first_token": None, "tokens": 0, "tps": 0.0, "tail": "", "text": ""}
        self.chars = 0
        self.send()

    def send(self) -> None:
        try:
            self.report(self.who, dict(self.state))
        except Exception:  # noqa: BLE001 - a display problem must never fail the model call
            pass

    def loading(self) -> None:
        """A local model that has to load into memory before it can answer."""
        self.state["phase"] = "loading"
        self.send()

    def retrying(self, attempt: int) -> None:
        self.state.update(phase="retrying", attempt=attempt, first_token=None, tokens=0, tail="", text="")
        self.chars = 0
        self.send()

    def feed(self, reasoning: str = "", text: str = "", tool_call: bool = False) -> None:
        if not (reasoning or text or tool_call):
            return
        s, now = self.state, time.monotonic()
        if s["first_token"] is None:
            s["first_token"] = now
        self.chars += len(reasoning) + len(text)
        s["tokens"] = max(s["tokens"] + 1, round(self.chars / 4))  # ~4 characters a token until the real count
        if text:
            s["phase"] = "writing"
            s["tail"] = (s["tail"] + text)[-TAIL:]
            if len(s["text"]) < TEXT:
                s["text"] += text
        elif reasoning:
            s["phase"] = "thinking"
            s["tail"] = (s["tail"] + reasoning)[-TAIL:]
        else:
            s["phase"] = "calling"
        elapsed = now - s["first_token"]
        s["tps"] = s["tokens"] / elapsed if elapsed > 0.2 else 0.0
        self.send()

    def done(self, usage: Any, usd: float = 0.0) -> dict:
        s, now = self.state, time.monotonic()
        out = getattr(usage, "completion_tokens", None)
        if out:
            s["tokens"] = out
        s["prompt_tokens"] = getattr(usage, "prompt_tokens", None) or 0
        first = s["first_token"] or now
        s["tps"] = s["tokens"] / (now - first) if now - first > 0.05 else 0.0
        s["seconds"] = now - s["started"]
        s["ttft"] = first - s["started"]
        s["usd"] = usd
        s["phase"] = "done"
        self.send()
        return s


def tool(surface: Any, who: str, name: str, running: bool) -> None:
    report = reporter(surface)
    if report:
        try:
            report(who, {"phase": "tool" if running else "waiting", "tool": name, "started": time.monotonic()})
        except Exception:  # noqa: BLE001
            pass


def idle(surface: Any, who: str) -> None:
    report = reporter(surface)
    if report:
        try:
            report(who, {"phase": "idle"})
        except Exception:  # noqa: BLE001
            pass
