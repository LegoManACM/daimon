"""Projects: long-running work that spans many tasks and days. One markdown file each in projects/.

A project keeps what a single task can't: a workspace that persists between dispatches, the harness session to
resume, handoff notes (the current state and what's next, rewritten by whoever worked on it last) and a log of
every dispatch. Tasks join a project with `project: <name>`; each one picks up where the last left off, and if
a harness session is lost, the notes and log are enough to start a fresh one.

Like tasks, projects are state: git-ignored and changed through tools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .home import join_frontmatter, split_frontmatter
from .schedule import now

STATUSES = ("active", "paused", "done")


@dataclass
class Project:
    name: str
    meta: dict[str, Any]
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.meta.get("status", "active")

    @property
    def title(self) -> str:
        return self.meta.get("title", self.name)

    def session(self, worker: str, harness: str) -> str | None:
        """The harness session to resume, if the worker last ran this project on the same harness."""
        entry = (self.meta.get("sessions") or {}).get(worker) or {}
        return entry.get("id") if entry.get("harness") == harness else None

    def line(self) -> str:
        bits = [f"{self.name} [{self.status}] {self.title}"]
        for key in ("worker", "workspace"):
            if self.meta.get(key):
                bits.append(f"{key}: {self.meta[key]}")
        return " · ".join(bits)

    def context(self, log_lines: int = 15) -> str:
        """The project as a prompt section: goal, handoff notes and the most recent log entries."""
        log = [ln for ln in self.sections.get("Log", "").splitlines() if ln.strip()][-log_lines:]
        parts = [f"# Project: {self.title} ({self.name})"]
        if self.meta.get("workspace"):
            parts.append(f"Workspace: {self.meta['workspace']}" +
                         (f" (branch {self.meta['branch']})" if self.meta.get("branch") else ""))
        parts.append(f"## Goal\n{self.sections.get('Goal', '').strip() or '(not set)'}")
        parts.append(f"## Handoff notes (state and next steps, from the last dispatch)\n"
                     f"{self.sections.get('Notes', '').strip() or '(none yet: this is the first dispatch)'}")
        if log:
            parts.append("## Recent log\n" + "\n".join(log))
        return "\n\n".join(parts)

    def render(self) -> str:
        body = "".join(f"## {name}\n{text.strip()}\n\n" for name, text in self.sections.items())
        return join_frontmatter(self.meta, body)


class ProjectStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
            raise ValueError(f"bad project name {name!r}: use lowercase letters, digits and dashes")
        return self.folder / f"{name}.md"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def get(self, name: str) -> Project:
        p = self._path(name)
        if not p.exists():
            raise ValueError(f"no project {name}")
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        sections, current = {}, None
        for line in body.splitlines():
            m = re.match(r"^## (.+)$", line)
            if m:
                current = m.group(1).strip()
                sections[current] = ""
            elif current:
                sections[current] += line + "\n"
        return Project(name, meta, sections)

    def all(self, statuses: tuple[str, ...] | None = None) -> list[Project]:
        if not self.folder.exists():
            return []
        projects = [self.get(p.stem) for p in sorted(self.folder.glob("*.md"))]
        return [p for p in projects if statuses is None or p.status in statuses]

    def save(self, project: Project) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        project.meta["updated"] = now().isoformat(timespec="minutes")
        self._path(project.name).write_text(project.render(), encoding="utf-8")

    def create(self, name: str, title: str, goal: str, worker: str | None = None, repo: str | None = None,
               folder: str | None = None) -> Project:
        if self._path(name).exists():
            raise ValueError(f"project {name} already exists")
        if repo and folder:
            raise ValueError("give repo (work in a git worktree of it) or folder (work in it directly), not both")
        meta = {"type": "project", "title": title, "status": "active", "worker": worker, "repo": repo,
                "folder": folder, "created": now().isoformat(timespec="minutes")}
        project = Project(name, {k: v for k, v in meta.items() if v}, {"Goal": goal, "Notes": "", "Log": ""})
        self.save(project)
        return project

    def update(self, name: str, log: str | None = None, notes: str | None = None, **fields: Any) -> Project:
        project = self.get(name)
        for key, value in fields.items():
            if key == "status" and value not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
            if value is None:
                project.meta.pop(key, None)
            else:
                project.meta[key] = value
        if notes is not None:
            project.sections["Notes"] = notes
        if log:
            project.sections["Log"] = project.sections.get("Log", "") + f"- {now():%m-%d %H:%M} {log}\n"
        self.save(project)
        return project

    def set_session(self, name: str, worker: str, harness: str, session: str | None) -> None:
        project = self.get(name)
        sessions = dict(project.meta.get("sessions") or {})
        if session:
            sessions[worker] = {"harness": harness, "id": session}
        else:
            sessions.pop(worker, None)
        project.meta["sessions"] = sessions
        self.save(project)


def summary(projects: list[Project]) -> str:
    return "\n".join(p.line() for p in projects) or "No projects."
