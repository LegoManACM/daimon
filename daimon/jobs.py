"""Background jobs: tool calls that keep running while the agent carries on.

A tool call that's still running after `tools.background_after_s` (config.yaml) becomes a job: the model is told
its id at once instead of waiting, and the call keeps going. The agent can also start a job on purpose
(start_job). When a job finishes, its result goes back to whoever started it:

  - still in the same turn (or task): before its next model call, as a message "[Job job3 finished ...]"
  - main, after its turn ended: as a new inbox item, which starts a turn (Agent sets `on_done`)
  - a worker never ends its task with jobs still running: it waits for them first

Calls that wait for the user's OK count too: an `ask` tool that isn't answered within the limit becomes a job, so
an approval waiting on your phone no longer holds up everything else. Jobs live in memory: a restart drops them
(the conversation keeps the "still running" note, so the agent can see one never came back).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .gate import Tool


@dataclass
class Job:
    id: str
    who: str
    tool: str
    args: dict[str, Any]
    task: asyncio.Task
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: str = ""
    delivered: bool = False

    def took(self) -> str:
        s = int((self.finished or time.time()) - self.started)
        return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

    def label(self) -> str:
        args = json.dumps(self.args, ensure_ascii=False)
        return f"{self.id} ({self.tool} {args if len(args) < 120 else args[:119] + '…'})"

    def report(self, limit: int) -> str:
        result = self.result if len(self.result) <= limit else \
            self.result[:limit] + f"\n…[truncated {len(self.result) - limit} chars]"
        return f"[Job {self.label()} finished after {self.took()}]\n{result}"


class Jobs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.count = 0
        self.active: set[str] = set()                     # whose loops are running right now
        self.on_done: Callable[[str], None] | None = None  # called for a finished job whose owner isn't running
        self._changed: asyncio.Event | None = None         # made lazily, inside the running loop

    def _event(self) -> asyncio.Event:
        if self._changed is None:
            self._changed = asyncio.Event()
        return self._changed

    def _notify(self) -> None:
        """Wake everyone waiting; later waiters get a fresh event."""
        ev, self._changed = self._event(), None
        ev.set()

    def adopt(self, who: str, tool: str, args: dict[str, Any], task: asyncio.Task, max_s: float) -> Job:
        """Make a running tool call a job."""
        self.count += 1
        job = Job(f"job{self.count}", who, tool, args, task)
        self.jobs[job.id] = job
        timer = asyncio.get_running_loop().call_later(max_s, task.cancel) if max_s > 0 else None

        def done(t: asyncio.Task) -> None:
            if timer:
                timer.cancel()
            job.finished = time.time()
            if t.cancelled():
                job.result = f"Error: the job was stopped after {job.took()}" + \
                             (" (it ran past tools.job_max_minutes)" if max_s and time.time() - job.started >= max_s
                              else "")
            elif t.exception() is not None:
                job.result = f"Error: {type(t.exception()).__name__}: {t.exception()}"
            else:
                job.result = t.result()
            self._notify()
            if who not in self.active and self.on_done:
                self.on_done(who)

        task.add_done_callback(done)
        return job

    def start(self, who: str, tool: str, args: dict[str, Any], run: Awaitable[str], max_s: float) -> Job:
        return self.adopt(who, tool, args, asyncio.ensure_future(run), max_s)

    def running(self, who: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.who == who and j.finished is None]

    def take(self, who: str, ids: list[str] | None = None) -> list[Job]:
        """Finished jobs not yet handed back (optionally only these ids), marked as handed back."""
        out = [j for j in self.jobs.values() if j.who == who and j.finished is not None and not j.delivered
               and (ids is None or j.id in ids)]
        for j in out:
            j.delivered = True
            self.jobs.pop(j.id, None)
        return out

    def waiting(self, who: str) -> bool:
        return any(j.who == who and j.finished is not None and not j.delivered for j in self.jobs.values())

    async def wait(self, who: str, ids: list[str] | None, timeout: float) -> None:
        """Until one of these jobs (or any of who's) finishes, or the timeout."""
        deadline = time.monotonic() + timeout
        while True:
            ev = self._event()  # taken before looking, so a job finishing in between still wakes us
            mine = [j for j in self.jobs.values() if j.who == who and (ids is None or j.id in ids)]
            finished = [j for j in mine if j.finished is not None]
            if not mine or (finished if ids is None else len(finished) == len(mine)):
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                await asyncio.wait_for(ev.wait(), left)
            except asyncio.TimeoutError:
                return

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.finished is not None:
            return False
        job.task.cancel()
        return True

    def cancel_all(self, who: str | None = None) -> None:
        """Stop jobs (all, or one owner's) and forget them: their results go nowhere."""
        for j in list(self.jobs.values()):
            if who is None or j.who == who:
                j.task.cancel()
                self.jobs.pop(j.id, None)

    def describe(self, who: str) -> str:
        return "\n".join(f"- {j.label()}, running for {j.took()}" for j in self.running(who))


def tools(jobs: Jobs, who: str, available: dict[str, Tool], run: Callable[[str, dict[str, Any]], Awaitable[str]],
          max_s: float, limit: int) -> list[Tool]:
    """start_job, job_wait, job_cancel: for the loop of `who`, over the tools it has this step."""

    async def start_job(args: dict[str, Any]) -> str:
        name, call_args = args["tool"], args.get("args") or {}
        if name not in available or name in ("start_job", "job_wait", "job_cancel"):
            return f"Error: no tool named {name}."
        job = jobs.start(who, name, call_args, run(name, call_args), max_s)
        return (f"Started job {job.id}. Its result will come to you when it's done (in this turn if you're still "
                "working, otherwise as a new message); job_wait waits for it.")

    async def job_wait(args: dict[str, Any]) -> str:
        ids = args.get("ids") or None
        await jobs.wait(who, ids, float(args.get("timeout_s") or 120))
        done = jobs.take(who, ids)
        still = [j.label() for j in jobs.running(who) if ids is None or j.id in ids]
        parts = [j.report(limit) for j in done]
        if still:
            parts.append("Still running: " + "; ".join(still))
        return "\n\n".join(parts) or "No jobs are running."

    async def job_cancel(args: dict[str, Any]) -> str:
        return f"Stopping {args['id']}." if jobs.cancel(args["id"]) else f"No running job {args['id']}."

    auto = lambda _a: "auto"  # noqa: E731 - the tool a job runs passes the consent gate itself
    return [
        Tool("start_job", "Run one of your tools in the background and carry on: you get a job id now and the "
             "result later. For slow calls (a long search, a test run, a local model reading many texts). A call "
             "that runs long becomes a job on its own anyway.",
             {"type": "object", "properties": {
                 "tool": {"type": "string", "description": "the tool's name, as you see it"},
                 "args": {"type": "object", "description": "its arguments"}}, "required": ["tool"]},
             start_job, auto),
        Tool("job_wait", "Wait for background jobs and get their results. Without ids, returns as soon as any "
             "job finishes.",
             {"type": "object", "properties": {
                 "ids": {"type": "array", "items": {"type": "string"}},
                 "timeout_s": {"type": "number", "description": "give up waiting after this (default 120)"}}},
             job_wait, auto),
        Tool("job_cancel", "Stop a background job.",
             {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, job_cancel, auto),
    ]
