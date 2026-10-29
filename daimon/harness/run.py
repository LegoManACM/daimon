"""Running a task on a harness: everything that's the same whatever the harness is.

- The task's thread (threads/tasks/<id>) records what was sent and what came back, like a native worker's.
- Sessions resume: within a task (after a question or a restart) and across a project's tasks. A session that
  can't be resumed is replaced by a fresh one that gets the project's notes and log and the task so far.
- Drivers that lend tools give the harness ours (kg_search, ask_main, report_progress, update_project_notes).
  For the others, the harness ends its reply with QUESTION: or NOTES: lines, which we parse.
- Rate limits re-queue the task for when the limit resets; the session resumes then.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import gate, knowledge
from ..gate import Tool
from ..home import Home, Worker
from ..projects import Project, ProjectStore
from ..tasks import Task
from ..tasktools import STR, WorkerRun, auto, obj, task_thread, worker_tools
from . import Job, Outcome, consent, load, model_entry, workspace

if TYPE_CHECKING:
    from ..runner import Runner

GUIDE = """# How this works
You are a worker for the user's personal agent ("main"), running a task it handed you. The user isn't watching
this session; main reads your final message and decides what to tell them.

- Work the task through to the end on your own, in your working directory.
- Finish with a short report: what you did, what you found, what's still open. That message is your result.
- This session may be resumed later, for an answer to your question or for the next task in the same project."""

TOOLS_GUIDE = """- Tools from the `daimon` server: `report_progress` for notable steps, `ask_main` when you're blocked on something
  only main or the user can answer (then stop: you'll be resumed with the answer), `kg_search`/`kg_read` for
  what the agent knows about the user and their world."""

PROJECT_TOOLS_GUIDE = """- This task is part of a project that continues over many tasks and days. Before you finish, call
  `update_project_notes` with the current state and next steps, so whoever picks it up next (maybe you, maybe a
  fresh session) can carry on."""

MARKERS_GUIDE = """- If you're blocked on something only the user can answer, end your reply with a line `QUESTION: <one
  clear question>` and stop. You'll be resumed with the answer."""

PROJECT_MARKERS_GUIDE = """- This task is part of a project that continues over many tasks and days. End your reply with a
  `NOTES:` section: the current state and next steps, for whoever picks it up next."""


class RateLimited(Exception):
    def __init__(self, retry_at: float, message: str):
        super().__init__(message)
        self.retry_at = retry_at


def runs_today(home: Home, harness: str) -> int:
    path = home.cache / "harness_runs.jsonl"
    if not path.exists():
        return 0
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return sum(1 for r in rows if r["ts"] >= start and r["harness"] == harness)


def limit_reason(home: Home, harness: str, running: int = 0) -> str:
    """Why a task for this harness can't start now, or ''. `running`: runs started but not yet recorded."""
    limits = ((home.config().get("limits") or {}).get("harness") or {}).get(harness) or {}
    cap = limits.get("max_runs_per_day")
    if cap not in (None, "unlimited") and runs_today(home, harness) + running >= int(cap):
        return f"{harness} reached max_runs_per_day ({cap})"
    return ""


def project_tools(projects: ProjectStore, project: Project) -> list[Tool]:
    def update_project_notes(notes: str) -> str:
        projects.update(project.name, notes=notes)
        return "Project notes saved."

    return [auto("update_project_notes",
                 "Replace the project's handoff notes: the current state of the work and the next steps. The next "
                 "task in this project starts from them.",
                 obj(["notes"], notes=STR), update_project_notes)]


def recap(task: Task, thread_messages: list[dict[str, Any]]) -> str:
    """The task so far, for a fresh session that replaces one we couldn't resume."""
    lines = [f"(Your earlier session on this task couldn't be resumed. Here's where it stands.)\n\n"
             f"Task: {task.title}\n\n{task.sections.get('Brief', '').strip()}"]
    for m in thread_messages[1:]:
        who = "You said" if m["role"] == "assistant" else "Then"
        lines.append(f"{who}: {str(m.get('content') or '')[:3000]}")
    return "\n\n".join(lines)


def parse_markers(text: str) -> tuple[str, str, str | None]:
    """Split QUESTION: and NOTES: off a reply from a harness without our tools. Returns (text, question, notes)."""
    notes = None
    m = re.search(r"^\s*NOTES:\s*(.*)$", text, re.S | re.M)
    if m:
        notes, text = m.group(1).strip(), text[:m.start()].rstrip()
    q = re.search(r"^\s*QUESTION:\s*(.+)$", text, re.M)
    question = q.group(1).strip() if q else ""
    return text, question, notes


async def run_task(runner: "Runner", task: Task, worker: Worker, projects: ProjectStore) -> tuple[str, str]:
    home, store = runner.home, runner.tasks
    harness = str(worker.meta["harness"])
    driver = load(harness, home.config())
    driver.options.update(worker.meta.get("harness_options") or {})
    project = projects.get(task.meta["project"]) if task.meta.get("project") else None

    path, branch = workspace.prepare(home, project.name if project else task.id,
                                     *([project.meta] if project else []), worker.meta)
    if project and project.meta.get("workspace") != str(path):
        projects.update(project.name, workspace=str(path), branch=branch)
        project = projects.get(project.name)
    store.update(task.id, workspace=str(path))

    thread = task_thread(home, task.id)
    if not thread.messages:
        due = f"\n\nDue: {task.meta['due']}" if task.meta.get("due") else ""
        thread.append({"role": "user", "content": f"Task: {task.title}\n\n{task.sections.get('Brief', '').strip()}{due}"})
    last = thread.messages[-1]
    session = task.meta.get("session") or (project.session(task.worker, harness) if project else None)
    if last["role"] == "user":
        prompt = last["content"]
        if task.meta.get("session") and len(thread.messages) == 1:
            prompt = f"(Your previous run was interrupted.) Continue this task:\n\n{prompt}"
    else:  # restarted after the last run had already answered: nothing new to say
        prompt = "(Your previous run was interrupted.) Continue the task where you left off."

    run = WorkerRun(task)
    lent = driver.lends_tools
    tools = worker_tools(store, run) + [t for t in knowledge.tools(runner.kb) if t.name in ("kg_search", "kg_read",
                                                                                            "kg_write")]
    if project:
        tools += project_tools(projects, project)
    context = await runner.retrieve(f"{task.title}\n{task.sections.get('Brief', '')}")
    guide = [GUIDE, TOOLS_GUIDE if lent else MARKERS_GUIDE]
    if project:
        guide.append(PROJECT_TOOLS_GUIDE if lent else PROJECT_MARKERS_GUIDE)
    system = "\n\n---\n\n".join(s.strip() for s in [
        "\n".join(guide), home.read("POLICY.md"), f"# You are: {worker.name}\n\n{worker.prompt}",
        project.context() if project else "", runner.knowledge_section(context),
        f"# Your task\n- id: {task.id}\n- title: {task.title}" + (f"\n- due: {task.meta['due']}" if task.meta.get("due") else ""),
    ] if s.strip())

    who = f"{task.worker} · {task.id}"
    rules = (worker.meta.get("harness_consent"), driver.options.get("consent"))

    async def approve(tool_name: str, args: dict[str, Any]) -> tuple[bool, str]:
        lvl = consent.level(tool_name, args, path, *rules)
        if lvl == "ask":
            proxy = Tool(f"{harness}:{tool_name}", f"{harness} wants to run {consent.describe(tool_name, args)}",
                         {}, lambda _a: None, lambda _a: "ask")
            decision = await runner.surface.approve(proxy, args, can_always=False, who=who)
            return decision.approved, decision.note
        if lvl == "notify":
            runner.surface.event("notify", f"[{who}] {consent.describe(tool_name, args)}")
        return True, ""

    def gated(tool: Tool) -> Tool:  # lent tools keep their consent levels
        async def call(args: dict[str, Any]) -> str:
            runner.surface.event("tool", f"[{who}] {tool.name} {gate.short(args)}")
            return await gate.call(tool, args, runner.surface, who)
        return Tool(tool.name, tool.description, tool.schema, call, lambda _a: "auto")

    model = worker.model or driver.options.get("default_model") or None
    model_entry(harness, model, home.config())  # a LiteLLM id here would be a mistake: refuse it, don't guess
    job = Job(task.id, task.worker, harness, prompt, system, path, driver.options, session, model,
              [gated(t) for t in tools], approve,
              event=lambda text: runner.surface.event("tool", f"[{who}] {text}"), stop=lambda: run.stop)

    before = workspace.head(path)
    store.update(task.id, log=f"{harness}: {'resuming session' if session else 'new session'} in {path}")
    outcome = await driver.run(job)
    if outcome.error and not outcome.started and session:
        store.update(task.id, log=f"couldn't resume session {session}; starting fresh ({outcome.error[:200]})")
        job.session = None
        job.prompt = recap(task, thread.messages) if len(thread.messages) > 1 else thread.messages[0]["content"]
        outcome = await driver.run(job)
    record(home, harness, task.worker, outcome, driver.options)

    if outcome.session:
        store.update(task.id, session=outcome.session)
        if project:
            projects.set_session(project.name, task.worker, harness, outcome.session)
    if outcome.retry_at:
        raise RateLimited(outcome.retry_at, outcome.error or f"{harness} is rate-limited")
    if outcome.error:
        raise RuntimeError(f"{harness}: {outcome.error}")

    text, question, notes = outcome.text, run.question, None
    if not lent:
        text, question, notes = parse_markers(text)
        if notes is not None and project:
            projects.update(project.name, notes=notes)
    thread.append({"role": "assistant", "content": outcome.text})
    if question:
        return "waiting", question

    changed = workspace.changes(path, before)
    result = text.strip() + f"\n\nWorkspace: {path}" + (f" (branch {branch})" if branch else "")
    if changed:
        result += f"\n{changed}"
    if project:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "(no report)")
        projects.update(project.name, log=f"{task.id} ({task.worker}): {first[:200]}")
    return "done", result


def record(home: Home, harness: str, worker: str, outcome: Outcome, options: dict[str, Any]) -> None:
    """Count the run for limits, and its spend: real for API keys, notional on a subscription."""
    if outcome.error and not outcome.started:  # never reached the agent (not installed, bad config): free
        return
    home.cache.mkdir(exist_ok=True)
    billed = options.get("auth", "subscription") == "api_key"
    with (home.cache / "harness_runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "harness": harness, "worker": worker, "usd": outcome.cost_usd}) + "\n")
    with (home.cache / "spend.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "worker": worker, "model": f"harness:{harness}",
                            "usd": outcome.cost_usd if billed else 0.0, "notional_usd": outcome.cost_usd}) + "\n")
