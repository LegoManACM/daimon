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

from . import gate, inbox, knowledge, progress, reactions, tasktools
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
UPDATE_FIRST_CHECK_S = 60      # after starting, so a restart loop doesn't hammer GitHub
UPDATE_EVERY_S = 3600          # how often to look (GitHub itself is asked at most daily)
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
        self._reacting: set[asyncio.Task] = set()
        self._reported: set[str] = set()
        self.auto_update = False  # `daimon run` turns it on; updates.auto in config.yaml decides

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def start(self, scheduler: bool = True) -> None:
        for task in self.tasks.all(("running",)):  # left running by a previous process
            self.tasks.update(task.id, status="queued", log="restarted after the agent stopped")
        self._background.append(asyncio.create_task(self._main_loop()))
        if scheduler:
            self._background.append(asyncio.create_task(self._scheduler()))
            self._background.append(asyncio.create_task(self._updates()))

    async def stop(self) -> None:
        running = [*self._background, *self.workers.values(), *self._reacting]
        for t in running:
            t.cancel()
        await asyncio.gather(*running, return_exceptions=True)
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

        async def recent_events(args: dict[str, Any]) -> str:
            rows = reactions.recent(self.home, int(args.get("limit") or 10), args.get("event") or "*")
            if not rows:
                return "No events yet."
            return "\n".join(f"- {datetime.fromtimestamp(r['ts']):%m-%d %H:%M} {r['event']} → "
                             f"{', '.join(r['handled']) or 'no reaction'}\n  data: "
                             f"{json.dumps(r['data'], ensure_ascii=False, default=str)}" for r in rows)

        async def preview_reaction(args: dict[str, Any]) -> str:
            name = args["name"]
            text = args.get("content") or self.home.read(f"reactions/{name}.md")
            if not text:
                return f"reactions/{name}.md doesn't exist; pass content to preview a draft."
            r = reactions.parse(name, text)
            tools = await self.reaction_tools()
            lines = [r.describe(), f"tool {r.call}: " + ("found" if r.call in tools else "NOT FOUND (check the name "
                     "in mcp_status; MCP tools are named <server>__<tool>)")]
            for row in reactions.recent(self.home, 50)[::-1]:
                event = reactions.Event(row["event"], row["data"])
                if not r.matches(event):
                    continue
                try:
                    lines.append(f"Latest matching event ({row['event']}) would call it with: "
                                 f"{json.dumps(r.render(event.data), ensure_ascii=False, default=str)}")
                except ValueError as e:
                    lines.append(f"Latest matching event ({row['event']}) would fail: {e}")
                break
            else:
                lines.append("No logged event matches it yet (recent_events shows what has arrived).")
            return "\n".join(lines)

        obj = {"type": "object", "properties": {}}
        return tasktools.main_tools(self.home, self.tasks, self._tick.set, self.projects) + [
            Tool("compact_conversation", "Fold the older part of this conversation into its summary.",
                 obj, compact, lambda _a: "auto"),
            Tool("recent_events", "Events that arrived from add-ons, routines and reactions, with their data and "
                 "which reactions handled them. Use it to see an event's fields before writing a reaction.",
                 {**obj, "properties": {"limit": {"type": "integer"}, "event": {
                     "type": "string", "description": "name or glob, e.g. messages.*"}}},
                 recent_events, lambda _a: "auto"),
            Tool("preview_reaction", "Check a reaction (reactions/<name>.md, or a draft passed as content) without "
                 "running it: whether its tool exists and what it would call for the latest matching event.",
                 {**obj, "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["name"]}, preview_reaction, lambda _a: "auto")]

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

    # ---- updates -------------------------------------------------------------------------------------
    async def _updates(self) -> None:
        """Look for a new Daimon release (at most daily, see setup/update.py); say so once, or install it when
        updates.auto is on and nothing is running."""
        from .setup import update
        await asyncio.sleep(UPDATE_FIRST_CHECK_S)
        while True:
            check, auto = update.settings(self.home)
            try:
                version = await asyncio.to_thread(update.available, self.home) if check else None
            except ConnectionError:
                version = None
            if version and auto and self.auto_update and not self.workers:
                try:
                    await asyncio.to_thread(update.launch, self.home, version, None)
                    self.surface.event("notify", f"Updating Daimon to {version}; back in a minute.")
                except Exception as e:  # noqa: BLE001
                    self.surface.event("error", f"update to {version}: {e}")
            elif version and update.announce_once(self.home, version):
                self.surface.event("notify", f"Daimon {version} is out (you have {update.current()}). Run "
                                             "`daimon update` to install it.")
            await asyncio.sleep(UPDATE_EVERY_S)

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
        """Inbox items from other processes. Reactions that want one call their tool directly; the rest go to
        main (urgent ones) or are batched for the curator worker."""
        rest = []
        for item in inbox.take(self.home):
            event = reactions.Event.from_inbox(item.meta, item.body)
            if self.react(event):
                continue
            if str(item.meta.get("for", "")).lower() == "main":
                self.inbox.put_nowait(Item("inbox", item.describe()))
            else:
                rest.append(item)
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

    # ---- reactions -----------------------------------------------------------------------------------
    def react(self, event: reactions.Event) -> bool:
        """Start every reaction that wants this event. True if they took it (none asked to pass it on)."""
        found, errors = reactions.load(self.home)
        for e in errors:
            if e not in self._reported:
                self._reported.add(e)
                self.surface.event("error", e)
        matched = [r for r in found if r.matches(event)] if event.depth <= reactions.MAX_DEPTH else []
        reactions.record(self.home, event, [r.name for r in matched])
        for r in matched:
            t = asyncio.create_task(self._react(r, event))
            self._reacting.add(t)
            t.add_done_callback(self._reacting.discard)
        return bool(matched) and not any(r.passes for r in matched)

    async def reaction_tools(self) -> dict[str, Tool]:
        """What a reaction may call: every MCP tool, tasks and wakes, knowledge. Not self-edits: event data
        comes from other people (texts, mail), so it never gets to change the agent's own files."""
        await self.hub.sync()
        tools = self.hub.tools("all") + self.main_extra() + knowledge.tools(self.runner.kb)
        return {t.name: t for t in tools}

    async def _react(self, r: reactions.Reaction, event: reactions.Event) -> None:
        who = f"reaction:{r.name}"
        try:
            tool = (await self.reaction_tools()).get(r.call)
            if tool is None:
                raise ValueError(f"no tool named {r.call} (is its MCP server connected?)")
            args = r.render(event.data)
            self.surface.event("tool", f"[{who}] {r.call} {gate.short(args)}")
            progress.tool(self.surface, who, r.call, running=True)
            try:
                result = await gate.call(tool, args, self.surface, who)
            finally:
                progress.idle(self.surface, who)
            if result.startswith("Error:"):
                raise RuntimeError(result[6:].strip())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one bad reaction must not stop the others
            self._react_failed(r, event, str(e))
            return
        if result.startswith("The user declined"):
            self.surface.event("notify", f"{who}: declined")
            return
        first = result.strip().splitlines()[0][:200] if result.strip() else "done"
        if r.then == "notify":
            self.surface.event("notify", f"{who}: {first}")
        elif r.then == "tell":
            self.surface.say(first)
        elif r.then == "main":
            self.inbox.put_nowait(Item("inbox", f"Reaction `{r.name}` handled {event.name} by calling {r.call}.\n"
                                                f"Result:\n{result}\n\nThe event:\n{event.text.strip()}"))
        self.react(reactions.Event("reaction.done", {**event.data, "reaction": r.name, "event": event.name,
                                                      "result": result[:2000]}, depth=event.depth + 1))

    def _react_failed(self, r: reactions.Reaction, event: reactions.Event, error: str) -> None:
        if r.on_error == "main":
            about = event.text.strip() or json.dumps(event.data, ensure_ascii=False, default=str)
            self.inbox.put_nowait(Item("inbox", f"Reaction `{r.name}` couldn't handle {event.name}: {error}\n"
                                                f"Handle it yourself if it matters, and fix reactions/{r.name}.md "
                                                f"if the rule is wrong.\n\nThe event:\n{about}"))
        elif r.on_error == "tell":
            self.surface.say(f"Reaction {r.name} failed: {error}")
        else:
            self.surface.event("error", f"reaction {r.name}: {error}")

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
        self.react(reactions.Event(f"routine.{name}", {"routine": name}, body))
        if worker == "main":
            self.inbox.put_nowait(Item("routine", f"{name}\n\n{body.strip()}"))
        elif worker != "none":  # none: only the event, for reactions to act on with no model
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
