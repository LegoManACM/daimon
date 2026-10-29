"""Built-in tools the agent uses to read and change its own files.

Each change is committed with the agent's one-line reason. Consent comes from config.yaml `self_edit`:
  policy: protected files (POLICY.md, config.yaml, mcp.json, models.yaml) ask, everything else is auto
  free:   everything auto        all: everything asks
Independently, assigning a model marked `approval: ask` in models.yaml always asks.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

import yaml

from . import harness as harnesses
from .gate import Surface, Tool
from .home import PROTECTED, Home, split_frontmatter
from .mcp_hub import McpHub

STR = {"type": "string"}


def obj(required: list[str], **props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def build(home: Home, hub: McpHub, surface: Surface) -> list[Tool]:
    tools: list[Tool] = []

    def tool(name: str, description: str, schema: dict[str, Any], level: Callable[[dict], str] | str = "auto"):
        def register(fn):
            async def run(args: dict[str, Any]) -> str:
                return fn(**args)
            lvl = level if callable(level) else (lambda _a, _l=level: _l)
            tools.append(Tool(name, description, schema, run, lvl))
            return fn
        return register

    # ---- consent helpers -----------------------------------------------------------------------------
    def files_level(paths: list[str]) -> str:
        mode = home.config().get("self_edit", "policy")
        if mode == "free":
            return "auto"
        if mode == "all":
            return "ask"
        return "ask" if any(p in PROTECTED for p in paths) else "auto"

    def check_content(rel: str, new: str | None) -> str:
        """Validate a file's new content; returns the extra level it requires ('auto' or 'ask')."""
        if new is None:
            return "auto"
        if rel == "MEMORY.md":
            cap = int(home.config().get("memory_max_chars", 6000))
            if len(new) > cap:
                raise ValueError(f"MEMORY.md would be {len(new)} chars; the cap is {cap}. Condense it: keep it "
                                 "general and move details elsewhere.")
        if rel in ("config.yaml", "models.yaml"):
            yaml.safe_load(new)  # refuse to write broken YAML the runtime can't load
        if rel == "mcp.json":
            json.loads(new)
        if rel.startswith("workers/") and rel.endswith(".md"):
            meta = split_frontmatter(new)[0]
            model, harness = meta.get("model"), meta.get("harness")
            if harness not in (None, "", "native"):  # the harness is the provider: its own model names
                entry = harnesses.model_entry(str(harness), model, home.config())
                old = split_frontmatter(home.read(rel))[0].get("model")
                return "ask" if entry.get("approval") == "ask" and model != old else "auto"
            if model:
                entry = home.model_entry(model)
                if not entry:
                    ids = [m["id"] for m in home.models().get("models", [])]
                    raise ValueError(f"model {model} isn't in models.yaml. Allowed: {', '.join(ids)}. Adding a "
                                     "model to models.yaml needs the user's OK.")
                missing = home.missing_keys(model)
                if missing:
                    raise ValueError(f"model {model} can't be used yet: {', '.join(missing)} isn't set in the user's "
                                     "secrets file. Pick a usable model (see Runtime) or ask the user to add the key.")
                old = split_frontmatter(home.read(rel))[0].get("model")
                if entry.get("approval") == "ask" and model != old:
                    return "ask"
        return "auto"

    def edit_level(rel: str, new: str | None) -> str:
        extra = check_content(rel, new)
        return "ask" if "ask" in (extra, files_level([rel])) else files_level([rel])

    def saved(rel_paths: list[str], reason: str) -> str:
        ref = home.commit(f"agent: {reason}", rel_paths)
        surface.event("edit", f"{', '.join(rel_paths)}: {reason}" + (f" ({ref})" if ref else ""))
        return f"Saved{f' as change {ref}' if ref else ' (no change)'}. It takes effect from your next step."

    def target(path: str) -> tuple[str, Any]:
        p = home.agent_path(path)
        return home.rel(p), p

    def edited(path: str, old_text: str, new_text: str) -> str:
        rel, p = target(path)
        if not p.exists():
            raise ValueError(f"{rel} doesn't exist")
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text appears {count} times in {rel}; it must match exactly once")
        return text.replace(old_text, new_text)

    # ---- reading -------------------------------------------------------------------------------------
    @tool("list_agent_files", "List the files that make up you (your home folder).", obj([]))
    def list_agent_files() -> str:
        return "\n".join(home.files())

    @tool("read_agent_file", "Read one of your own files.", obj(["path"], path=STR))
    def read_agent_file(path: str) -> str:
        rel, p = target(path)
        if not p.exists():
            return f"{rel} doesn't exist."
        return p.read_text(encoding="utf-8")

    # ---- changing yourself ---------------------------------------------------------------------------
    @tool("edit_agent_file",
          "Change part of one of your files by replacing old_text (must match exactly once) with new_text. "
          "Use this to change your behaviour, rules, memory or workers.",
          obj(["path", "old_text", "new_text", "reason"], path=STR, old_text=STR, new_text=STR,
              reason={"type": "string", "description": "One line: why. Becomes the change's history entry."}),
          level=lambda a: edit_level(target(a["path"])[0], edited(a["path"], a["old_text"], a["new_text"])))
    def edit_agent_file(path: str, old_text: str, new_text: str, reason: str) -> str:
        rel, p = target(path)
        p.write_text(edited(path, old_text, new_text), encoding="utf-8")
        return saved([rel], reason)

    @tool("write_agent_file", "Create one of your files, or replace its whole content.",
          obj(["path", "content", "reason"], path=STR, content=STR, reason=STR),
          level=lambda a: edit_level(target(a["path"])[0], a["content"]))
    def write_agent_file(path: str, content: str, reason: str) -> str:
        rel, p = target(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return saved([rel], reason)

    @tool("delete_agent_file", "Delete one of your files (it stays in history and can be restored).",
          obj(["path", "reason"], path=STR, reason=STR),
          level=lambda a: files_level([target(a["path"])[0]]))
    def delete_agent_file(path: str, reason: str) -> str:
        rel, p = target(path)
        if rel in ("SELF.md", "config.yaml", "workers/main.md"):
            raise ValueError(f"{rel} is required; edit it instead of deleting it")
        p.unlink()
        return saved([rel], reason)

    @tool("create_worker",
          "Create a worker: a model + prompt + tools you can later hand tasks to. A native worker's model must be "
          "listed in models.yaml. A harness worker's model is the harness's own (e.g. opus for claude-code, run on "
          "the user's subscription): never add a model to models.yaml for a harness worker.",
          obj(["name", "prompt", "reason"], name=STR, prompt=STR, reason=STR,
              model={"type": "string", "description": "Native: a LiteLLM id from models.yaml. Harness: one of the "
                     "harness's models (see Harnesses in Runtime). Default if omitted."},
              mcps={"type": "array", "items": STR, "description": "MCP server names this worker may use"},
              skills={"type": "array", "items": STR},
              harness={"type": "string", "description": "Run its tasks in an external agent instead of your own "
                       "loop, e.g. claude-code (harnesses are listed in config.yaml). Best for coding and long "
                       "projects."},
              repo={"type": "string", "description": "Harness workers: a git repository to work in (a worktree "
                    "per task, or per project)"}),
          level=lambda a: edit_level(f"workers/{a['name']}.md", worker_text(**a)))
    def create_worker(name: str, prompt: str, reason: str, model: str | None = None,
                      mcps: list[str] | None = None, skills: list[str] | None = None, harness: str | None = None,
                      repo: str | None = None) -> str:
        rel = f"workers/{name}.md"
        if (home.root / rel).exists():
            raise ValueError(f"{rel} already exists; edit it instead")
        home.write(rel, worker_text(name, prompt, reason, model, mcps, skills, harness, repo))
        return saved([rel], reason)

    def worker_text(name: str, prompt: str, reason: str = "", model: str | None = None,
                    mcps: list[str] | None = None, skills: list[str] | None = None, harness: str | None = None,
                    repo: str | None = None) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("worker names are lowercase letters, digits and dashes")
        if harness and harness != "native" and harness not in harnesses.entries(home.config()):
            raise ValueError(f"no harness {harness}; known: {', '.join(harnesses.entries(home.config()))}")
        native = harness in (None, "", "native")
        if not native and not model:
            model = harnesses.entries(home.config())[harness].get("default_model")
        meta = {"type": "worker", "model": model or (home.models().get("default_for_new_workers") if native else None),
                "mcps": mcps or [], "skills": skills or []}
        meta = {k: v for k, v in meta.items() if v is not None}
        meta.update({k: v for k, v in {"harness": harness, "repo": repo}.items() if v})
        return f"---\n{yaml.safe_dump(meta, sort_keys=False).strip()}\n---\n{prompt.strip()}\n"

    # ---- tools and consent ---------------------------------------------------------------------------
    @tool("add_mcp_server",
          "Connect an MCP server (gives you new tools). Use command+args for a local server or url for a "
          "remote one. Refer to secrets as ${NAME} (stored in the user's secrets file, which you can't read).",
          obj(["name", "reason"], name=STR, reason=STR, command=STR,
              args={"type": "array", "items": STR}, url=STR,
              env={"type": "object", "additionalProperties": STR},
              headers={"type": "object", "additionalProperties": STR}),
          level=lambda _a: files_level(["mcp.json"]))
    def add_mcp_server(name: str, reason: str, command: str | None = None, args: list[str] | None = None,
                       url: str | None = None, env: dict | None = None, headers: dict | None = None) -> str:
        if not (command or url):
            raise ValueError("give either command (local server) or url (remote server)")
        data = home.mcp()
        entry = {"url": url, "headers": headers} if url else {"command": command, "args": args or [], "env": env}
        data.setdefault("mcpServers", {})[name] = {k: v for k, v in entry.items() if v}
        home.write_mcp(data)
        return saved(["mcp.json"], reason) + " Its tools will appear on your next step; check mcp_status."

    @tool("remove_mcp_server", "Disconnect an MCP server.", obj(["name", "reason"], name=STR, reason=STR),
          level=lambda _a: files_level(["mcp.json"]))
    def remove_mcp_server(name: str, reason: str) -> str:
        data = home.mcp()
        if data.get("mcpServers", {}).pop(name, None) is None:
            raise ValueError(f"no MCP server named {name}")
        home.write_mcp(data)
        return saved(["mcp.json"], reason)

    @tool("set_tool_consent",
          "Set the consent level for an MCP server's tool(s): auto, notify or ask. tool may be a glob like "
          "'send_*' or '*'.",
          obj(["server", "tool", "level", "reason"], server=STR, tool=STR,
              level={"type": "string", "enum": ["auto", "notify", "ask"]}, reason=STR),
          level=lambda _a: files_level(["mcp.json"]))
    def set_tool_consent(server: str, tool: str, level: str, reason: str) -> str:
        hub.set_level(server, tool, level)
        return saved(["mcp.json"], reason)

    @tool("mcp_status", "Show connected MCP servers, their tools and consent levels, and any errors.", obj([]))
    def mcp_status() -> str:
        lines = []
        for s in hub.servers.values():
            if s.error:
                lines.append(f"{s.name}: FAILED to connect: {s.error}")
                continue
            lines.append(f"{s.name}: connected")
            lines += [f"  {t.name} [{hub.level(s.name, t)}]" for t in s.tools]
        return "\n".join(lines) or "No MCP servers configured."

    # ---- history -------------------------------------------------------------------------------------
    @tool("change_history", "List recent changes to your files (yours and the user's), newest first.",
          obj([], limit={"type": "integer"}))
    def change_history(limit: int = 15) -> str:
        return home.history(limit) or "No history yet."

    @tool("undo_change", "Undo one change from change_history by its id.",
          obj(["change", "reason"], change=STR, reason=STR),
          level=lambda a: files_level(home.changed_files(a["change"])))
    def undo_change(change: str, reason: str) -> str:
        files = home.changed_files(change)
        ref = home.revert(change, reason)
        surface.event("edit", f"undid {change} ({', '.join(files)}): {reason}")
        return f"Undone as change {ref}."

    return tools
