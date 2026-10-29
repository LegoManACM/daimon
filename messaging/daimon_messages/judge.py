"""Asking a watcher's model about some texts, through LiteLLM, so any provider works: a local Ollama or LM Studio
model, or a cloud one (Anthropic, OpenAI, OpenRouter, Gemini, ...). LiteLLM comes with Daimon; installed on its
own, `pip install litellm`.

Small local models wander from the format, so the answer is read leniently: the first JSON object in the reply,
with any <think> block dropped.
"""
from __future__ import annotations

import json
import re
from typing import Any

TIMEOUT_S = 120


async def ask(model: str, params: dict[str, Any], messages: list[dict]) -> dict:
    try:
        import litellm
    except ImportError as e:
        raise RuntimeError("Watchers that read texts need LiteLLM (`pip install litellm`).") from e
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    resp = await litellm.acompletion(model=model, messages=messages, temperature=0, max_tokens=600,
                                     timeout=TIMEOUT_S, **params)
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
