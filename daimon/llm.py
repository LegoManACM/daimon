"""Model calls that survive slow models: a local one loading for minutes, a provider that stalls or drops.

Every call goes through `call`. It waits generously for the first token (a local model may still be loading into
memory), then only as long as the stream stays silent, and retries what's worth retrying (connection errors, rate
limits, timeouts) with a backoff, instead of LiteLLM's own retries that would start the same slow wait over.

Claude models get their prompt marked for caching (other providers cache a repeated prefix on their own), which
pays off because the system prompt stays the same from step to step: what changes goes in the turn's context
(see context.py).

Ollama gets extra care, because it's the usual local model:
  - keep_alive: Ollama unloads a model 5 minutes after its last use, so a personal agent pays the load on almost
    every call. Daimon asks it to keep the model loaded longer (config.yaml model_calls.ollama.keep_alive).
  - num_ctx: Ollama's default context window is small, and it cuts a longer prompt silently. Calls get a fixed
    window (model_calls.ollama.num_ctx) unless the model's params set one. It must be the same on every call:
    Ollama reloads the model when it changes.
  - warm: start loading a model before it's needed (when the agent starts, when a turn begins, when a watcher has
    texts waiting to be read).
  - explain: when a call fails, say what's actually wrong (Ollama not running, model not pulled, still loading).

The text watchers (daimon_messages) use this module too, when Daimon is installed alongside them.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

OLLAMA_PREFIXES = ("ollama/", "ollama_chat/")
OLLAMA_DEFAULT_BASE = "http://localhost:11434"


class ModelTimeout(Exception):
    """The model didn't start answering, or went silent, for longer than allowed."""


@dataclass
class Settings:
    first_token_timeout_s: float = 300.0  # sent → first token: covers a local model loading
    stall_timeout_s: float = 120.0        # longest silence inside a streamed answer
    timeout_s: float = 600.0              # a whole answer, when it isn't streamed
    retries: int = 2                      # after connection errors, rate limits and timeouts
    keep_alive: Any = "30m"               # Ollama: keep the model loaded this long after its last use
    num_ctx: int | None = 16384           # Ollama: context window (None: Ollama's own default)
    warm_up: bool = True                  # Ollama: load models before they're needed
    cache: bool = True                    # mark prompts cacheable for providers that need it (Anthropic)

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "Settings":
        cfg = cfg or {}
        mc = cfg.get("model_calls") or {}
        ol = mc.get("ollama") or {}
        s = cls()
        for key in ("first_token_timeout_s", "stall_timeout_s", "timeout_s"):
            if mc.get(key) is not None:
                setattr(s, key, float(mc[key]))
        if mc.get("retries") is not None:
            s.retries = int(mc["retries"])
        if "cache" in mc:
            s.cache = bool(mc["cache"])
        if "keep_alive" in ol:
            s.keep_alive = ol["keep_alive"]
        if "num_ctx" in ol:
            s.num_ctx = int(ol["num_ctx"]) if ol["num_ctx"] else None
        if "warm_up" in ol:
            s.warm_up = bool(ol["warm_up"])
        return s


# ---- Ollama --------------------------------------------------------------------------------------------------

def is_ollama(model: str) -> bool:
    return model.startswith(OLLAMA_PREFIXES)


def ollama_name(model: str) -> str:
    return model.split("/", 1)[1]


def ollama_base(params: dict[str, Any]) -> str:
    return (params.get("api_base") or os.environ.get("OLLAMA_API_BASE") or OLLAMA_DEFAULT_BASE).rstrip("/")


def prepare(model: str, params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """The provider params for a call: the model's own, plus Ollama's keep_alive and num_ctx when it doesn't set
    them."""
    params = dict(params)
    if is_ollama(model):
        if settings.keep_alive is not None:
            params.setdefault("keep_alive", settings.keep_alive)
        if settings.num_ctx:
            params.setdefault("num_ctx", settings.num_ctx)
    return params


def caches_on_request(model: str) -> bool:
    """Claude caches a prompt only where the request marks it; OpenAI, Gemini and Ollama reuse a repeated prefix
    on their own."""
    return "anthropic/" in model or "claude" in model.lower()


def _same_model(a: str, b: str) -> bool:
    return a == b or a == f"{b}:latest" or b == f"{a}:latest"


async def ollama_state(model: str, params: dict[str, Any]) -> str:
    """down | missing | loaded | not loaded: what Ollama says about this model right now."""
    base, name = ollama_base(params), ollama_name(model)
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            loaded = (await client.get(f"{base}/api/ps")).json().get("models") or []
            if any(_same_model(m.get("name", ""), name) or _same_model(m.get("model", ""), name) for m in loaded):
                return "loaded"
            pulled = (await client.get(f"{base}/api/tags")).json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return "down"
    if not any(_same_model(m.get("name", ""), name) or _same_model(m.get("model", ""), name) for m in pulled):
        return "missing"
    return "not loaded"


_warming: dict[str, asyncio.Task] = {}


def warm(model: str, params: dict[str, Any], settings: Settings) -> asyncio.Task | None:
    """Start loading an Ollama model in the background (no-op for other models, or if it's already loading).
    Returns the task, which callers may ignore."""
    if not is_ollama(model) or not settings.warm_up:
        return None
    params = prepare(model, params, settings)
    key = f"{ollama_base(params)}|{ollama_name(model)}|{params.get('num_ctx')}"
    running = _warming.get(key)
    if running and not running.done():
        return running

    async def load() -> None:
        if await ollama_state(model, params) != "not loaded":
            return  # loaded already, or nothing to load: the call itself will report it
        body: dict[str, Any] = {"model": ollama_name(model), "prompt": ""}  # an empty prompt only loads the model
        if params.get("keep_alive") is not None:
            body["keep_alive"] = params["keep_alive"]
        if params.get("num_ctx"):
            body["options"] = {"num_ctx": params["num_ctx"]}  # the same window the calls use, or it loads twice
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=max(settings.first_token_timeout_s, 60))) \
                    as client:
                await client.post(f"{ollama_base(params)}/api/generate", json=body)
        except httpx.HTTPError:
            pass  # warming is a head start, never required

    try:
        task = asyncio.get_running_loop().create_task(load())
    except RuntimeError:
        return None
    _warming[key] = task
    return task


async def explain(model: str, params: dict[str, Any], error: BaseException, waited: float | None = None) -> str:
    """A plain account of a failed call. For Ollama, asks it what's wrong."""
    what = str(error) if isinstance(error, ModelTimeout) else f"{type(error).__name__}: {error}"
    if not is_ollama(model):
        return what
    base, name = ollama_base(params), ollama_name(model)
    state = await ollama_state(model, params)
    if state == "down":
        return f"{what}. Ollama isn't answering at {base}: is it running?"
    if state == "missing":
        return f"{what}. Ollama doesn't have {name}: run `ollama pull {name}`."
    if isinstance(error, ModelTimeout):
        took = f" for {waited:.0f}s" if waited else ""
        if state == "not loaded":
            return (f"{what}. Ollama is running but {name} isn't loaded: it was probably still loading{took}. "
                    "On a slow computer, raise model_calls.first_token_timeout_s in config.yaml or use a smaller "
                    "model.")
        return (f"{what}. {name} is loaded but answered too slowly{took}: the computer may be busy, or the prompt "
                "too long for it.")
    return what


# ---- calls ---------------------------------------------------------------------------------------------------

def _retryable(e: BaseException) -> bool:
    if isinstance(e, (ModelTimeout, asyncio.TimeoutError, httpx.TransportError)):
        return True
    try:
        from litellm import exceptions as x
    except ImportError:
        return False
    return isinstance(e, (x.APIConnectionError, x.RateLimitError, x.ServiceUnavailableError,
                          x.InternalServerError, x.BadGatewayError, x.Timeout))


Feed = Callable[[Any], None]  # gets each streamed chunk


async def call(model: str, messages: list[dict], tools: list[dict] | None, params: dict[str, Any],
               settings: Settings, feed: Feed | None = None,
               on_retry: Callable[[int, str], None] | None = None) -> Any:
    """One completion, streamed through `feed` when given. Returns the same response a plain call returns.
    Raises the last error (a ModelTimeout for a slow model) once retries run out."""
    import litellm
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    params = prepare(model, params, settings)
    params.pop("num_retries", None)  # retries happen here, around the timeouts
    if settings.cache and caches_on_request(model):
        # The system prompt and the conversation so far: the next step and the next turn reuse both.
        params.setdefault("cache_control_injection_points", [{"location": "message", "role": "system"},
                                                             {"location": "message", "index": -1}])
    attempts = max(settings.retries, 0) + 1
    for attempt in range(1, attempts + 1):
        try:
            if feed is None:
                return await asyncio.wait_for(
                    litellm.acompletion(model=model, messages=messages, tools=tools or None,
                                        timeout=settings.timeout_s, num_retries=0, **params),
                    settings.timeout_s + 5)
            return await _streamed(litellm, model, messages, tools, params, settings, feed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt == attempts or not _retryable(e):
                raise
            if on_retry:
                on_retry(attempt, f"{type(e).__name__}: {e}"[:200])
            await asyncio.sleep(min(2 * 4 ** (attempt - 1), 30))
    raise AssertionError("unreachable")


async def _streamed(litellm: Any, model: str, messages: list[dict], tools: list[dict] | None,
                    params: dict[str, Any], settings: Settings, feed: Feed) -> Any:
    started = time.monotonic()
    # The HTTP client's own read timeout must not fire before ours do.
    http_timeout = max(settings.first_token_timeout_s, settings.stall_timeout_s) + 30

    async def first() -> tuple[Any, Any]:
        stream = await litellm.acompletion(model=model, messages=messages, tools=tools or None, stream=True,
                                           stream_options={"include_usage": True}, timeout=http_timeout,
                                           num_retries=0, **params)
        it = stream.__aiter__()
        try:
            return it, await it.__anext__()
        except StopAsyncIteration:
            return it, None

    try:
        it, chunk = await asyncio.wait_for(first(), settings.first_token_timeout_s)
    except asyncio.TimeoutError:
        raise ModelTimeout(f"{model} didn't start answering within {settings.first_token_timeout_s:.0f}s") from None
    if chunk is None:
        raise ModelTimeout(f"{model}'s stream ended without a response")
    chunks = [chunk]
    feed(chunk)
    while True:
        try:
            chunk = await asyncio.wait_for(it.__anext__(), settings.stall_timeout_s)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise ModelTimeout(f"{model} went silent for {settings.stall_timeout_s:.0f}s mid-answer "
                               f"({time.monotonic() - started:.0f}s in)") from None
        chunks.append(chunk)
        feed(chunk)
    resp = litellm.stream_chunk_builder(chunks, messages=messages)
    if resp is None:
        raise ModelTimeout(f"{model}'s stream ended without a response")
    return resp
