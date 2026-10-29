"""Consent: every tool call passes through here. The gate never refuses on its own; `ask` routes the decision
to the user through whatever surface is attached (the terminal, Telegram: see gateway.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

LEVELS = ("auto", "notify", "ask")


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]
    level: Callable[[dict[str, Any]], str]
    source: str = "builtin"
    # Called when the user answers "always" to an ask, to make the tool auto from now on. None = not offered.
    allow_always: Callable[[], Awaitable[None]] | None = None

    def spec(self, level_hint: str) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name,
            "description": f"{self.description}\n[consent: {level_hint}]".strip(),
            "parameters": self.schema or {"type": "object", "properties": {}},
        }}


@dataclass
class Decision:
    approved: bool
    note: str = ""
    always: bool = False


class Surface(Protocol):
    """Where the agent meets the user. The CLI implements this; channel adapters will too."""

    async def approve(self, tool: Tool, args: dict[str, Any], can_always: bool, who: str) -> Decision: ...
    def event(self, kind: str, text: str) -> None: ...
    def say(self, text: str) -> None: ...  # the agent speaking unprompted (background results)


async def call(tool: Tool, args: dict[str, Any], surface: Surface, who: str = "main") -> str:
    try:
        level = tool.level(args)
    except Exception as e:  # a level check that fails (bad path, unknown model) is reported back to the model
        return f"Error: {e}"
    if level not in LEVELS:
        level = "ask"
    if level == "ask":
        decision = await surface.approve(tool, args, can_always=tool.allow_always is not None, who=who)
        if not decision.approved:
            return "The user declined this action." + (f' They said: "{decision.note}"' if decision.note else "")
        if decision.always and tool.allow_always:
            await tool.allow_always()
    try:
        result = await tool.run(args)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
    if level == "notify":
        surface.event("notify", f"{tool.name} {short(args)}")
    if level == "ask" and decision.note:
        result = f'(The user approved and added: "{decision.note}")\n{result}'
    return result


def short(args: dict[str, Any], limit: int = 120) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 1] + "…"
