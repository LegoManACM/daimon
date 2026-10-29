"""Tasks: one markdown file each in tasks/. They're state, not behaviour, so they're git-ignored and changed
through task tools rather than file edits.

Status flow: proposed -> queued -> running -> waiting -> done | failed | cancelled
  proposed: the agent's own idea, waiting for the user's go-ahead
  queued:   will run when run_at passes (or right away without one)
  waiting:  the worker asked main a question; answering re-queues it
A task for worker `main` is a wake: when due, it arrives in main's inbox.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


from .home import join_frontmatter, split_frontmatter
from .schedule import now, parse_time

STATUSES = ("proposed", "queued", "running", "waiting", "done", "failed", "cancelled")
OPEN = ("proposed", "queued", "running", "waiting")


@dataclass
class Task:
    id: str
    meta: dict[str, Any]
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.meta.get("status", "queued")

    @property
    def worker(self) -> str:
        return self.meta.get("worker", "main")

    @property
    def title(self) -> str:
        return self.meta.get("title", self.id)

    def due_now(self, at: datetime) -> bool:
        run_at = self.meta.get("run_at")
        return self.status == "queued" and (not run_at or parse_time(run_at) <= at)

    def line(self) -> str:
        bits = [f"{self.id} [{self.status}] {self.title} (worker: {self.worker}"]
        for key in ("project", "run_at", "due", "created"):
            if self.meta.get(key):
                bits.append(f", {key}: {self.meta[key]}")
        return "".join(bits) + ")"

    def render(self) -> str:
        body = "".join(f"## {name}\n{text.strip()}\n\n" for name, text in self.sections.items())
        return join_frontmatter(self.meta, body)


class TaskStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, task_id: str) -> Path:
        if not re.fullmatch(r"[\w-]+", task_id):
            raise ValueError(f"bad task id {task_id!r}")
        return self.folder / f"{task_id}.md"

    def get(self, task_id: str) -> Task:
        p = self._path(task_id)
        if not p.exists():
            raise ValueError(f"no task {task_id}")
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        sections, current = {}, None
        for line in body.splitlines():
            m = re.match(r"^## (.+)$", line)
            if m:
                current = m.group(1).strip()
                sections[current] = ""
            elif current:
                sections[current] += line + "\n"
        return Task(task_id, meta, sections)

    def all(self, statuses: tuple[str, ...] | None = None) -> list[Task]:
        if not self.folder.exists():
            return []
        tasks = [self.get(p.stem) for p in sorted(self.folder.glob("*.md"))]
        return [t for t in tasks if statuses is None or t.status in statuses]

    def save(self, task: Task) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(task.id).write_text(task.render(), encoding="utf-8")

    def create(self, title: str, brief: str, worker: str = "main", status: str = "queued",
               created_by: str = "agent", run_at: str | None = None, due: str | None = None,
               parent: str | None = None, project: str | None = None) -> Task:
        if status not in ("proposed", "queued"):
            raise ValueError("new tasks start as proposed or queued")
        if run_at:
            run_at = parse_time(run_at).isoformat(timespec="minutes")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
        base = f"{now():%Y-%m-%d}-{slug}"
        task_id, n = base, 2
        while self._path(task_id).exists():
            task_id, n = f"{base}-{n}", n + 1
        meta = {"type": "task", "title": title, "status": status, "worker": worker, "created_by": created_by,
                "created": now().isoformat(timespec="minutes"), "run_at": run_at, "due": due, "parent": parent, "project": project}
        task = Task(task_id, {k: v for k, v in meta.items() if v is not None}, {"Brief": brief, "Log": ""})
        self.save(task)
        return task

    def update(self, task_id: str, log: str | None = None, **fields: Any) -> Task:
        task = self.get(task_id)
        for key, value in fields.items():
            if key == "status" and value not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            if key == "run_at" and value:
                value = parse_time(value).isoformat(timespec="minutes")
            if value is None:
                task.meta.pop(key, None)
            else:
                task.meta[key] = value
        if log:
            task.sections["Log"] = task.sections.get("Log", "") + f"- {now():%m-%d %H:%M} {log}\n"
        self.save(task)
        return task

    def set_result(self, task_id: str, result: str) -> None:
        task = self.get(task_id)
        task.sections["Result"] = result
        self.save(task)


def summary(tasks: list[Task]) -> str:
    return "\n".join(t.line() for t in tasks) or "No tasks."
