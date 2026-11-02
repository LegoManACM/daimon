"""Settings for the web page: config.yaml, models.yaml, provider keys, the agent's files, the background service
and the checklist after the fast-track setup. What `daimon config` does, done from a browser.

It reads and writes the same files the agent and the CLI use, each change a commit like any other.
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any

import yaml

from ..home import TEMPLATES, Home

EDITABLE = {"config.yaml", "models.yaml", "mcp.json", "POLICY.md", "SELF.md", "MEMORY.md", "CONTEXT.md"}


# ---- config.yaml: what the page can change -----------------------------------------------------------
@dataclass
class Setting:
    key: str                  # dotted path in config.yaml
    label: str
    help: str = ""
    kind: str = "int"         # int, bool, text, choice (a few, as buttons), select (a list, as a dropdown),
                              # limit (a number, or empty for no limit)
    options: list[tuple[Any, str]] = field(default_factory=list)
    restart: bool = False     # takes effect when the agent restarts
    unit: str = ""

    @property
    def path(self) -> list[str]:
        return self.key.split(".")


SECTIONS: list[tuple[str, str, list[Setting]]] = [
    ("behaviour", "Behaviour & approvals", [
        Setting("self_edit", "Who approves its edits to its own files", "Policy: workers, skills and memory change "
                "freely; POLICY.md, add-ons, models and settings need your OK.", "choice",
                [("all", "All need my OK"), ("policy", "Policy"), ("free", "Free")]),
        Setting("max_steps_per_turn", "Steps per message", "Tool-call rounds before it stops and reports. Longer "
                "turns cost more."),
        Setting("memory_max_chars", "Memory size", "MEMORY.md can't grow past this; it condenses instead.",
                unit="characters"),
        Setting("thread.compact_at_tokens", "Summarise the conversation past", "Older messages fold into a summary.",
                unit="tokens"),
        Setting("thread.keep_recent_messages", "Recent messages kept word for word"),
        Setting("tool_result_max_chars", "Longest tool result", "Longer results are cut.", unit="characters"),
    ]),
    ("calls", "Model calls", [
        Setting("model_calls.first_token_timeout_s", "Wait for the first word", "A local model can take minutes to "
                "load the first time.", unit="seconds"),
        Setting("model_calls.stall_timeout_s", "Longest silence mid-answer", unit="seconds"),
        Setting("model_calls.timeout_s", "Longest answer that isn't streamed", unit="seconds"),
        Setting("model_calls.retries", "Retries", "After connection errors, rate limits and timeouts."),
        Setting("model_calls.cache", "Let Claude cache the prompt", "Cheaper and faster between steps.", "bool"),
        Setting("model_calls.ollama.keep_alive", "Keep local models loaded", "Ollama, after the last use.", "choice",
                [(0, "Unload"), ("10m", "10 min"), ("30m", "30 min"), ("2h", "2 h"), (-1, "Forever")]),
        Setting("model_calls.ollama.num_ctx", "Local context window", "Ollama's own default is small and cuts long "
                "prompts without saying so.", unit="tokens"),
        Setting("model_calls.ollama.warm_up", "Warm up local models", "Start loading them before they're needed.",
                "bool"),
    ]),
    ("tools", "Tools & jobs", [
        Setting("tools.background_after_s", "Run slow tools in the background after", "0: every call waits until "
                "it's done.", unit="seconds"),
        Setting("tools.job_max_minutes", "Stop a background job after", unit="minutes"),
        Setting("tools.mcp_timeout_s", "Longest add-on tool call", restart=True, unit="seconds"),
    ]),
    ("workers", "Workers", [  # title and a runs-per-day row per harness: see sections()
        Setting("limits.max_concurrent_workers", "Workers at once", "Empty: no limit.", "limit"),
        Setting("harnesses.claude-code.auth", "Claude Code bills", "", "choice",
                [("subscription", "My Claude subscription"), ("api_key", "ANTHROPIC_API_KEY")], restart=True),
        Setting("harness.workspaces", "Workspaces folder", "Where project and task workspaces go. Empty: a folder "
                "next to the home folder.", "text"),
    ]),
    ("knowledge", "Knowledge", [
        Setting("knowledge.folder", "Notes folder", "A folder in the home, or any path (an Obsidian vault).", "text"),
        Setting("knowledge.embeddings", "Meaning-based search", "Finds notes by what they mean, not only their words. "
                "Picked from a provider's embedding models.", "text"),
        Setting("knowledge.retrieve_for_main", "Look up notes for every message", "", "bool"),
        Setting("knowledge.retrieval_max_chars", "Knowledge per prompt", unit="characters"),
        Setting("knowledge.curator", "Worker that files the inbox", "", "select"),  # options: the workers
    ]),
    ("web", "Web page", [
        Setting("web.listen", "Reachable from", "", "choice",
                [("localhost", "This computer"), ("tailscale", "Tailscale"), ("all", "My network")], restart=True),
        Setting("web.auth", "Who gets in", "", "choice",
                [("token", "Sign-in token"), ("tailscale", "My Tailscale devices"), ("none", "No sign-in")],
                restart=True),
        Setting("web.port", "Port", restart=True),
        Setting("web.behind_proxy", "Behind a reverse proxy", "So it sees the real https and addresses.", "bool",
                restart=True),
    ]),
    ("running", "Running & updates", [
        Setting("updates.check", "Look for new releases", "Once a day; it tells you.", "bool"),
        Setting("updates.auto", "Install them by itself", "When it's idle, running in the background.", "bool"),
    ]),
]
BY_KEY = {s.key: s for _, _, items in SECTIONS for s in items}


def lookup(cfg: Any, path: list[str]) -> Any:
    for key in path:
        if not isinstance(cfg, dict):
            return None
        cfg = cfg.get(key)
    return cfg


def defaults() -> dict[str, Any]:
    return yaml.safe_load((TEMPLATES / "config.yaml").read_text(encoding="utf-8")) or {}


def coerce(s: Setting, value: Any) -> Any:
    """The value as config.yaml should hold it, or ValueError in words the page can show."""
    if s.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{s.label}: on or off")
        return value
    if s.kind in ("choice", "select"):
        allowed = [v for v, _ in s.options]
        if value not in allowed and not (s.key == "web.listen" and isinstance(value, str) and value.strip()):
            raise ValueError(f"{s.label}: pick one of the options")
        return value.strip() if isinstance(value, str) else value
    if s.kind == "text":
        value = str(value or "").strip()
        return value or None
    if s.kind == "limit" and value in (None, "", "unlimited"):
        return "unlimited" if s.key == "limits.max_concurrent_workers" else None
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{s.label}: a whole number") from None
    if n < 0 or (s.key == "web.port" and not 0 < n < 65536):
        raise ValueError(f"{s.label}: that number is out of range")
    return n


HARNESS_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini": "Gemini"}


def harness_title(name: str) -> str:
    return HARNESS_NAMES.get(name) or name.replace("-", " ").replace("_", " ").title()


def sections(home: Home) -> list[tuple[str, str, list[Setting]]]:
    """SECTIONS, with the workers section named after the harnesses in use and a daily cap for each."""
    from ..harness import entries
    names = sorted(entries(home.config()), key=lambda n: (n != "claude-code", n))
    out = []
    for sid, title, items in SECTIONS:
        if sid == "workers":
            titles = [harness_title(n) for n in names]
            title = "Workers & " + titles[0] if len(titles) == 1 else \
                "Workers, " + ", ".join(titles[:-1]) + " & " + titles[-1] if titles else title
            caps = [Setting(f"limits.harness.{n}.max_runs_per_day", f"{harness_title(n)} runs per day",
                            ("They share your Claude plan's limits. " if n == "claude-code" else "") + "Empty: no limit.",
                            "limit") for n in names if "." not in n]
            items = items[:1] + caps + items[1:]
        workers = sorted(p.stem for p in (home.root / "workers").glob("*.md"))
        items = [replace(s, options=[(w, w) for w in workers]) if s.key == "knowledge.curator" else s for s in items]
        out.append((sid, title, items))
    return out


def addresses() -> list[str]:
    """This computer's own IPv4 addresses, for web.listen: its network ones and its Tailscale one."""
    import socket
    from . import access
    found = set()
    try:
        found |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    try:
        found.add(access.resolve_listen("tailscale"))
    except access.AccessError:
        pass
    return sorted(a for a in found if not a.startswith("127."))


def view(home: Home) -> list[dict[str, Any]]:
    cfg, base = home.config(), defaults()
    out = []
    for sid, title, items in sections(home):
        rows = []
        for s in items:
            # A key missing from config.yaml (a home made before it existed) means the default is in use.
            value, default = lookup(cfg, s.path), lookup(base, s.path)
            rows.append({"key": s.key, "label": s.label, "help": s.help, "kind": s.kind, "unit": s.unit,
                         "options": s.options, "restart": s.restart, "default": default,
                         "value": default if value is None else value})
        out.append({"id": sid, "title": title, "items": rows})
    return out


def set_setting(home: Home, key: str, value: Any) -> dict[str, Any]:
    s = next((s for _, _, items in sections(home) for s in items if s.key == key), None)
    if s is None:
        raise ValueError(f"{key} can't be changed here")
    value = coerce(s, value)
    if s.key == "knowledge.folder" and value is None:
        value = "knowledge"
    home.set_config(s.path, value)
    if s.key == "web.auth" and value == "token" and not os.environ.get("WEB_TOKEN"):
        from .access import new_token
        home.set_secret("WEB_TOKEN", new_token())
    sha = home.commit(f"user (web): {key} = {json.dumps(value, ensure_ascii=False)}", ["config.yaml"])
    return {"key": key, "value": value, "commit": sha}


# ---- what waits for a restart ------------------------------------------------------------------------
RESTART_SECRETS = ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")


def boot_state(home: Home) -> dict[str, Any]:
    """What the running agent started with, to tell which changes it hasn't picked up yet."""
    return {"config": copy.deepcopy(home.config()), "secrets": {k: os.environ.get(k, "") for k in RESTART_SECRETS},
            "notes": []}


def pending_restart(home: Home, boot: dict[str, Any]) -> list[str]:
    cfg, then = home.config(), boot["config"]
    out = [s.label for s in BY_KEY.values() if s.restart and lookup(cfg, s.path) != lookup(then, s.path)]
    if cfg.get("channels") != then.get("channels") or \
            any(os.environ.get(k, "") != v for k, v in boot["secrets"].items()):
        out.append("Telegram")
    return out + [n for n in boot["notes"] if n not in out]


# ---- models.yaml -------------------------------------------------------------------------------------
def save_models(home: Home, data: dict[str, Any], message: str) -> str | None:
    """models.yaml with its opening comments kept (the rest is rewritten from the data)."""
    head = []
    for line in home.read("models.yaml").splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None)
    home.write("models.yaml", "\n".join(head).rstrip() + "\n\n" + body if head else body)
    return home.commit(message, ["models.yaml"])


def models_view(home: Home) -> dict[str, Any]:
    from .data import models
    return {**models(home), "default": home.models().get("default_for_new_workers", "")}


def set_default_model(home: Home, model: str) -> str | None:
    if home.model_entry(model) is None:
        raise ValueError(f"{model} isn't in models.yaml")
    text = re.sub(r"^default_for_new_workers:.*$", f"default_for_new_workers: {model}", home.read("models.yaml"),
                  flags=re.M)
    home.write("models.yaml", text)
    return home.commit(f"user (web): new workers use {model}", ["models.yaml"])


def save_model(home: Home, entry: dict[str, Any], replace: str | None = None) -> str | None:
    model_id = str(entry.get("id") or "").strip()
    if not re.fullmatch(r"[^\s/]+/\S+", model_id):  # providers' ids vary (OpenRouter's aliases start with ~)
        raise ValueError("A model id looks like provider/model, e.g. anthropic/claude-sonnet-5-5")
    data = home.models()
    models = data.setdefault("models", [])
    old = next((m for m in models if m.get("id") == (replace or model_id)), None)
    if model_id != (replace or model_id) and any(m.get("id") == model_id for m in models):
        raise ValueError(f"{model_id} is already in the list")
    new: dict[str, Any] = dict(old or {})
    new["id"] = model_id
    for key in ("tier",):
        if entry.get(key):
            new[key] = str(entry[key])
    if "good_for" in entry:
        new["good_for"] = [g.strip() for g in str(entry["good_for"]).split(",") if g.strip()] \
            if isinstance(entry["good_for"], str) else list(entry["good_for"] or [])
    if "approval" in entry:
        if entry["approval"] == "ask":
            new["approval"] = "ask"
        else:
            new.pop("approval", None)
    price = entry.get("price")
    if isinstance(price, dict) and price.get("in") is not None and price.get("out") is not None:
        new["price"] = {"in": float(price["in"]), "out": float(price["out"])}
    if old is None:
        models.append(new)
    else:
        models[models.index(old)] = new
    return save_models(home, data, f"user (web): {'added' if old is None else 'changed'} model {model_id}")


def remove_model(home: Home, model_id: str) -> str | None:
    if home.worker("main").model == model_id:
        raise ValueError("The main agent uses that model. Pick another main model first.")
    data = home.models()
    before = len(data.get("models", []))
    data["models"] = [m for m in data.get("models", []) if m.get("id") != model_id]
    if len(data["models"]) == before:
        raise ValueError(f"{model_id} isn't in models.yaml")
    if data.get("default_for_new_workers") == model_id:
        data["default_for_new_workers"] = home.worker("main").model
    return save_models(home, data, f"user (web): removed model {model_id}")


def providers_view() -> list[dict[str, Any]]:
    from ..setup.providers import PROVIDERS
    out = []
    for p in PROVIDERS.values():
        key = os.environ.get(p.env, "") if p.env else ""
        out.append({"key": p.key, "title": p.title, "env": p.env, "set": bool(key), "last4": key[-4:] if key else "",
                    "where": p.where, "local": p.env is None})
    return out


def set_provider_key(home: Home, provider: str, raw: str) -> int:
    """Check the key with the provider, then save it. Returns how many models it offers."""
    from ..setup.providers import PROVIDERS, KeyProblem, clean_key, fetch_models
    p = PROVIDERS.get(provider)
    if p is None or not p.env:
        raise ValueError("That provider doesn't take a key")
    key = clean_key(raw, p.env)
    if not key:
        raise ValueError("Paste the key first")
    try:
        found = fetch_models(p, key)
    except KeyProblem as e:
        raise ValueError(str(e)) from None
    home.set_secret(p.env, key)
    return len(found)


def provider_models(provider: str, query: str = "", embeddings: bool = False) -> list[dict[str, Any]]:
    """The provider's chat models, or with `embeddings` the ones for meaning-based search (knowledge.embeddings)."""
    from ..setup.providers import (OLLAMA, PROVIDERS, KeyProblem, Model, fetch_embedding_models, fetch_models,
                                   ollama_models, search)
    p = PROVIDERS.get(provider)
    if p is None:
        raise ValueError(f"unknown provider {provider}")
    if embeddings:
        if p.key == "anthropic":  # no embedding models of its own
            return []
        if p.env and not os.environ.get(p.env):
            raise ValueError(f"Add your {p.title} key first, under Provider keys.")
        try:
            found = fetch_embedding_models(p, os.environ.get(p.env, "") if p.env else "")
        except KeyProblem as e:
            raise ValueError(str(e)) from None
        except ConnectionError as e:
            raise ValueError(str(e)) from None
    elif p.env is None:
        names = ollama_models(os.environ.get("OLLAMA_API_BASE", OLLAMA))
        if names is None:
            raise ValueError("Ollama isn't running on this computer")
        found = [Model(n, price=(0, 0)) for n in names]
    else:
        try:
            found = fetch_models(p, os.environ.get(p.env, ""))
        except KeyProblem as e:
            raise ValueError(str(e)) from None
        except Exception:  # noqa: BLE001 - offline, or the provider is down: its usual suspects will do
            found = [Model(m) for m in p.models]
    if query:
        found = search(found, query)
    return [{"id": m.id, "about": m.describe(), "price": {"in": m.price[0], "out": m.price[1]} if m.price else None}
            for m in found[:80]]


# ---- the agent's files -------------------------------------------------------------------------------
def save_file(home: Home, rel: str, text: str) -> str | None:
    if rel not in EDITABLE:
        raise ValueError(f"{rel} can't be edited here")
    if rel.endswith((".yaml", ".yml")):
        try:
            if not isinstance(yaml.safe_load(text) or {}, dict):
                raise ValueError(f"{rel} must be a YAML mapping")
        except yaml.YAMLError as e:
            raise ValueError(f"That isn't valid YAML: {e}") from None
    elif rel.endswith(".json"):
        try:
            if not isinstance(json.loads(text), dict):
                raise ValueError(f"{rel} must be a JSON object")
        except json.JSONDecodeError as e:
            raise ValueError(f"That isn't valid JSON: {e}") from None
    home.write(rel, text if text.endswith("\n") else text + "\n")
    return home.commit(f"user (web): edited {rel}", [rel])


# ---- the checklist after the fast-track setup --------------------------------------------------------
def setup_state(home: Home) -> dict[str, Any]:
    path = home.cache / "setup.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_setup_state(home: Home, **changes: Any) -> dict[str, Any]:
    state = {**setup_state(home), **changes}
    home.cache.mkdir(parents=True, exist_ok=True)
    (home.cache / "setup.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def checklist(home: Home) -> dict[str, Any]:
    from ..setup.wizard import claude_path
    state = setup_state(home)
    skipped = set(state.get("skipped", []))
    servers = home.mcp().get("mcpServers", {})
    main = home.worker("main").model
    steps = [
        {"id": "model", "title": "Model", "done": bool(main) and not home.missing_keys(main), "sub": main},
        {"id": "web", "title": "Web page", "done": True, "sub": "you're on it"},
        {"id": "telegram", "title": "Reach you on your phone",
         "done": bool(os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
         "sub": "Telegram lets it message you, send notices and ask before acting while this page is closed."},
        {"id": "calendar", "title": "Calendar", "done": "calendar" in servers,
         "sub": "Google, Outlook, iCloud: what's on, free time, adding events"},
        {"id": "messages", "title": "Text messages", "done": "messages" in servers,
         "sub": "Your SMS and RCS texts through Google Messages"},
        {"id": "canvas", "title": "Canvas", "done": "canvas" in servers,
         "sub": "Classes, homework, grades and course files"},
        {"id": "claude", "title": "Claude Code", "done": bool(claude_path()),
         "sub": "Hand coding and long jobs to it, on your Claude subscription."},
        {"id": "devices", "title": "Your other devices", "done": bool(state.get("linked")),
         "sub": "A sign-in link or QR code for your phone or laptop"},
    ]
    for s in steps:
        s["skipped"] = s["id"] in skipped and not s["done"]
    left = [s for s in steps if not s["done"] and not s["skipped"]]
    return {"steps": steps, "done": sum(1 for s in steps if s["done"]), "total": len(steps),
            "finished": not left or bool(state.get("dismissed"))}


def link(home: Home, ch) -> dict[str, Any]:  # noqa: ANN001 - the WebChannel
    """The page's address, a link that signs a browser in, and a QR code of it."""
    from .access import TokenAuth
    url = ch.urls[0] if ch.urls else ""
    signed = f"{url}/?token={ch.auth.token()}" if isinstance(ch.auth, TokenAuth) else url
    save_setup_state(home, linked=True)
    return {"url": url, "link": signed, "qr": qr_svg(signed)}


def qr_svg(text: str) -> str:
    try:
        import segno
    except ImportError:
        return ""
    import io
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=5, border=2, dark="#111", light="#fff", xmldecl=False,
                                    svgns=True)
    return out.getvalue().decode()


# ---- the background service and updates --------------------------------------------------------------
def service_view(home: Home) -> dict[str, Any]:
    from ..setup import service, update
    check, auto = update.settings(home)
    try:
        latest = update.available(home) if check else None
    except Exception:  # noqa: BLE001 - offline
        latest = None
    return {"service": service.running(home), "lines": service.status(home), "version": update.current(),
            "latest": latest, "pid": os.getpid()}

