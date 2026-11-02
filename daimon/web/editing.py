"""Editing tasks and the schedule from the web page: what the Edit and Add forms send.

Everything is checked here and a ValueError carries the message the page shows. Tasks go through TaskStore the
way the agent's own tools write them; routines are routines/<name>.md files, committed like any other change.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from ..home import Home, join_frontmatter, set_yaml_path, split_frontmatter
from ..schedule import next_run, now, parse_time
from ..tasks import Task
from ..tasktools import create_wake

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
FULL_DAYS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
             "sat": "Saturday", "sun": "Sunday"}


def text(b: dict[str, Any], key: str) -> str:
    return str(b.get(key) or "").strip()


def when_value(value: Any, what: str = "time") -> str | None:
    """A date/time from a form ('' clears it) as the ISO text tasks store."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return parse_time(value).isoformat(timespec="minutes")
    except ValueError:
        raise ValueError(f"That {what} isn't valid; pick it from the calendar.") from None


def not_past(run_at: str) -> str:
    if parse_time(run_at) < now() - timedelta(minutes=2):
        raise ValueError("That time has already passed.")
    return run_at


def check_worker(home: Home, worker: str) -> None:
    if not (home.root / "workers" / f"{worker}.md").is_file() or not re.fullmatch(r"[\w-]+", worker):
        raise ValueError(f"There's no worker called {worker!r}.")


# ---- tasks -------------------------------------------------------------------------------------------
def edit_task(agent, task_id: str, b: dict[str, Any]) -> Task:  # noqa: ANN001 - an Agent
    """Apply the fields of an Edit form. Only fields that are present are looked at."""
    t = agent.tasks.get(task_id)
    running = t.status == "running"
    fields: dict[str, Any] = {}
    changed: list[str] = []

    if "title" in b:
        title = " ".join(text(b, "title").split())
        if not title:
            raise ValueError("A task needs a title.")
        if title != t.title:
            fields["title"], _ = title, changed.append("title")
    brief = None
    if "brief" in b and text(b, "brief") != (t.sections.get("Brief") or "").strip():
        if running:
            raise ValueError("A running task can't change its brief. Cancel it first, then edit it.")
        brief = text(b, "brief")
        changed.append("brief")
    if "worker" in b and text(b, "worker") != t.worker:
        if running:
            raise ValueError("A running task can't change its worker. Cancel it first, then edit it.")
        check_worker(agent.home, text(b, "worker"))
        fields["worker"] = text(b, "worker")
        changed.append("worker")
    if "run_at" in b:
        new = when_value(b["run_at"], "time")
        old = when_value(t.meta.get("run_at")) if t.meta.get("run_at") else None
        if new != old:
            if new and t.status in ("queued", "proposed"):
                not_past(new)
            fields["run_at"] = new
            changed.append("time")
    if "due" in b:
        due = text(b, "due")
        if due:
            try:
                due = datetime.fromisoformat(due).date().isoformat() if len(due) <= 10 else when_value(due, "due date")
            except ValueError:
                raise ValueError("That due date isn't valid; pick it from the calendar.") from None
        if (due or None) != (str(t.meta["due"]) if t.meta.get("due") else None):
            fields["due"] = due or None
            changed.append("due date")
    if "project" in b:
        project = text(b, "project")
        if project and not agent.projects.exists(project):
            raise ValueError(f"There's no project called {project!r}.")
        if (project or None) != t.meta.get("project"):
            fields["project"] = project or None
            changed.append("project")
    if not changed:
        return t
    t = agent.tasks.update(task_id, log=f"edited by the user on the web page ({', '.join(changed)})", brief=brief, **fields)
    agent._tick.set()
    return t


def add_item(agent, b: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    """The Schedule view's "+ Add": a reminder, a scheduled task or a routine."""
    kind = text(b, "kind")
    if kind == "reminder":
        note = text(b, "text")
        if not note:
            raise ValueError("Write what the reminder should say.")
        run_at = when_value(b.get("when"))
        if not run_at:
            raise ValueError("Pick when to remind you.")
        task = create_wake(agent.tasks, note, not_past(run_at), created_by="user (web)")
        agent._tick.set()
        return {"kind": "task", "id": task.id}
    if kind == "task":
        title, worker = text(b, "title"), text(b, "worker") or "main"
        if not title:
            raise ValueError("A task needs a title.")
        run_at = when_value(b.get("when"))
        if not run_at:
            raise ValueError("Pick when it should run.")
        check_worker(agent.home, worker)
        task = agent.tasks.create(title, text(b, "brief") or title, worker=worker, created_by="user (web)",
                                  run_at=not_past(run_at))
        agent._tick.set()
        return {"kind": "task", "id": task.id}
    if kind == "routine":
        name = text(b, "name").lower()
        save_routine(agent.home, name, b, new=True)
        agent._tick.set()
        return {"kind": "routine", "name": name}
    raise ValueError("Pick what to add: a reminder, a task or a routine.")


# ---- routines ----------------------------------------------------------------------------------------
def routine_path(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError("A routine's name is lowercase letters, numbers and dashes, like weekly-review.")
    return f"routines/{name}.md"


def routine(home: Home, name: str) -> dict[str, Any]:
    rel = routine_path(name)
    if not (home.root / rel).is_file():
        raise ValueError(f"There's no routine called {name}.")
    meta, body = split_frontmatter(home.read(rel))
    return {"name": name, "schedule": str(meta.get("schedule", "")), "worker": str(meta.get("worker", "main")),
            "enabled": meta.get("enabled", True) is not False, "project": meta.get("project"), "body": body.strip(),
            **describe(str(meta.get("schedule", "")))}


def save_routine(home: Home, name: str, b: dict[str, Any], new: bool = False) -> str | None:
    """Create or change routines/<name>.md and commit it. Frontmatter keys and comments we don't edit stay."""
    rel = routine_path(name)
    exists = (home.root / rel).is_file()
    if new and exists:
        raise ValueError(f"There's already a routine called {name}.")
    if not new and not exists:
        raise ValueError(f"There's no routine called {name}.")
    old = home.read(rel)
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", old, re.S)
    head, old_body = (m.group(1), m.group(2)) if m else ("", old)
    meta = split_frontmatter(old)[0]
    schedule = text(b, "schedule") if "schedule" in b else str(meta.get("schedule", ""))
    worker = text(b, "worker") if "worker" in b else str(meta.get("worker", "main"))
    enabled = bool(b["enabled"]) if "enabled" in b else meta.get("enabled", True) is not False
    body = text(b, "body") if "body" in b else old_body.strip()
    if not schedule:
        raise ValueError("Give it a schedule, like daily 08:00 or weekly sun 18:00.")
    next_run(schedule, now())  # the same parser the scheduler uses: raises with a readable message
    if worker != "none":
        check_worker(home, worker)
    if not body and worker != "none":
        raise ValueError("Write the instructions for it.")
    if new:
        text_out = join_frontmatter({"schedule": schedule, "worker": worker, "enabled": enabled}, body + "\n")
    else:
        for key, value in (("schedule", schedule), ("worker", worker), ("enabled", enabled)):
            if meta.get(key) != value:
                head = set_yaml_path(head, [key], value).rstrip("\n")
        text_out = f"---\n{head}\n---\n{body}\n"
    if text_out == old:
        return None
    home.write(rel, text_out)
    what = "added" if new else "changed"
    return home.commit(f"user (web): routine {name} {what}", [rel])


def delete_routine(home: Home, name: str) -> str | None:
    rel = routine_path(name)
    if name == "nightly":
        raise ValueError("nightly is the built-in upkeep routine. Turn it off instead of deleting it.")
    if not (home.root / rel).is_file():
        raise ValueError(f"There's no routine called {name}.")
    (home.root / rel).unlink()
    return home.commit(f"user (web): routine {name} deleted", [rel])


def run_routine(agent, name: str) -> None:  # noqa: ANN001
    """Fire a routine now, as if its time had come. The schedule is unchanged."""
    rel = routine_path(name)
    if not (agent.home.root / rel).is_file():
        raise ValueError(f"There's no routine called {name}.")
    meta, body = split_frontmatter(agent.home.read(rel))
    agent._fire(name, meta, body)
    agent._tick.set()


# ---- schedules in words ------------------------------------------------------------------------------
def clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def describe(schedule: str, count: int = 3) -> dict[str, Any]:
    """Whether a schedule parses, what it means in words, and its next few times (ISO). Never raises."""
    try:
        at, runs = now(), []
        for _ in range(count):
            at = next_run(schedule, at)
            runs.append(at.isoformat(timespec="minutes"))
    except (ValueError, IndexError) as e:
        return {"ok": False, "error": str(e) or "That isn't a schedule I know.", "runs": [], "words": ""}
    p = schedule.lower().split()
    if p[0] == "every":
        n = "".join(p[1:])
        unit = {"m": "minute", "h": "hour", "d": "day"}[n[-1]]
        words = f"Every {int(n[:-1])} {unit}{'' if int(n[:-1]) == 1 else 's'}, counting from the last run"
    elif p[0] == "daily":
        words = f"Every day at {clock(p[1])}"
    elif p[0] == "weekdays":
        words = f"Monday to Friday at {clock(p[1])}"
    elif p[0] == "weekly":
        days = [FULL_DAYS[d[:3]] for d in p[1].split(",")]
        words = f"Every {', '.join(days[:-1]) + ' and ' + days[-1] if len(days) > 1 else days[0]} at {clock(p[2])}"
    else:
        words = f"On day {int(p[1])} of every month at {clock(p[2])}"
    return {"ok": True, "words": words, "runs": runs}

