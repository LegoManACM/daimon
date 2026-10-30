"""The agent loop: build the prompt from files, call the model, run tool calls through the gate, repeat.

Every step re-reads the agent's files, so a change it makes to itself applies on its very next step. The same
loop runs the main agent's turns and workers' tasks; they differ only in prompt and tools.

What the model sees is built to stay the same from step to step, so providers can cache it:
  1. the system prompt: the agent's files, and runtime facts that change only when something is set up
  2. the conversation, each user message stamped with when it arrived
  3. the turn's context (CONTEXT.md, rendered once per turn: the time, tasks, calendar...) in front of the
     message that started the turn
Tool calls that run long become background jobs (jobs.py); their results come back as messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any, Callable

import litellm

from . import context as turn_context
from . import gate, guide, jobs, knowledge, llm, progress, reactions, selftools
from .harness import describe as harness_list
from .harness import run as harness
from .gate import Surface, Tool
from .home import Home, Worker, expand_secrets, split_frontmatter
from .mcp_hub import McpHub
from .projects import ProjectStore
from .tasks import Task, TaskStore
from .tasktools import WorkerRun, task_thread, worker_tools
from .thread import Thread

litellm.suppress_debug_info = True
litellm.drop_params = True

SELF_EDIT = {
    "policy": "edits to POLICY.md, config.yaml, mcp.json and models.yaml ask the user; other files are auto",
    "free": "all edits to your files are auto",
    "all": "every edit to your files asks the user",
}


class ModelError(Exception):
    pass


class Runner:
    def __init__(self, home: Home, hub: McpHub, surface: Surface, tasks: TaskStore | None = None,
                 main_extra: Callable[[], list[Tool]] | None = None, projects: ProjectStore | None = None):
        self.home, self.hub, self.surface = home, hub, surface
        self.tasks = tasks or TaskStore(home.root / "tasks")
        self.projects = projects or ProjectStore(home.root / "projects")
        self.main_extra = main_extra or (lambda: [])
        self.kb = knowledge.Knowledge(home)
        self.context = turn_context.Renderer(self)
        self.jobs = jobs.Jobs()

    # ---- prompt --------------------------------------------------------------------------------------
    def skills(self, worker: Worker) -> list[str]:
        out = []
        for name in worker.meta.get("skills") or []:
            text = self.home.read(f"skills/{name}/SKILL.md") or self.home.read(f"skills/{name}.md")
            if text:
                out.append(f"# Skill: {name}\n\n{split_frontmatter(text)[1].strip()}")
        return out

    def runtime(self, worker: Worker) -> list[str]:
        """Facts that change only when something is set up. What changes on its own (the time, tasks, spend) is in
        the turn's context instead, so this prompt stays cacheable."""
        mcp = self.hub.status()
        return [
            "# Runtime",
            f"- You are worker `{worker.name}` on model {worker.model}",
            f"- MCP servers: {'; '.join(mcp) if mcp else 'none connected'}",
            "- Messages start with when they arrived, like [Fri Oct 30 14:02]. The <turn-context> block in front of "
            "the message that started this turn has the current time and what's going on (rendered from "
            "CONTEXT.md; guide topic `context`).",
        ]

    async def retrieve(self, text: str) -> str:
        """Knowledge relevant to a message or task, for the prompt. Never fails the turn."""
        limit = int(self.home.config().get("knowledge", {}).get("retrieval_max_chars", 6000))
        try:
            return await self.kb.context_for(text, limit)
        except Exception as e:  # noqa: BLE001 - e.g. the embedding model is down
            self.surface.event("error", f"knowledge retrieval: {e}")
            return ""

    @staticmethod
    def knowledge_section(context: str) -> str:
        return f"# Relevant knowledge (retrieved for this; kg_read for more)\n\n{context}" if context else ""

    def main_prompt(self, worker: Worker, thread: Thread) -> str:
        h, cfg = self.home, self.home.config()
        memory = h.read("MEMORY.md")
        workers = [f"{w.name} ({w.model}" + (f", harness {w.meta['harness']}" if w.meta.get("harness") else "") + ")"
                   for w in self.workers() if w.name != "main"]
        usable, unusable = [], []
        for m in h.models().get("models", []):
            tags = [m.get("tier", "")] + (["approval: ask"] if m.get("approval") == "ask" else [])
            missing = h.missing_keys(m["id"])
            (unusable if missing else usable).append(
                f"{m['id']} ({', '.join(t for t in tags if t)})" + (f" [missing {', '.join(missing)}]" if missing else ""))
        routines = [f"{n} ({m.get('schedule')}{'' if m.get('enabled', True) else ', disabled'})"
                    for n, m in self.routines()]
        found, broken = reactions.load(h)
        runtime = self.runtime(worker) + [
            f"- Home folder: {h.root}",
            f"- Your files: {', '.join(h.files())}",
            f"- self_edit mode: {cfg.get('self_edit', 'policy')} ({SELF_EDIT[cfg.get('self_edit', 'policy')]})",
            f"- MEMORY.md: {len(memory)} of {cfg.get('memory_max_chars', 6000)} chars",
            f"- Usable models: {'; '.join(usable) or 'none'}",
            f"- Listed but not usable yet: {'; '.join(unusable) or 'none'}",
            f"- Knowledge: notes in {self.kb.where()} (kg_search, kg_read, kg_write)",
            f"- Workers: {', '.join(workers) or 'none yet'}",
            f"- Harnesses (external agents; their models run on their own account, never via models.yaml): "
            f"{'; '.join(harness_list(cfg))}",
            f"- Routines: {', '.join(routines) or 'none'}",
            "- Reactions:" + ("".join(f"\n  - {r.describe()}" for r in found) or " none") +
            "".join(f"\n  - BROKEN {e}" for e in broken),
            "- Turn context: " + ("CONTEXT.md" if h.read("CONTEXT.md") else "the built-in default (no CONTEXT.md)"),
        ]
        sections = [h.read("SELF.md"), h.read("POLICY.md"), memory, f"# You are: {worker.name}\n\n{worker.prompt}",
                    *self.skills(worker)]
        if thread.summary:
            sections.append(f"# Earlier in this conversation (summary)\n\n{thread.summary}")
        sections.append(guide.index(h, self.hub))
        sections.append("\n".join(runtime))
        return "\n\n---\n\n".join(s.strip() for s in sections if s.strip())

    def worker_prompt(self, worker: Worker, task: Task) -> str:
        h = self.home
        meta = task.meta
        about = [f"# Your task: {task.title}", f"- id: {task.id}"] + \
                [f"- {k}: {meta[k]}" for k in ("due", "created_by") if meta.get(k)]
        project = self.projects.get(meta["project"]) if meta.get("project") else None
        allowed = worker.meta.get("mcps") or []
        servers = [s for s in self.hub.servers.values() if s.instructions and not s.error
                   and (allowed == "all" or s.name in allowed)]
        addons = "# Your add-ons\n\n" + "\n\n".join(f"## {s.name}\n{s.overview()}" for s in servers) if servers else ""
        sections = [h.read("WORKER.md"), h.read("POLICY.md"), h.read("MEMORY.md"),
                    f"# You are: {worker.name}\n\n{worker.prompt}", *self.skills(worker), addons,
                    "\n".join(about), project.context() if project else "", "\n".join(self.runtime(worker))]
        return "\n\n---\n\n".join(s.strip() for s in sections if s.strip())

    def workers(self) -> list[Worker]:
        out = []
        for p in sorted((self.home.root / "workers").glob("*.md")):
            try:
                out.append(self.home.worker(p.stem))
            except Exception:
                pass
        return out

    def routines(self) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for p in sorted((self.home.root / "routines").glob("*.md")):
            meta, _ = split_frontmatter(p.read_text(encoding="utf-8"))
            out.append((p.stem, meta))
        return out

    # ---- model ---------------------------------------------------------------------------------------
    def model_params(self, model: str) -> dict[str, Any]:
        entry = self.home.model_entry(model)
        if entry is None:
            raise ModelError(f"Model {model} isn't listed in models.yaml.")
        return expand_secrets(entry.get("params") or {})

    def call_settings(self) -> llm.Settings:
        return llm.Settings.from_config(self.home.config())

    def warm(self, model: str) -> None:
        """Start loading a local model before it's needed. Never fails."""
        try:
            llm.warm(model, self.model_params(model), self.call_settings())
        except Exception:  # noqa: BLE001
            pass

    async def complete(self, model: str, messages: list[dict], tools: list[dict] | None, worker: str) -> Any:
        report = progress.reporter(self.surface)
        stream = report is not None and self.home.config().get("stream", True)
        settings = self.call_settings()
        params = self.model_params(model)
        meter = progress.Meter(report, progress.WHO.get(), model) if stream else None
        if meter and llm.is_ollama(model) and await llm.ollama_state(model, params) == "not loaded":
            meter.loading()

        def feed(chunk: Any) -> None:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is not None:
                meter.feed(getattr(delta, "reasoning_content", None) or "", delta.content or "",
                           bool(getattr(delta, "tool_calls", None)))

        def on_retry(attempt: int, why: str) -> None:
            self.surface.event("notify", f"{model}: retrying ({why})")
            if meter:
                meter.retrying(attempt)

        started = time.monotonic()
        try:
            resp = await llm.call(model, messages, tools, params, settings, feed if meter else None, on_retry)
        except Exception as e:
            raise ModelError(await llm.explain(model, params, e, time.monotonic() - started)) from e
        usd = self.record_spend(resp, model, worker)
        if meter:
            meter.done(getattr(resp, "usage", None), usd)
        return resp

    def record_spend(self, resp: Any, model: str, worker: str) -> float:
        usage = getattr(resp, "usage", None)
        try:
            cost = litellm.completion_cost(completion_response=resp) or 0.0
        except Exception:
            cost = 0.0
        price = (self.home.model_entry(model) or {}).get("price")  # {in, out} USD per million tokens
        if not cost and price and usage:
            cost = (usage.prompt_tokens * price.get("in", 0) + usage.completion_tokens * price.get("out", 0)) / 1e6
        self.home.cache.mkdir(exist_ok=True)
        with (self.home.cache / "spend.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "worker": worker, "model": model, "usd": cost,
                                "in": getattr(usage, "prompt_tokens", 0), "out": getattr(usage, "completion_tokens", 0)})
                    + "\n")
        return cost

    def spend_today(self) -> float:
        path = self.home.cache / "spend.jsonl"
        if not path.exists():
            return 0.0
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["ts"] >= start:
                total += row.get("usd") or 0.0
        return total

    # ---- tools ---------------------------------------------------------------------------------------
    async def main_tools(self, worker: Worker) -> dict[str, Tool]:
        await self.hub.sync()
        tools = selftools.build(self.home, self.hub, self.surface) + self.main_extra() + knowledge.tools(self.kb)
        tools += self.hub.tools(worker.meta.get("mcps", []))
        return {t.name: t for t in tools}

    @staticmethod
    def level_hint(tool: Tool) -> str:
        try:
            return tool.level({})
        except Exception:
            return "depends on the change"  # path- or content-dependent levels (self-edits)

    # ---- the loop ------------------------------------------------------------------------------------
    async def loop(self, thread: Thread, load: Callable[[], Worker], tools_for: Callable[[Worker], Any],
                   prompt_for: Callable[[Worker], str], who: str, stop: Callable[[], bool] = lambda: False,
                   context: str = "", start: int | None = None) -> str:
        """Run model steps until the model answers without tool calls. `context` (the rendered CONTEXT.md) goes
        in front of the message at index `start`, the one that started this turn."""
        token = progress.WHO.set(who)
        self.jobs.active.add(who)
        try:
            return await self._loop(thread, load, tools_for, prompt_for, who, stop, context, start)
        finally:
            self.jobs.active.discard(who)
            progress.WHO.reset(token)
            progress.idle(self.surface, who)
            if self.jobs.waiting(who) and self.jobs.on_done:  # finished as the turn ended: don't lose it
                self.jobs.on_done(who)

    def wire(self, thread: Thread, context: str, start: int | None) -> list[dict[str, Any]]:
        """The thread as the provider gets it: each user message stamped with its time, the turn's context in
        front of the message that started it, and no fields of Daimon's own."""
        out = []
        for i, m in enumerate(thread.messages):
            m = dict(m)
            ts = m.pop("ts", None)
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                text = f"{turn_context.stamp(ts)} {m['content']}" if ts else m["content"]
                if i == start and context:
                    text = f"<turn-context>\n{context}\n</turn-context>\n\n{text}"
                m["content"] = text
            out.append(m)
        return out

    def deliver_jobs(self, thread: Thread, who: str) -> None:
        """Finished background jobs go into the conversation before the next model call."""
        done = self.jobs.take(who)
        if done:
            limit = int(self.home.config().get("tool_result_max_chars", 20000))
            thread.append({"role": "user", "ts": time.time(),
                           "content": "\n\n".join(j.report(limit) for j in done)})

    async def _loop(self, thread: Thread, load: Callable[[], Worker], tools_for: Callable[[Worker], Any],
                    prompt_for: Callable[[Worker], str], who: str, stop: Callable[[], bool], context: str,
                    start: int | None) -> str:
        cfg = self.home.config()
        tcfg = cfg.get("tools") or {}
        job_max = float(tcfg.get("job_max_minutes", 30)) * 60
        limit = int(cfg.get("tool_result_max_chars", 20000))
        # main hands late results to the inbox (on_done); everyone else waits for their jobs before finishing
        wait_for_jobs = who != "main" or self.jobs.on_done is None
        reply, retried_empty = "", False
        for _step in range(int(cfg.get("max_steps_per_turn", 30))):
            self.deliver_jobs(thread, who)
            worker = load()  # re-read: the agent may have just changed itself
            tools = await tools_for(worker)

            async def run(name: str, args: dict[str, Any], tools: dict[str, Tool] = tools) -> str:
                return await self.run_tool(tools, name, json.dumps(args), who)
            tools = {**tools, **{t.name: t for t in jobs.tools(self.jobs, who, tools, run, job_max, limit)}}
            specs = [t.spec(self.level_hint(t)) for t in tools.values()]
            messages = [{"role": "system", "content": prompt_for(worker)}, *self.wire(thread, context, start)]
            resp = await self.complete(worker.model, messages, specs, worker.name)
            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls and not (msg.content or "").strip():
                # Some models (small local ones especially) occasionally return only reasoning. Retry once.
                if not retried_empty:
                    retried_empty = True
                    continue
                reply = reply or "(The model returned an empty response.)"
                break
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                                       for c in calls]
            thread.append(entry)
            reply = msg.content or reply
            if not calls:
                if wait_for_jobs and self.jobs.running(who):
                    await self.jobs.wait(who, None, job_max)  # its result needs an answer before this ends
                    continue
                break
            if msg.content and who == "main":
                self.surface.event("thinking", msg.content)
            for c in calls:
                result = await self.run_call(tools, c.function.name, c.function.arguments, who, job_max)
                if len(result) > limit:
                    result = result[:limit] + f"\n…[truncated {len(result) - limit} chars]"
                thread.append({"role": "tool", "tool_call_id": c.id, "content": result})
            if stop():
                break
        else:
            reply = (reply + "\n\n" if reply else "") + "(Stopped: reached max_steps_per_turn.)"
        return reply

    async def run_call(self, tools: dict[str, Tool], name: str, raw_args: str | None, who: str,
                       job_max: float) -> str:
        """A tool call that gets `tools.background_after_s` to finish; after that it carries on as a job and the
        model hears so."""
        budget = float((self.home.config().get("tools") or {}).get("background_after_s", 30))
        call = asyncio.ensure_future(self.run_tool(tools, name, raw_args, who))
        if budget <= 0 or name in ("job_wait", "start_job", "job_cancel"):
            return await call
        done, _ = await asyncio.wait({call}, timeout=budget)
        if done:
            return call.result()
        try:
            args = json.loads(raw_args or "{}") or {}
        except json.JSONDecodeError:
            args = {}
        job = self.jobs.adopt(who, name, args, call, job_max)
        self.surface.event("tool", f"{'' if who == 'main' else f'[{who}] '}{name} → background job {job.id}")
        return (f"Still running after {budget:.0f}s, so it carries on in the background as job {job.id}. Its result "
                "will come to you when it's done: in this turn if you're still working, otherwise as a new message. "
                "Carry on with something else, job_wait to wait for it, or job_cancel to stop it.")

    async def run_tool(self, tools: dict[str, Tool], name: str, raw_args: str | None, who: str) -> str:
        tool = tools.get(name)
        if tool is None:
            return f"Error: no tool named {name}."
        try:
            args = json.loads(raw_args or "{}") or {}
        except json.JSONDecodeError as e:
            return f"Error: arguments weren't valid JSON ({e})."
        self.surface.event("tool", f"{'' if who == 'main' else f'[{who}] '}{name} {gate.short(args)}")
        progress.tool(self.surface, who, name, running=True)
        try:
            return await gate.call(tool, args, self.surface, who)
        finally:
            progress.tool(self.surface, who, name, running=False)

    async def turn(self, thread: Thread, user_text: str) -> str:
        """One message to the main agent (from the user, or an inbox event) and its full response."""
        self.home.commit_user_edits()
        thread.repair()
        main = self.home.worker("main")
        self.warm(main.model)  # loads while the context renders
        thread.append({"role": "user", "content": user_text, "ts": time.time()})
        start = len(thread.messages) - 1
        kcfg = self.home.config().get("knowledge", {})
        about = user_text if kcfg.get("retrieve_for_main", True) else ""
        context = await self.context.render(self.context.source(main.meta), about, await self.main_tools(main),
                                            "main")
        reply = await self.loop(thread, lambda: self.home.worker("main"), self.main_tools,
                                lambda w: self.main_prompt(w, thread), "main", context=context, start=start)
        await self.maybe_compact(thread)
        return reply

    async def run_task(self, task: Task) -> tuple[str, str]:
        """Run (or continue) a worker's task. Returns (status, text): done/result or waiting/question."""
        worker = self.home.worker(task.worker)
        if worker.meta.get("harness") not in (None, "", "native"):
            who = f"{task.worker} · {task.id}"
            progress.tool(self.surface, who, f"{worker.meta['harness']} (external agent)", running=True)
            try:
                return await harness.run_task(self, task, worker, self.projects)
            finally:
                progress.idle(self.surface, who)
        self.warm(worker.model)
        thread = task_thread(self.home, task.id)
        if not thread.messages:
            due = f"\n\nDue: {task.meta['due']}" if task.meta.get("due") else ""
            thread.append({"role": "user", "ts": time.time(),
                           "content": f"Task: {task.title}\n\n{task.sections.get('Brief', '').strip()}{due}"})
        thread.repair()
        run = WorkerRun(task)
        extra = worker_tools(self.tasks, run) + knowledge.tools(self.kb)
        if task.meta.get("project"):
            extra += harness.project_tools(self.projects, self.projects.get(task.meta["project"]))

        async def tools_for(worker: Worker) -> dict[str, Tool]:
            await self.hub.sync()
            return {t.name: t for t in self.hub.tools(worker.meta.get("mcps", [])) + extra}

        who = f"{task.worker} · {task.id}"
        start = max((i for i, m in enumerate(thread.messages) if m.get("role") == "user"), default=None)
        context = await self.context.render(self.context.source(worker.meta, main=False),
                                            f"{task.title}\n{task.sections.get('Brief', '')}",
                                            await tools_for(worker), who)
        try:
            reply = await self.loop(thread, lambda: self.home.worker(task.worker), tools_for,
                                    lambda w: self.worker_prompt(w, task), who, stop=lambda: run.stop,
                                    context=context, start=start)
        finally:
            self.jobs.cancel_all(who)  # a stopped or finished task leaves nothing running
        return ("waiting", run.question) if run.question else ("done", reply)

    # ---- compaction ----------------------------------------------------------------------------------
    async def maybe_compact(self, thread: Thread, force: bool = False) -> bool:
        tcfg = self.home.config().get("thread", {})
        if not force and thread.tokens() < int(tcfg.get("compact_at_tokens", 40000)):
            return False
        model = self.home.worker("main").model

        async def summarize(previous: str, old: list[dict]) -> str:
            transcript = "\n".join(f"{m['role']}: {m.get('content') or json.dumps(m.get('tool_calls'))}"[:2000]
                                   for m in old)
            prompt = ("Update the running summary of a conversation between a user and their personal agent. "
                      "Keep decisions, facts about the user, commitments, open threads and anything the agent "
                      "promised to do. Drop chit-chat. Write concise markdown.\n\n"
                      f"## Current summary\n{previous or '(none)'}\n\n## New messages\n{transcript}")
            resp = await self.complete(model, [{"role": "user", "content": prompt}], None, "compaction")
            return resp.choices[0].message.content or previous

        done = await thread.compact(summarize, int(tcfg.get("keep_recent_messages", 12)))
        if done:
            self.surface.event("compact", "older conversation folded into the summary")
        return done


def provider_hint(model: str) -> str:
    """A short hint when a provider key looks missing, so the error makes sense."""
    keys = {"anthropic/": "ANTHROPIC_API_KEY", "openai/": "OPENAI_API_KEY", "openrouter/": "OPENROUTER_API_KEY",
            "gemini/": "GEMINI_API_KEY"}
    for prefix, key in keys.items():
        if model.startswith(prefix) and not os.environ.get(key):
            return f"{key} isn't set. Add it to your secrets file."
    return ""
