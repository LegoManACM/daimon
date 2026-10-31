"""What the web page shows, read from the running agent and its home folder. Plain dicts, ready for JSON.

Nothing here is kept in sync by hand: every call reads the same files and state the agent itself uses.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any

from ..home import PROTECTED, Home, split_frontmatter
from ..tasks import OPEN

CONTENT_MAX = 6000  # per message sent to the page; the full text stays in the thread


# ---- conversation ------------------------------------------------------------------------------------
def message(i: int, m: dict[str, Any]) -> dict[str, Any]:
    content = m.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    out: dict[str, Any] = {"i": i, "role": m.get("role"), "content": content[:CONTENT_MAX]}
    if len(content) > CONTENT_MAX:
        out["truncated"] = len(content) - CONTENT_MAX
    if m.get("ts"):
        out["ts"] = m["ts"]
    if m.get("tool_calls"):
        out["calls"] = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name", ""),
                         "args": (c.get("function") or {}).get("arguments", "")[:2000]} for c in m["tool_calls"]]
    if m.get("tool_call_id"):
        out["call"] = m["tool_call_id"]
    return out


def thread(t, limit: int = 300) -> dict[str, Any]:  # noqa: ANN001 - a Thread
    start = max(0, len(t.messages) - limit)
    return {"messages": [message(i, m) for i, m in enumerate(t.messages) if i >= start],
            "summary": bool(t.summary), "older": start}


# ---- spend -------------------------------------------------------------------------------------------
def spend_rows(home: Home, since: float) -> list[dict[str, Any]]:
    path = home.cache / "spend.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ts", 0) >= since:
            out.append(row)
    return out


def spend(home: Home) -> dict[str, Any]:
    now = datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week, month = day - timedelta(days=day.weekday()), day.replace(day=1)
    rows = spend_rows(home, month.timestamp())
    today = [r for r in rows if r["ts"] >= day.timestamp()]

    def group(key: str) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in today:
            g = out.setdefault(r.get(key) or "?", {"name": r.get(key) or "?", "usd": 0.0, "calls": 0, "in": 0, "out": 0})
            g["usd"] += r.get("usd") or 0.0
            g["calls"] += 1
            g["in"] += r.get("in") or 0
            g["out"] += r.get("out") or 0
        return sorted(out.values(), key=lambda g: (-g["usd"], -g["calls"]))

    total = lambda rs: round(sum(r.get("usd") or 0.0 for r in rs), 6)  # noqa: E731
    return {"today": total(today), "week": total([r for r in rows if r["ts"] >= week.timestamp()]),
            "month": total(rows), "month_name": now.strftime("%B"), "calls": len(today),
            "by_model": group("model"), "by_worker": group("worker")}


# ---- tasks -------------------------------------------------------------------------------------------
def task_row(t) -> dict[str, Any]:  # noqa: ANN001 - a Task
    log = (t.sections.get("Log") or "").strip().splitlines()
    row = {"id": t.id, "title": t.title, "status": t.status, "worker": t.worker,
           **{k: str(t.meta[k]) for k in ("run_at", "due", "project", "created", "created_by", "blocked") if t.meta.get(k)}}
    if log:
        row["last"] = log[-1].lstrip("- ")
    if t.status == "waiting":
        asked = next((line for line in reversed(log) if "asked main:" in line), "")
        row["question"] = asked.split("asked main:", 1)[-1].strip()
    return row


def tasks(store, include_closed: int = 15) -> list[dict[str, Any]]:  # noqa: ANN001 - a TaskStore
    everything = store.all()
    open_ = [task_row(t) for t in everything if t.status in OPEN]
    closed = [t for t in everything if t.status not in OPEN]
    closed.sort(key=lambda t: str(t.meta.get("created", "")), reverse=True)
    return open_ + [task_row(t) for t in closed[:include_closed]]


def task(store, task_id: str) -> dict[str, Any]:  # noqa: ANN001
    t = store.get(task_id)
    return {**task_row(t), "sections": {k: v.strip() for k, v in t.sections.items()},
            "meta": {k: str(v) for k, v in t.meta.items()}}


# ---- schedule ----------------------------------------------------------------------------------------
def schedule(home: Home, store) -> list[dict[str, Any]]:  # noqa: ANN001
    state_file = home.cache / "routines.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    out = []
    for path in sorted((home.root / "routines").glob("*.md")):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        entry = state.get(path.stem) or {}
        first = next((line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")), "")
        out.append({"kind": "routine", "name": path.stem, "schedule": str(meta.get("schedule", "")),
                    "worker": meta.get("worker", "main"), "enabled": meta.get("enabled", True) is not False,
                    "next": entry.get("next"), "last": entry.get("last"), "about": first[:200]})
    for t in store.all(("queued",)):
        if t.meta.get("run_at"):
            out.append({"kind": "wake" if t.worker == "main" else "task", "name": t.title, "id": t.id,
                        "worker": t.worker, "next": str(t.meta["run_at"]), "project": t.meta.get("project")})
    return sorted(out, key=lambda e: (not e.get("enabled", True), e.get("next") or "9999"))


# ---- changes (the home's git history) ----------------------------------------------------------------
def changes(home: Home, limit: int = 60) -> list[dict[str, Any]]:
    r = home._git("log", f"-n{limit}", "--format=%x1e%h%x1f%aI%x1f%s", "--name-status")
    out = []
    for block in r.stdout.split("\x1e"):
        if not block.strip():
            continue
        head, *files = block.strip().splitlines()
        sha, when, subject = head.split("\x1f", 2)
        out.append({"sha": sha, "when": when, "subject": subject,
                    "files": [{"op": f.split("\t")[0][:1], "path": f.split("\t")[-1]} for f in files if "\t" in f]})
    return out


def diff(home: Home, sha: str, limit: int = 80_000) -> str:
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        raise ValueError("bad change id")
    r = home._git("show", "--format=%h %s%n%aI%n", "--patch", sha)
    if r.returncode != 0:
        raise ValueError(f"unknown change {sha}")
    return r.stdout if len(r.stdout) <= limit else r.stdout[:limit] + "\n…(cut)"


# ---- the agent's files -------------------------------------------------------------------------------
def files(home: Home) -> dict[str, Any]:
    mode = home.config().get("self_edit", "policy")
    level = lambda rel: "auto" if mode == "free" else "ask" if mode == "all" or rel in PROTECTED else "auto"  # noqa: E731
    out = []
    for rel in home.files():
        p = home.root / rel
        out.append({"path": rel, "size": p.stat().st_size, "level": level(rel), "modified": p.stat().st_mtime})
    return {"mode": mode, "files": out}


def read_file(home: Home, rel: str) -> str:
    p = home.agent_path(rel)  # refuses paths outside the home
    parts = p.relative_to(home.root).parts
    if parts[0] in (".git", ".cache") or not p.is_file():
        raise ValueError(f"no file {rel}")
    return p.read_text(encoding="utf-8", errors="replace")


# ---- models ------------------------------------------------------------------------------------------
def models(home: Home) -> dict[str, Any]:
    entries = home.models().get("models", [])
    return {"main": home.worker("main").model,
            "models": [{"id": m.get("id"), "tier": m.get("tier", ""), "good_for": m.get("good_for") or [],
                        "price": m.get("price"), "approval": m.get("approval")} for m in entries if m.get("id")]}


def set_main_model(home: Home, model: str) -> str | None:
    """Point workers/main.md at another model from models.yaml. A commit, like any other change to the agent."""
    if home.model_entry(model) is None:
        raise ValueError(f"{model} isn't in models.yaml")
    text = home.read("workers/main.md")
    m = re.match(r"^(---\r?\n)(.*?)(\r?\n---)", text, re.S)
    if not m:
        raise ValueError("workers/main.md has no frontmatter")
    head = m.group(2)
    head = re.sub(r"^model:.*$", f"model: {model}", head, count=1, flags=re.M) if re.search(r"^model:", head, re.M) \
        else head + f"\nmodel: {model}"
    home.write("workers/main.md", m.group(1) + head + m.group(3) + text[m.end():])
    return home.commit(f"user (web): main model is now {model}", ["workers/main.md"])


# ---- status ------------------------------------------------------------------------------------------
def status(agent, started: float) -> dict[str, Any]:  # noqa: ANN001 - an Agent
    from ..cli import version
    home = agent.home
    day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    commits = home._git("rev-list", "--count", f"--since={day.isoformat()}", "HEAD").stdout.strip()
    state_file = home.cache / "routines.json"
    routines = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    upcoming = sorted([v.get("next") for v in routines.values() if v.get("next")] +
                      [str(t.meta["run_at"]) for t in agent.tasks.all(("queued",)) if t.meta.get("run_at")])
    running = []
    for task_id in agent.workers:
        try:
            t = agent.tasks.get(task_id)
            running.append({"id": t.id, "title": t.title, "worker": t.worker})
        except ValueError:
            pass
    done_today = sum(1 for t in agent.tasks.all(("done",))
                     if str(t.meta.get("created", "")) >= day.isoformat()[:10])
    return {
        "version": version(), "pid": os.getpid(), "started": started, "uptime": time.time() - started,
        "home": str(home.root), "paused": getattr(agent, "paused", False),
        "today": {"model_calls": len(spend_rows(home, day.timestamp())), "tasks_done": done_today,
                  "commits": int(commits or 0), "notes": agent.runner.kb.count()},
        "next": upcoming[0] if upcoming else None, "workers": running,
        "jobs": [{"id": j.id, "who": j.who, "tool": j.tool, "started": j.started}
                 for j in agent.runner.jobs.jobs.values() if j.finished is None],
        "servers": [{"name": s.name, "tools": len(s.tools), "error": s.error} for s in agent.hub.servers.values()],
        "servers_configured": len(home.mcp().get("mcpServers", {})),
        "channels": [c.name for c in agent.surface.channels if c.name != "log"]
        if hasattr(agent.surface, "channels") else [],
    }
