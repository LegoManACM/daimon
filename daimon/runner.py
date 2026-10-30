"""The agent loop: build the prompt from files, call the model, run tool calls through the gate, repeat.

Every step re-reads the agent's files, so a change it makes to itself applies on its very next step. The same
loop runs the main agent's turns and workers' tasks; they differ only in prompt and tools.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Callable

import litellm

from . import gate, guide, knowledge, progress, reactions, selftools
from .harness import describe as harness_list
from .harness import run as harness
from .gate import Surface, Tool
from .home import Home, Worker, expand_secrets, split_frontmatter
from .mcp_hub import McpHub
from .projects import ProjectStore
from .projects import summary as project_summary
from .tasks import OPEN, Task, TaskStore, summary
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

    # ---- prompt --------------------------------------------------------------------------------------
    def skills(self, worker: Worker) -> list[str]:
        out = []
        for name in worker.meta.get("skills") or []:
            text = self.home.read(f"skills/{name}/SKILL.md") or self.home.read(f"skills/{name}.md")
            if text:
                out.append(f"# Skill: {name}\n\n{split_frontmatter(text)[1].strip()}")
        return out

    def runtime(self, worker: Worker) -> list[str]:
        mcp = self.hub.status()
        return [
            "# Runtime",
            f"- Now: {datetime.now().astimezone().strftime('%A %Y-%m-%d %H:%M %Z')}",
            f"- You are worker `{worker.name}` on model {worker.model}",
            f"- MCP servers: {'; '.join(mcp) if mcp else 'none connected'}",
            f"- Spend today: ${self.spend_today():.4f}",
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

    def main_prompt(self, worker: Worker, thread: Thread, context: str = "") -> str:
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
            f"- Knowledge: {self.kb.count()} notes in {self.kb.where()} (kg_search, kg_read, kg_write)",
            f"- Workers: {', '.join(workers) or 'none yet'}",
            f"- Harnesses (external agents; their models run on their own account, never via models.yaml): "
            f"{'; '.join(harness_list(cfg))}",
            f"- Routines: {', '.join(routines) or 'none'}",
            "- Reactions:" + ("".join(f"\n  - {r.describe()}" for r in found) or " none") +
            "".join(f"\n  - BROKEN {e}" for e in broken),
            "- Open tasks:\n" + "\n".join(f"  - {line}" for line in summary(self.tasks.all(OPEN)).splitlines()),
            "- Active projects:\n" + "\n".join(
                f"  - {line}" for line in project_summary(self.projects.all(("active",))).splitlines()),
        ]
        sections = [h.read("SELF.md"), h.read("POLICY.md"), memory, f"# You are: {worker.name}\n\n{worker.prompt}",
                    *self.skills(worker)]
        if thread.summary:
            sections.append(f"# Earlier in this conversation (summary)\n\n{thread.summary}")
        sections.append(self.knowledge_section(context))
        sections.append(guide.index(h, self.hub))
        sections.append("\n".join(runtime))
        return "\n\n---\n\n".join(s.strip() for s in sections if s.strip())

    def worker_prompt(self, worker: Worker, task: Task, context: str = "") -> str:
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
                    "\n".join(about), project.context() if project else "", self.knowledge_section(context),
                    "\n".join(self.runtime(worker))]
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

    async def complete(self, model: str, messages: list[dict], tools: list[dict] | None, worker: str) -> Any:
        report = progress.reporter(self.surface)
        stream = report is not None and self.home.config().get("stream", True)
        meter = progress.Meter(report, progress.WHO.get(), model) if stream else None
        try:
            params = {"num_retries": 2, **self.model_params(model)}
            if meter:
                resp = await self.streamed(model, messages, tools, params, meter)
            else:
                resp = await litellm.acompletion(model=model, messages=messages, tools=tools or None, **params)
        except ModelError:
            raise
        except Exception as e:
            raise ModelError(f"{type(e).__name__}: {e}") from e
        usd = self.record_spend(resp, model, worker)
        if meter:
            meter.done(getattr(resp, "usage", None), usd)
        return resp

    @staticmethod
    async def streamed(model: str, messages: list[dict], tools: list[dict] | None, params: dict,
                       meter: progress.Meter) -> Any:
        """A streamed call, fed to the meter as it arrives, rebuilt into the same response a plain call returns."""
        chunks = []
        stream = await litellm.acompletion(model=model, messages=messages, tools=tools or None, stream=True,
                                           stream_options={"include_usage": True}, **params)
        async for chunk in stream:
            chunks.append(chunk)
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is not None:
                meter.feed(getattr(delta, "reasoning_content", None) or "", delta.content or "",
                           bool(getattr(delta, "tool_calls", None)))
        resp = litellm.stream_chunk_builder(chunks, messages=messages) if chunks else None
        if resp is None:
            raise ModelError("the model's stream ended without a response")
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
                   prompt_for: Callable[[Worker], str], who: str, stop: Callable[[], bool] = lambda: False) -> str:
        token = progress.WHO.set(who)
        try:
            return await self._loop(thread, load, tools_for, prompt_for, who, stop)
        finally:
            progress.WHO.reset(token)
            progress.idle(self.surface, who)

    async def _loop(self, thread: Thread, load: Callable[[], Worker], tools_for: Callable[[Worker], Any],
                    prompt_for: Callable[[Worker], str], who: str, stop: Callable[[], bool]) -> str:
        cfg = self.home.config()
        reply, retried_empty = "", False
        for _step in range(int(cfg.get("max_steps_per_turn", 30))):
            worker = load()  # re-read: the agent may have just changed itself
            tools = await tools_for(worker)
            specs = [t.spec(self.level_hint(t)) for t in tools.values()]
            messages = [{"role": "system", "content": prompt_for(worker)}, *thread.messages]
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
                break
            if msg.content and who == "main":
                self.surface.event("thinking", msg.content)
            for c in calls:
                result = await self.run_tool(tools, c.function.name, c.function.arguments, who)
                limit = int(cfg.get("tool_result_max_chars", 20000))
                if len(result) > limit:
                    result = result[:limit] + f"\n…[truncated {len(result) - limit} chars]"
                thread.append({"role": "tool", "tool_call_id": c.id, "content": result})
            if stop():
                break
        else:
            reply = (reply + "\n\n" if reply else "") + "(Stopped: reached max_steps_per_turn.)"
        return reply

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
        thread.append({"role": "user", "content": user_text})
        kcfg = self.home.config().get("knowledge", {})
        context = await self.retrieve(user_text) if kcfg.get("retrieve_for_main", True) else ""
        reply = await self.loop(thread, lambda: self.home.worker("main"), self.main_tools,
                                lambda w: self.main_prompt(w, thread, context), "main")
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
        thread = task_thread(self.home, task.id)
        if not thread.messages:
            due = f"\n\nDue: {task.meta['due']}" if task.meta.get("due") else ""
            thread.append({"role": "user", "content": f"Task: {task.title}\n\n{task.sections.get('Brief', '').strip()}{due}"})
        thread.repair()
        run = WorkerRun(task)
        extra = worker_tools(self.tasks, run) + knowledge.tools(self.kb)
        if task.meta.get("project"):
            extra += harness.project_tools(self.projects, self.projects.get(task.meta["project"]))
        context = await self.retrieve(f"{task.title}\n{task.sections.get('Brief', '')}")

        async def tools_for(worker: Worker) -> dict[str, Tool]:
            await self.hub.sync()
            return {t.name: t for t in self.hub.tools(worker.meta.get("mcps", [])) + extra}

        who = f"{task.worker} · {task.id}"
        reply = await self.loop(thread, lambda: self.home.worker(task.worker), tools_for,
                                lambda w: self.worker_prompt(w, task, context), who, stop=lambda: run.stop)
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
