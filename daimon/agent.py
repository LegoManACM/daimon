"""The running agent: one inbox feeding the main thread, a scheduler, and a pool of workers.

Everything that reaches the main agent goes through the inbox and is handled one at a time: your messages,
wakes it scheduled, routine runs, and workers' results and questions.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from datetime import datetime

from . import inbox, tasktools
from .gate import Surface, Tool
from .harness.run import RateLimited, limit_reason
from .home import Home, split_frontmatter
from .mcp_hub import McpHub
from .projects import ProjectStore
from .runner import Runner
from .schedule import next_run, now, parse_time
from .tasks import TaskStore
from .thread import Thread

TICK_SECONDS = 20
QUIET = "[quiet]"


@dataclass
class Item:
    kind: str  # user | wake | routine | report | inbox
    text: str
    task_id: str | None = None
    reply: asyncio.Future | None = None  # set for messages whose sender waits for the answer


class Agent:
    def __init__(self, home: Home, surface: Surface):
        self.home, self.surface = home, surface
        self.hub = McpHub(home)
        self.tasks = TaskStore(home.root / "tasks")
        self.projects = ProjectStore(home.root / "projects")
        self.runner = Runner(home, self.hub, surface, self.tasks, main_extra=self.main_extra, projects=self.projects)
        home.ignore("projects/")  # homes made before projects existed
        self.thread = Thread(home.root / "threads" / "main")
        self.inbox: asyncio.Queue[Item] = asyncio.Queue()
        self.workers: dict[str, asyncio.Task] = {}
        self._tick = asyncio.Event()
        self._background: list[asyncio.Task] = []
        self._reported: set[str] = set()

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def start(self, scheduler: bool = True) -> None:
        for task in self.tasks.all(("running",)):  # left running by a previous process
            self.tasks.update(task.id, status="queued", log="restarted after the agent stopped")
        self._background.append(asyncio.create_task(self._main_loop()))
        if scheduler:
            self._background.append(asyncio.create_task(self._scheduler()))

    async def stop(self) -> None:
        for t in [*self._background, *self.workers.values()]:
            t.cancel()
        await asyncio.gather(*self._background, *self.workers.values(), return_exceptions=True)
        await self.hub.close()

    async def send(self, text: str) -> str:
        """A message from the user; returns the main agent's reply."""
        reply = asyncio.get_running_loop().create_future()
        await self.inbox.put(Item("user", text, reply=reply))
        return await reply

    def main_extra(self) -> list[Tool]:
        async def compact(_args: dict[str, Any]) -> str:
            done = await self.runner.maybe_compact(self.thread, force=True)
            return "Older conversation folded into the summary." if done else "Nothing to compact yet."

        return tasktools.main_tools(self.home, self.tasks, self._tick.set, self.projects) + [
            Tool("compact_conversation", "Fold the older part of this conversation into its summary.",
                 {"type": "object", "properties": {}}, compact, lambda _a: "auto")]

    # ---- main thread ---------------------------------------------------------------------------------
    async def _main_loop(self) -> None:
        while True:
            item = await self.inbox.get()
            try:
                reply = await self.runner.turn(self.thread, self._message(item))
                if item.kind == "wake" and item.task_id:
                    self.tasks.set_result(item.task_id, reply)
                    self.tasks.update(item.task_id, status="done", log="handled")
                if item.reply:
                    item.reply.set_result(reply)
                elif reply.strip() and not reply.strip().startswith(QUIET):
                    self.surface.say(reply)
            except Exception as e:  # noqa: BLE001 - the loop must survive a failed turn
                if item.kind == "wake" and item.task_id:
                    self.tasks.update(item.task_id, status="failed", log=f"error: {e}")
                if item.reply:
                    item.reply.set_exception(e)
                else:
                    self.surface.event("error", f"handling {item.kind}: {e}")

    def _message(self, item: Item) -> str:
        if item.kind == "user":
            return item.text
        labels = {"wake": "Wake-up you scheduled", "routine": "Routine", "report": "Worker report",
                  "inbox": "Inbox item"}
        return f"[{labels[item.kind]}]\n{item.text}\n\n(Background event: the user didn't send this. Reply " \
               f"with what they should hear, or start your reply with {QUIET} if nothing needs their attention.)"

    # ---- scheduler -----------------------------------------------------------------------------------
    async def _scheduler(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.surface.event("error", f"scheduler: {e}")
            self._tick.clear()
            try:
                await asyncio.wait_for(self._tick.wait(), TICK_SECONDS)
            except TimeoutError:
                pass

    def tick(self) -> None:
        self._routines()
        self._drops()
        self._dispatch()

    def _drops(self) -> None:
        """Inbox items from other processes: urgent ones to main, the rest batched for the curator worker."""
        items = inbox.take(self.home)
        urgent = [i for i in items if str(i.meta.get("for", "")).lower() == "main"]
        rest = [i for i in items if i not in urgent]
        for item in urgent:
            self.inbox.put_nowait(Item("inbox", item.describe()))
        if not rest:
            return
        curator = self.home.config().get("knowledge", {}).get("curator", "curator")
        listing = "\n\n".join(i.describe() for i in rest)
        if (self.home.root / "workers" / f"{curator}.md").exists():
            self.tasks.create(f"Curate {len(rest)} inbox item{'s' if len(rest) > 1 else ''}",
                              f"New inbox items. File what's worth keeping into the knowledge graph.\n\n{listing}",
                              worker=curator, created_by="inbox")
        else:
            self.inbox.put_nowait(Item("inbox", f"There's no `{curator}` worker to file these, so they came to "
                                                f"you:\n\n{listing}"))

    def _routines(self) -> None:
        state_file = self.home.cache / "routines.json"
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
        at = now()
        for path in sorted((self.home.root / "routines").glob("*.md")):
            name = path.stem
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
            schedule = str(meta.get("schedule", ""))
            if not meta.get("enabled", True):
                continue
            try:
                entry = state.get(name)
                if not entry or entry.get("schedule") != schedule:  # new or changed: first run at the next slot
                    entry = state[name] = {"schedule": schedule, "next": next_run(schedule, at).isoformat()}
                if parse_time(entry["next"]) <= at:  # also catches runs missed while the agent was off
                    self._fire(name, meta, body)
                    entry["next"] = next_run(schedule, at).isoformat()
                    entry["last"] = at.isoformat(timespec="minutes")
            except ValueError as e:
                if f"routine:{name}:{schedule}" not in self._reported:
                    self._reported.add(f"routine:{name}:{schedule}")
                    self.surface.event("error", f"routines/{name}.md: {e}")
        self.home.cache.mkdir(exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _fire(self, name: str, meta: dict[str, Any], body: str) -> None:
        worker = meta.get("worker", "main")
        if worker == "main":
            self.inbox.put_nowait(Item("routine", f"{name}\n\n{body.strip()}"))
        else:
            self.tasks.create(f"{name} {now():%m-%d}", body.strip(), worker=worker, created_by=f"routine:{name}",
                              project=meta.get("project"))
        self.surface.event("routine", name)

    def _dispatch(self) -> None:
        for task_id, running in list(self.workers.items()):
            if self.tasks.get(task_id).status == "cancelled":
                running.cancel()
        limit = self.home.config().get("limits", {}).get("max_concurrent_workers", 3)
        at = now()
        busy = {self.tasks.get(t).meta.get("project") for t in self.workers} - {None}
        for task in self.tasks.all(("queued",)):
            if not task.due_now(at) or task.id in self.workers:
                continue
            project = task.meta.get("project")
            if project and (project in busy or self._project_paused(project)):  # one dispatch at a time per project
                continue
            if task.worker != "main" and (blocked := self._harness_blocked(task)):
                if task.meta.get("blocked") != blocked:
                    self.tasks.update(task.id, log=f"waiting: {blocked}", blocked=blocked)
                continue
            if task.worker == "main":
                self.tasks.update(task.id, status="running", log="woke main")
                self.inbox.put_nowait(Item("wake", f"{task.title}\n\n{task.sections.get('Brief', '').strip()}",
                                           task.id))
            elif limit in (None, 0, "unlimited") or len(self.workers) < int(limit):
                self.tasks.update(task.id, status="running", log=f"started by {task.worker}", blocked=None)
                self.workers[task.id] = asyncio.create_task(self._work(task.id))
                busy.add(project)

    def _project_paused(self, name: str) -> bool:
        return self.projects.exists(name) and self.projects.get(name).status != "active"

    def _harness(self, task_id: str) -> str | None:
        try:
            harness = self.home.worker(self.tasks.get(task_id).worker).meta.get("harness")
        except (FileNotFoundError, ValueError):
            return None  # _work reports the missing worker
        return None if harness in (None, "", "native") else str(harness)

    def _harness_blocked(self, task) -> str:
        harness = self._harness(task.id)
        if not harness:
            return ""
        running = sum(1 for t in self.workers if self._harness(t) == harness)
        return limit_reason(self.home, harness, running)

    async def _work(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        self.surface.event("task", f"{task.worker} started {task_id}")
        try:
            status, text = await self.runner.run_task(task)
            if self.tasks.get(task_id).status == "cancelled":
                return
            if status == "waiting":
                self.tasks.update(task_id, status="waiting", log=f"asked main: {text[:200]}")
                report = f"Task {task_id} ({task.title}), worker {task.worker}, asks:\n{text}\n\n" \
                         "Answer with answer_task (ask the user first if only they know)."
            else:
                self.tasks.set_result(task_id, text)
                self.tasks.update(task_id, status="done", log="done")
                report = f"Task {task_id} ({task.title}) finished by {task.worker}. Result:\n{text}"
        except RateLimited as e:  # resumes in the same session once the limit resets
            when = datetime.fromtimestamp(e.retry_at).astimezone().isoformat(timespec="minutes")
            self.tasks.update(task_id, status="queued", run_at=when, log=f"rate-limited until {when}: {e}")
            self.surface.event("notify", f"{task.worker} is rate-limited; {task_id} resumes at {when[11:16]}")
            return
        except asyncio.CancelledError:
            self.tasks.update(task_id, log="stopped")
            raise
        except Exception as e:  # noqa: BLE001
            self.tasks.update(task_id, status="failed", log=f"error: {e}")
            report = f"Task {task_id} ({task.title}) failed: {e}"
        finally:
            self.workers.pop(task_id, None)
            self._tick.set()
        await self.inbox.put(Item("report", report, task_id))
