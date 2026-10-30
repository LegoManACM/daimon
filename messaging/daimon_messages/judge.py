"""Asking a watcher's model about some texts, through LiteLLM, so any provider works: a local Ollama or LM Studio
model, or a cloud one (Anthropic, OpenAI, OpenRouter, Gemini, ...). LiteLLM comes with Daimon; installed on its
own, `pip install litellm`.

With Daimon installed alongside (the usual way), calls go through daimon.llm: a local model is kept loaded
between texts (model_calls.ollama.keep_alive in the agent's config.yaml), started loading as soon as a text is
waiting to be read (`warm`), retried once if it times out or drops, and a failure says what's actually wrong
(Ollama not running, the model not pulled, still loading).

Small local models wander from the format, so the answer is read leniently: the first JSON object in the reply,
with any <think> block dropped.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

TIMEOUT_S = 300


def _llm() -> Any:
    try:
        from daimon import llm
    except ImportError:
        return None
    return llm


def call_settings(home: Path | None, timeout_s: float) -> Any:
    """daimon.llm settings from the agent's config.yaml (so a local model is loaded the same way for both), with
    the watchers' own timeout."""
    llm = _llm()
    cfg = {}
    if home and (home / "config.yaml").exists():
        try:
            cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            cfg = {}
    s = llm.Settings.from_config(cfg)
    s.timeout_s = float(timeout_s)
    s.retries = 1  # then the watcher tries the chat again later
    return s


def warm(model: str, params: dict[str, Any], home: Path | None = None) -> None:
    """Start loading a local model now, so it's ready when the chat goes quiet and gets read."""
    llm = _llm()
    if llm is not None and model:
        llm.warm(model, params, call_settings(home, TIMEOUT_S))


async def ask(model: str, params: dict[str, Any], messages: list[dict], timeout_s: float = TIMEOUT_S,
              home: Path | None = None) -> dict:
    try:
        import litellm
    except ImportError as e:
        raise RuntimeError("Watchers that read texts need LiteLLM (`pip install litellm`).") from e
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    params = {"temperature": 0, "max_tokens": 600, **params}  # a watcher's params may change either
    llm = _llm()
    if llm is None:
        resp = await litellm.acompletion(model=model, messages=messages, timeout=timeout_s, **params)
    else:
        try:
            resp = await llm.call(model, messages, None, params, call_settings(home, timeout_s))
        except Exception as e:
            raise RuntimeError(await llm.explain(model, params, e, timeout_s)) from e
    return read_verdict(resp.choices[0].message.content or "")


def read_verdict(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            match = obj.get("match")
            obj["match"] = match is True or str(match).strip().lower() in ("true", "yes")
            obj["evidence"] = [str(e) for e in obj.get("evidence") or [] if e]
            obj["fields"] = obj.get("fields") if isinstance(obj.get("fields"), dict) else {}
            return obj
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON in the model's answer: {text[:200]!r}")
