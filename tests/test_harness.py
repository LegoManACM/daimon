"""Harness workers with fake drivers: projects across dispatches, session resume, questions, rate limits, limits,
consent and the generic CLI driver."""
import asyncio
import dataclasses
import sys
import textwrap
import time
from pathlib import Path

import pytest

from daimon import agent as agent_mod
from daimon.agent import Agent
from daimon.harness import Driver, Outcome, consent, load
from daimon.harness.run import parse_markers
from daimon.home import Home
from daimon.projects import ProjectStore
from test_agent import Surface, call, script, until


class Fake(Driver):
    """Each run pops the next step: a function (job) -> Outcome, which may call the lent tools."""
    lends_tools = True
    steps: list = []
    jobs: list = []

    async def run(self, job):
        Fake.jobs.append(dataclasses.replace(job))  # as it was for this run (run.py reuses the job on a retry)
        return await Fake.steps.pop(0)(job)


def reply(text, session="s1", **kw):
    async def step(job):
        return Outcome(text, session, **kw)
    return step


def tool(name, **args):
    async def step(job):
        tools = {t.name: t for t in job.tools}
        await tools[name].run(args)
        return None
    return step


def then(*steps):
    async def step(job):
        out = None
        for s in steps:
            out = await s(job) or out
        return out
    return step


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.setattr(agent_mod, "TICK_SECONDS", 0.05)
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    h.write("routines/nightly.md", h.read("routines/nightly.md").replace("enabled: true", "enabled: false"))
    cfg = h.read("config.yaml") + f"\nharness:\n  workspaces: {(tmp_path / 'ws').as_posix()}\n" \
                                  "harnesses:\n  fake:\n    driver: test_harness:Fake\n"
    h.write("config.yaml", cfg)
    h.write("workers/coder.md", "---\ntype: worker\nmodel: anthropic/claude-sonnet-5-5\nharness: fake\n---\nWrite code.\n")
    Fake.steps, Fake.jobs = [], []
    return h


def run_agent(home, body):
    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start()
        try:
            await body(a, s)
        finally:
            await a.stop()
    asyncio.run(go())


def test_project_continues_across_dispatches(home, monkeypatch):
    projects = ProjectStore(home.root / "projects")
    projects.create("site", "Website", "Build the site.", worker="coder")
    script(monkeypatch, {"main": ["[quiet]"] * 4})
    Fake.steps = [
        then(tool("report_progress", note="scaffolded"), tool("update_project_notes", notes="Homepage done; next: about page"),
             reply("Built the homepage.", session="sess-1", cost_usd=0.5)),
        reply("Built the about page.", session="sess-1"),
    ]

    async def body(a, s):
        tools = {t.name: t for t in a.main_extra()}
        await tools["create_task"].run({"title": "Homepage", "brief": "Make the homepage.", "project": "site"})
        await until(lambda: len(a.tasks.all(("done",))) == 1)
        await tools["create_task"].run({"title": "About", "brief": "Make the about page.", "project": "site"})
        await until(lambda: len(a.tasks.all(("done",))) == 2)

    run_agent(home, body)
    first, second = Fake.jobs
    assert first.session is None and "Make the homepage." in first.prompt
    assert "Build the site." in first.system and "first dispatch" in first.system
    assert second.session == "sess-1" and "Make the about page." in second.prompt   # resumed the project's session
    assert "Homepage done; next: about page" in second.system                       # and sees the handoff notes
    assert first.workspace == second.workspace and (first.workspace / ".git").exists()
    p = projects.get("site")
    assert p.session("coder", "fake") == "sess-1"
    assert "Built the homepage." in p.sections["Log"] and "Built the about page." in p.sections["Log"]
    task = [t for t in TaskStoreOf(home).all() if t.title == "Homepage"][0]
    assert "scaffolded" in task.sections["Log"] and "Workspace:" in task.sections["Result"]


def TaskStoreOf(home):
    from daimon.tasks import TaskStore
    return TaskStore(home.root / "tasks")


def test_question_resumes_same_session(home, monkeypatch):
    store = TaskStoreOf(home)
    script(monkeypatch, {"main": [lambda: call("answer_task", task_id=store.all()[0].id, answer="Use blue"),
                                  "[quiet]", "[quiet]"]})
    Fake.steps = [then(tool("ask_main", question="Which colour?"), reply("", session="q-1")),
                  reply("Done in blue.", session="q-1")]

    async def body(a, s):
        store.create("Paint", "Paint the button.", worker="coder")
        a._tick.set()
        await until(lambda: store.all()[0].status == "done")

    run_agent(home, body)
    assert Fake.jobs[1].session == "q-1" and "Answer from the main agent: Use blue" in Fake.jobs[1].prompt
    assert "Done in blue." in store.all()[0].sections["Result"]


def test_lost_session_starts_fresh_with_recap(home, monkeypatch):
    projects = ProjectStore(home.root / "projects")
    projects.create("api", "API", "Build the API.", worker="coder")
    projects.set_session("api", "coder", "fake", "gone")
    script(monkeypatch, {"main": ["[quiet]"]})
    Fake.steps = [reply("", session="gone", error="No conversation found", started=False),
                  reply("Rebuilt.", session="new")]

    async def body(a, s):
        TaskStoreOf(home).create("Next", "Add auth.", worker="coder", project="api")
        a._tick.set()
        await until(lambda: TaskStoreOf(home).all()[0].status == "done")

    run_agent(home, body)
    assert [j.session for j in Fake.jobs] == ["gone", None]
    assert "Add auth." in Fake.jobs[1].prompt
    assert projects.get("api").session("coder", "fake") == "new"


def test_rate_limit_requeues_until_reset(home, monkeypatch):
    script(monkeypatch, {"main": []})
    Fake.steps = [reply("", session="r-1", error="limit", retry_at=time.time() + 3600)]

    async def body(a, s):
        TaskStoreOf(home).create("Big job", "Do it.", worker="coder")
        a._tick.set()
        await until(lambda: "rate-limited" in TaskStoreOf(home).all()[0].sections.get("Log", ""))

    run_agent(home, body)
    t = TaskStoreOf(home).all()[0]
    assert t.status == "queued" and t.meta["run_at"] and t.meta["session"] == "r-1"


def test_max_runs_per_day_holds_tasks(home, monkeypatch):
    home.write("config.yaml", home.read("config.yaml").replace(
        "    claude-code: { max_runs_per_day: 20 }", "    fake: { max_runs_per_day: 1 }"))
    script(monkeypatch, {"main": ["[quiet]"]})
    Fake.steps = [reply("one")]

    async def body(a, s):
        store = TaskStoreOf(home)
        store.create("A", "a", worker="coder")
        store.create("B", "b", worker="coder")
        a._tick.set()
        await until(lambda: any("max_runs_per_day" in t.sections.get("Log", "") for t in store.all()))

    run_agent(home, body)
    assert sorted(t.status for t in TaskStoreOf(home).all()) == ["done", "queued"]


def test_consent_levels(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert consent.level("Bash", {"command": "pytest -q"}, ws) == "auto"
    assert consent.level("Bash", {"command": "cd x && git push origin main"}, ws) == "ask"
    assert consent.level("Edit", {"file_path": str(ws / "a.py")}, ws) == "auto"
    assert consent.level("Write", {"file_path": str(tmp_path / "elsewhere.txt")}, ws) == "ask"
    assert consent.level("mcp__gmail__send", {}, ws) == "ask"
    assert consent.level("mcp__daimon__ask_main", {}, ws) == "auto"
    assert consent.level("Bash", {"command": "git push"}, ws, {"Bash(git push*)": "auto"}) == "auto"


def test_markers():
    text, question, notes = parse_markers("Did half.\nQUESTION: Which DB?\nNOTES:\nSchema drafted; next: migrations")
    assert question == "Which DB?" and notes == "Schema drafted; next: migrations" and "NOTES" not in text


def test_unknown_harness():
    with pytest.raises(ValueError, match="no harness named nope"):
        load("nope", {})


FAKE_CLI = textwrap.dedent("""
    import json, sys
    args = sys.argv[1:]
    if args[0] == "resume":
        print(json.dumps({"session_id": args[1], "result": "resumed: " + args[2][-20:]}))
    else:
        print("noise")
        print(json.dumps({"session_id": "cli-1", "result": "fresh run\\nNOTES:\\nhalf done"}))
""")


def test_cli_driver_with_markers_and_resume(home, monkeypatch, tmp_path):
    agent_cli = tmp_path / "agent_cli.py"
    agent_cli.write_text(FAKE_CLI)
    py = Path(sys.executable).as_posix()
    home.write("config.yaml", home.read("config.yaml") + textwrap.indent(textwrap.dedent(f"""\
        other:
          driver: cli
          command: ["{py}", "{agent_cli.as_posix()}", "new", "{{prompt}}"]
          resume: ["{py}", "{agent_cli.as_posix()}", "resume", "{{session}}", "{{prompt}}"]
          output: jsonl
          result: result
          session: session_id
        """), "  "))
    home.write("workers/codex.md", "---\ntype: worker\nmodel: test/model\nharness: other\n---\nCode.\n")
    projects = ProjectStore(home.root / "projects")
    projects.create("cli", "CLI project", "Try the CLI.", worker="codex")
    script(monkeypatch, {"main": ["[quiet]", "[quiet]"]})

    async def body(a, s):
        store = TaskStoreOf(home)
        store.create("One", "first", worker="codex", project="cli")
        a._tick.set()
        await until(lambda: store.all()[0].status == "done")
        store.create("Two", "second please", worker="codex", project="cli")
        a._tick.set()
        await until(lambda: all(t.status == "done" for t in store.all()))

    run_agent(home, body)
    p = projects.get("cli")
    assert p.sections["Notes"].strip() == "half done" and p.session("codex", "other") == "cli-1"
    results = sorted(t.sections["Result"] for t in TaskStoreOf(home).all())
    assert results[0].startswith("fresh run") and "NOTES" not in results[0]
    assert results[1].startswith("resumed:") and "second please" in results[1]


def test_claude_sdk_options_use_the_subscription(tmp_path, monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from daimon.harness import Job
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-secrets")
    job = Job("t", "coder", "claude-code", "hi", "sys", tmp_path, {}, session="abc", model="claude-sonnet-5-5")
    opts = load("claude-code", {}).build_options(job)
    assert opts.env == {"ANTHROPIC_API_KEY": ""}  # the CLI falls back to your Claude login, not the API key
    assert opts.resume == "abc" and opts.cwd == str(tmp_path) and opts.model == "claude-sonnet-5-5"
    assert opts.system_prompt["append"] == "sys" and "daimon" in opts.mcp_servers
    billed = load("claude-code", {"harnesses": {"claude-code": {"driver": "claude-sdk", "auth": "api_key"}}})
    assert billed.build_options(job).env == {}


def test_harness_workers_use_harness_models_not_models_yaml(home):
    from daimon import selftools
    from daimon.gate import Decision

    class S(Surface):
        async def approve(self, tool, args, can_always, who="main"):
            return Decision(True)
    tools = {t.name: t for t in selftools.build(home, None, S())}
    create = tools["create_worker"]
    args = {"name": "opus-coder", "prompt": "Code.", "reason": "test", "harness": "claude-code", "model": "opus"}
    assert create.level(args) == "auto"   # opus isn't in models.yaml, and doesn't need to be
    asyncio.run(create.run(args))
    assert home.worker("opus-coder").model == "opus"
    for bad in ("openrouter/anthropic/claude-opus-5.5", "anthropic/claude-opus-5-5"):
        with pytest.raises(ValueError, match="models.yaml doesn't apply"):
            create.level({**args, "name": "x", "model": bad})
    asyncio.run(create.run({**args, "name": "default-coder", "model": None}))
    assert home.worker("default-coder").model == "sonnet"  # the harness's default, not default_for_new_workers
    home.write("config.yaml", home.read("config.yaml") + "  claude-code:\n    models: [sonnet, {id: opus, approval: ask}]\n")
    assert create.level({**args, "name": "y"}) == "ask"   # restrict or gate models per harness in config.yaml


def test_repo_with_or_without_git(home, tmp_path):
    from daimon.harness import workspace
    from daimon.harness.workspace import git
    plain = tmp_path / "LifeStuff"
    plain.mkdir()
    assert workspace.prepare(home, "p1", {"repo": str(plain)}) == (plain, None)  # no git: works in it directly
    git(plain, "init", "-q")
    (plain / "a.txt").write_text("a")
    git(plain, "add", "-A")
    git(plain, "commit", "-qm", "init")
    path, branch = workspace.prepare(home, "p2", {"repo": str(plain)})  # after git init: its own worktree
    assert branch == "daimon/p2" and path != plain and (path / "a.txt").exists()
