"""Model providers for setup: checking an API key, and the provider's own live list of models to pick from."""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

OLLAMA = "http://localhost:11434"


@dataclass
class Provider:
    key: str
    title: str
    env: str | None      # the secret holding its API key
    models: list[str]    # suggestions when the live list can't be fetched
    where: str = ""      # where to get a key
    prefix: str = ""     # what its keys start with


PROVIDERS = {
    "anthropic": Provider("anthropic", "Anthropic (Claude)", "ANTHROPIC_API_KEY",
                          ["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"],
                          "https://console.anthropic.com/settings/keys", "sk-ant-"),
    "openai": Provider("openai", "OpenAI", "OPENAI_API_KEY", ["openai/gpt-5", "openai/gpt-5-mini"],
                       "https://platform.openai.com/api-keys", "sk-"),
    "openrouter": Provider("openrouter", "OpenRouter (hundreds of models, one key)", "OPENROUTER_API_KEY",
                           ["openrouter/anthropic/claude-sonnet-5.5", "openrouter/openai/gpt-5"],
                           "https://openrouter.ai/keys", "sk-or-"),
    "gemini": Provider("gemini", "Google Gemini", "GEMINI_API_KEY", [], "https://aistudio.google.com/apikey", "AIza"),
    "ollama": Provider("ollama", "Ollama (runs on this computer, free, no key)", None, ["ollama_chat/qwen3.5:9b"],
                       "https://ollama.com"),
}


@dataclass
class Model:
    id: str                       # the LiteLLM id, e.g. openrouter/anthropic/claude-sonnet-5.5
    name: str = ""
    context: int = 0              # tokens
    price: tuple[float, float] | None = None  # USD per million tokens, in / out

    def describe(self) -> str:
        bits = [self.name] if self.name and self.name != self.id.split("/", 1)[-1] else []
        if self.context:
            bits.append(f"{self.context // 1000}k context" if self.context < 1_000_000 else
                        f"{self.context / 1_000_000:g}M context")
        if self.price:
            bits.append("free" if self.price == (0, 0) else f"${self.price[0]:g} in / ${self.price[1]:g} out per M")
        return " · ".join(bits)


class KeyProblem(Exception):
    """The provider turned the key down. The message says so in plain words."""


def clean_key(raw: str, env: str | None = None) -> str:
    """What was pasted, minus the usual extras: whitespace, quotes, a `NAME=` or `Bearer ` in front."""
    key = raw.strip().strip("'\"").strip()
    if env and key.upper().startswith(env + "="):
        key = key[len(env) + 1:].strip().strip("'\"")
    key = re.sub(r"^(Bearer|export)\s+", "", key, flags=re.I)
    return re.sub(r"\s+", "", key)


def masked(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 14 else "*" * len(key)


def _get(url: str, **kw) -> dict:
    try:
        r = httpx.get(url, timeout=20, **kw)
    except httpx.HTTPError as e:
        raise ConnectionError(f"couldn't reach {httpx.URL(url).host}: {type(e).__name__}") from None
    if r.status_code in (400, 401, 403):
        raise KeyProblem(_reason(r))
    r.raise_for_status()
    return r.json()


def _reason(r: httpx.Response) -> str:
    try:
        err = r.json().get("error", {})
        detail = err.get("message", "") if isinstance(err, dict) else str(err)
    except ValueError:
        detail = ""
    plain = "the provider didn't accept this key"
    return f"{plain} ({detail})" if detail else plain


def fetch_models(p: Provider, key: str = "") -> list[Model]:
    """The provider's current models, newest-ish first. Checks the key on the way: raises KeyProblem if it's
    turned down, ConnectionError if the provider can't be reached."""
    if p.key == "anthropic":
        data = _get("https://api.anthropic.com/v1/models?limit=1000",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        return [Model(f"anthropic/{m['id']}", m.get("display_name", "")) for m in data.get("data", [])]
    if p.key == "openai":
        data = _get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"})
        chat = [m for m in data.get("data", []) if re.match(r"(gpt-|o\d|chatgpt-)", m["id"]) and not re.search(
            r"audio|realtime|transcribe|tts|image|search|embedding|moderation", m["id"])]
        return [Model(f"openai/{m['id']}") for m in sorted(chat, key=lambda m: -m.get("created", 0))]
    if p.key == "openrouter":
        _get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})  # the list is public
        data = _get("https://openrouter.ai/api/v1/models")
        out = []
        for m in data.get("data", []):
            if m["id"].endswith(":batch"):
                continue
            pr = m.get("pricing") or {}
            try:
                price = (round(float(pr["prompt"]) * 1e6, 4), round(float(pr["completion"]) * 1e6, 4))
            except (KeyError, TypeError, ValueError):
                price = None
            out.append(Model(f"openrouter/{m['id']}", m.get("name", ""), m.get("context_length") or 0, price))
        return out
    if p.key == "gemini":
        data = _get("https://generativelanguage.googleapis.com/v1beta/models", params={"key": key, "pageSize": 1000})
        return [Model("gemini/" + m["name"].removeprefix("models/"), m.get("displayName", ""),
                      m.get("inputTokenLimit") or 0)
                for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    if p.key == "ollama":
        models = ollama_models()
        if models is None:
            raise ConnectionError(f"Ollama isn't answering at {OLLAMA}")
        return [Model(m) for m in models]
    return []


def ollama_models(base: str = OLLAMA) -> list[str] | None:
    """Local Ollama models as LiteLLM ids, or None if Ollama isn't reachable."""
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3)
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return [f"ollama_chat/{m['name']}" for m in r.json().get("models", [])]


def search(models: list[Model], query: str) -> list[Model]:
    """Models matching every word of the query, in the id or the name."""
    words = query.lower().split()
    return [m for m in models if all(w in f"{m.id} {m.name}".lower() for w in words)]
