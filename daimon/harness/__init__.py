"""Harnesses: external agents (Claude Code, Codex, Gemini CLI, your own) that run a worker's task instead of the
native loop. A worker opts in with `harness: <name>`.

Everything around the run is shared and harness-agnostic (see run.py): the workspace, the session to resume,
projects, the tools we lend the harness, consent and limits. A driver only knows how to talk to one kind of
agent: it gets a Job and returns an Outcome.

Harnesses are named in config.yaml under `harnesses:`, each with a `driver`:
  claude-sdk           Claude Code through the Claude Agent SDK (built in as `claude-code`)
  cli                  any headless command line (`codex exec`, `gemini -p`, ...), configured with templates
  package.module:Class your own Driver subclass
Every other key in the entry, and the worker's `harness_options`, is passed to the driver as options.

A harness worker's `model` is the harness's own model name (`opus`, `sonnet` for Claude Code), not a LiteLLM id:
the harness is the provider, so models.yaml doesn't apply. The entry's `models` lists the allowed names
(strings, or {id, approval: ask}); without it any name goes. `default_model` is used when the worker sets none.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..gate import Tool

BUILTIN = {
    "claude-code": {"driver": "claude-sdk", "default_model": "sonnet",
                    "models": ["opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
                               "claude-fable-5-1"]},
}


@dataclass
class Job:
    """One dispatch of a task to a harness."""
    task_id: str
    worker: str
    harness: str
    prompt: str                    # what to say now: the brief on a fresh start, or the answer / next task
    system: str                    # instructions to add to the harness's own system prompt
    workspace: Path
    options: dict[str, Any]        # the harness entry from config.yaml merged with the worker's harness_options
    session: str | None = None     # resume this session, or start fresh
    model: str | None = None
    tools: list[Tool] = field(default_factory=list)  # ours, lent to the harness (kg_search, ask_main, ...)
    # Consent for the harness's own actions, for drivers that can intercept them: returns (allowed, note).
    approve: Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]] | None = None
    event: Callable[[str], None] = lambda _text: None  # a step worth showing (tool use, progress)
    stop: Callable[[], bool] = lambda: False           # true once the harness asked main a question


@dataclass
class Outcome:
    text: str                      # the harness's final message
    session: str | None = None     # the session to resume next time
    cost_usd: float = 0.0
    error: str = ""                # set when the run failed
    retry_at: float | None = None  # unix time: rate-limited, try again then
    started: bool = True           # false if it failed before doing anything (e.g. the session to resume is gone)


class Driver:
    """Subclass and implement run(). `options` comes from config.yaml and the worker file."""
    lends_tools = False  # true if the driver gives the harness our tools (otherwise run.py uses text markers)

    def __init__(self, name: str, options: dict[str, Any]):
        self.name, self.options = name, options

    async def run(self, job: Job) -> Outcome:
        raise NotImplementedError


def entries(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    configured = config.get("harnesses") or {}
    return {name: {**BUILTIN.get(name, {}), **(configured.get(name) or {})} for name in {**BUILTIN, **configured}}


def model_entry(name: str, model: str | None, config: dict[str, Any]) -> dict[str, Any]:
    """The harness's entry for a worker's model ({} if the harness allows any). Raises if it isn't allowed."""
    entry = entries(config).get(name)
    if entry is None:
        raise ValueError(f"no harness named {name}. Known: {', '.join(entries(config))}")
    allowed = entry.get("models")
    if not model or allowed is None:
        return {}
    for m in allowed:
        m = m if isinstance(m, dict) else {"id": m}
        if m.get("id") == model:
            return m
    ids = [m["id"] if isinstance(m, dict) else m for m in allowed]
    raise ValueError(f"{model} isn't a {name} model. {name} runs its own models (on its own account, e.g. your "
                     f"Claude subscription), so models.yaml doesn't apply: use one of {', '.join(ids)}.")


def describe(config: dict[str, Any]) -> list[str]:
    """One line per harness for the main agent's prompt."""
    out = []
    for name, entry in entries(config).items():
        ids = [m["id"] if isinstance(m, dict) else m for m in entry.get("models") or []]
        auth = entry.get("auth", "subscription") if entry.get("driver") == "claude-sdk" else None
        out.append(f"{name} (models: {', '.join(ids) or 'any'}" + (f"; default {entry['default_model']}"
                   if entry.get("default_model") else "") + (f"; billed to the {auth}" if auth else "") + ")")
    return out


def load(name: str, config: dict[str, Any]) -> Driver:
    entry = entries(config).get(name)
    if entry is None:
        raise ValueError(f"no harness named {name}. Known: {', '.join(entries(config))}. Add one under "
                         "`harnesses:` in config.yaml.")
    driver = str(entry.get("driver", "cli"))
    if driver == "claude-sdk":
        from .claude_sdk import ClaudeSdk as cls
    elif driver == "cli":
        from .cli import Cli as cls
    elif ":" in driver:
        module, _, attr = driver.partition(":")
        cls = getattr(importlib.import_module(module), attr)
    else:
        raise ValueError(f"harness {name}: unknown driver {driver!r} (claude-sdk, cli, or package.module:Class)")
    return cls(name, {k: v for k, v in entry.items() if k != "driver"})
