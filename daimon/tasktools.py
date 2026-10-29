"""Tools for tasks: the main agent plans and manages work; workers report back on the task they're running."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from .gate import Tool
from .home import Home
from .projects import ProjectStore
from .projects import summary as project_summary
from .schedule import now, parse_time
from .tasks import OPEN, Task, TaskStore, summary
from .thread import Thread

STR = {"type": "string"}


def obj(required: list[str], **props: Any) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


def auto(name: str, description: str, schema: dict[str, Any], fn: Callable[..., str]) -> Tool:
    async def run(args: dict[str, Any]) -> str:
        return fn(**args)
    return Tool(name, description, schema, run, lambda _a: "auto")


def task_thread(home: Home, task_id: str) -> Thread:
    return Thread(home.root / "threads" / "tasks" / task_id)


def main_tools(home: Home, store: TaskStore, wake: Callable[[], None],
               projects: ProjectStore | None = None) -> list[Tool]:
    """`wake` nudges the scheduler so a task that's due now starts without waiting for the next tick."""
    projects = projects or ProjectStore(home.root / "projects")

    def check_worker(worker: str) -> None:
        home.worker(worker)  # raises if workers/<name>.md doesn't exist

    def resolve_when(when: str | None, in_minutes: float | None) -> str | None:
        """Relative times avoid clock arithmetic by the model; absolute ones mustn't be in the past."""
        if in_minutes is not None:
            return (now() + timedelta(minutes=float(in_minutes))).isoformat(timespec="minutes")
        if when and parse_time(when) < now() - timedelta(minutes=2):
            raise ValueError(f"{when} is in the past (it's now {now():%Y-%m-%dT%H:%M}). Use in_minutes for "
                             "relative times.")
        return when

    def create_task(title: str, brief: str, worker: str | None = None, status: str = "queued",
                    run_at: str | None = None, in_minutes: float | None = None, due: str | None = None,
                    project: str | None = None) -> str:
        if project:
            p = projects.get(project)
            if p.status != "active":
                raise ValueError(f"project {project} is {p.status}; set it active first")
            worker = worker or p.meta.get("worker")
        if not worker:
            raise ValueError("give a worker (or a project that has one)")
        check_worker(worker)
        task = store.create(title, brief, worker=worker, status=status, run_at=resolve_when(run_at, in_minutes),
                            due=due, project=project)
        wake()
        return f"Created {task.line()}"

    def schedule_wake(note: str, when: str | None = None, in_minutes: float | None = None) -> str:
        run_at = resolve_when(when, in_minutes)
        if not run_at:
            raise ValueError("give either when (ISO time) or in_minutes")
        task = store.create(note.splitlines()[0][:60], note, worker="main", run_at=run_at)
        wake()
        return f"Scheduled: you'll be woken at {task.meta['run_at']} with this note. ({task.id})"

    def list_tasks(status: str = "open") -> str:
        if status == "open":
            return summary(store.all(OPEN))
        return summary(store.all(None if status == "all" else (status,)))

    def read_task(task_id: str) -> str:
        return store.get(task_id).render()

    def update_task(task_id: str, status: str | None = None, run_at: str | None = None,
                    worker: str | None = None, note: str | None = None) -> str:
        if worker:
            check_worker(worker)
        fields = {k: v for k, v in {"status": status, "run_at": run_at, "worker": worker}.items() if v}
        task = store.update(task_id, log=note or (f"status -> {status}" if status else None), **fields)
        wake()
        return f"Updated {task.line()}"

    def answer_task(task_id: str, answer: str) -> str:
        task = store.get(task_id)
        if task.status != "waiting":
            return f"{task_id} isn't waiting for an answer (it's {task.status})."
        task_thread(home, task_id).append({"role": "user", "content": f"Answer from the main agent: {answer}"})
        store.update(task_id, log=f"answered: {answer[:200]}", status="queued")
        wake()
        return f"Answered; {task_id} is queued to continue."

    def create_project(name: str, title: str, goal: str, worker: str | None = None, repo: str | None = None,
                       folder: str | None = None) -> str:
        if worker:
            check_worker(worker)
        p = projects.create(name, title, goal, worker=worker, repo=repo, folder=folder)
        return f"Created project {p.line()}. Give it tasks with create_task(project='{name}')."

    def list_projects(status: str = "active") -> str:
        return project_summary(projects.all(None if status == "all" else (status,)))

    def read_project(name: str) -> str:
        p = projects.get(name)
        tasks = [t for t in store.all() if t.meta.get("project") == name]
        return p.render() + "\n## Tasks\n" + summary(tasks)

    def update_project(name: str, status: str | None = None, worker: str | None = None, goal: str | None = None,
                       notes: str | None = None, note: str | None = None, new_session: bool = False) -> str:
        if worker:
            check_worker(worker)
        fields = {k: v for k, v in {"status": status, "worker": worker}.items() if v}
        p = projects.update(name, log=note or (f"status -> {status}" if status else None), notes=notes, **fields)
        if goal:
            p.sections["Goal"] = goal
            projects.save(p)
        if new_session:
            p.meta["sessions"] = {}
            projects.save(p)
        return f"Updated project {p.line()}"

    when = {"type": "string", "description": "ISO local date/time, e.g. 2026-10-27T09:00"}
    in_minutes = {"type": "number", "description": "Instead of a time: how many minutes from now"}
    return [
        auto("create_task",
             "Create a task for a worker (or for yourself, worker 'main'). status 'proposed' means it waits for "
             "the user's go-ahead; 'queued' runs at run_at, or right away.",
             obj(["title", "brief"], title=STR,
                 worker={"type": "string", "description": "Required unless the project has a default worker"},
                 brief={"type": "string", "description": "Everything the worker needs: goal, context, what done "
                        "looks like. Workers don't see your conversation."},
                 status={"type": "string", "enum": ["proposed", "queued"]}, run_at=when, in_minutes=in_minutes,
                 due=when,
                 project={"type": "string", "description": "Part of this project: the worker continues from the "
                          "project's workspace, notes and (for harness workers) session"}),
             create_task),
        auto("schedule_wake",
             "Wake yourself later with a note (a reminder, a follow-up, a check-in). The note arrives as a message "
             "to you at that time.",
             obj(["note"], note=STR, when=when, in_minutes=in_minutes), schedule_wake),
        auto("list_tasks", "List tasks. status: open (default), all, or one status.",
             obj([], status=STR), list_tasks),
        auto("read_task", "Read a task's brief, log and result.", obj(["task_id"], task_id=STR), read_task),
        auto("update_task",
             "Change a task: approve a proposal (status queued), cancel it, reschedule it, reassign it, or add a "
             "note to its log.",
             obj(["task_id"], task_id=STR, status={"type": "string", "enum": ["proposed", "queued", "cancelled",
                                                                               "done", "failed"]},
                 run_at=when, worker=STR, note=STR),
             update_task),
        auto("answer_task", "Answer a worker's question on a waiting task; the worker continues with it.",
             obj(["task_id", "answer"], task_id=STR, answer=STR), answer_task),
        auto("create_project",
             "Start a project: long-running work over many tasks and days, with one workspace and running handoff "
             "notes. Give repo (a git repository: work happens in a worktree on branch daimon/<name>) or folder "
             "(work directly in it), or neither for a scratch folder.",
             obj(["name", "title", "goal"],
                 name={"type": "string", "description": "lowercase-with-dashes"}, title=STR,
                 goal={"type": "string", "description": "What the project is for and what done looks like"},
                 worker={"type": "string", "description": "Default worker for its tasks"}, repo=STR, folder=STR),
             create_project),
        auto("list_projects", "List projects. status: active (default), paused, done, or all.",
             obj([], status=STR), list_projects),
        auto("read_project", "Read a project: goal, handoff notes, log, sessions and its tasks.",
             obj(["name"], name=STR), read_project),
        auto("update_project",
             "Change a project: pause/finish/reactivate it (status), change its default worker or goal, rewrite "
             "its handoff notes, add a log note, or new_session=true to make the next task start a fresh harness "
             "session (from the notes) instead of resuming.",
             obj(["name"], name=STR, status={"type": "string", "enum": ["active", "paused", "done"]}, worker=STR,
                 goal=STR, notes=STR, note=STR, new_session={"type": "boolean"}),
             update_project),
    ]


@dataclass
class WorkerRun:
    """Per-run state a worker's tools can change."""
    task: Task
    question: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def stop(self) -> bool:
        return bool(self.question)


def worker_tools(store: TaskStore, run: WorkerRun) -> list[Tool]:
    def report_progress(note: str) -> str:
        store.update(run.task.id, log=note)
        return "Noted in the task log."

    def ask_main(question: str) -> str:
        run.question = question
        return "Question sent. Stop here; you'll continue with the answer."

    return [
        auto("report_progress", "Add a short progress note to your task's log.",
             obj(["note"], note=STR), report_progress),
        auto("ask_main",
             "Ask the main agent a question you can't resolve yourself (it may ask the user). Your run pauses "
             "until the answer arrives.",
             obj(["question"], question=STR), ask_main),
    ]
