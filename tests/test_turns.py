"""Turn context (CONTEXT.md), message times, a cacheable system prompt, background jobs and slow model calls."""
import asyncio
from types import SimpleNamespace as NS

import litellm
import pytest

from daimon import llm
from daimon.agent import Agent
from daimon.context import tool_args
from daimon.gate import Tool
from daimon.home import Home
from daimon.mcp_hub import McpHub
from daimon.runner import ModelError, Runner
from daimon.thread import Thread

from test_agent import Surface, call, script, until


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    h.write("routines/nightly.md", h.read("routines/nightly.md").replace("enabled: true", "enabled: false"))
    return h


def run(coro):
    return asyncio.run(coro)


def tool(name, result="", level="auto", delay=0.0, calls=None):
    async def go(args):
        if calls is not None:
            calls.append(args)
        await asyncio.sleep(delay)
        return result or f"{name} done"
    return Tool(name, f"{name} tool", {"type": "object", "properties": {}}, go, lambda _a: level)


def set_config(home, **sections):
    cfg = home.config()
    for key, value in sections.items():
        cfg[key] = {**(cfg.get(key) or {}), **value} if isinstance(value, dict) else value
    home.write_yaml("config.yaml", cfg)


# ---- CONTEXT.md --------------------------------------------------------------------------------------------

def test_context_tags_render_and_empty_sections_drop(home):
    r = Runner(home, McpHub(home), Surface())
    calls = []
    tools = {"cal__events": tool("cal__events", "- 10:00 Dentist", calls=calls),
             "cal__add": tool("cal__add", level="ask")}
    home.write("notes/today.md", "Gym at 6.")
    text = ("<!-- a comment the agent never sees -->\n{now}\n\n## Calendar\n"
            "{tool:cal__events start=today days=1 | every=1h}\n\n## Nothing here\n{jobs}\n\n## Notes\n"
            "{file:notes/today.md}\n\n## Risky\n{tool:cal__add title=x}\n\n## Broken\n{file:../../outside.md}\n")

    out = run(r.context.render(text, "", tools))
    assert "a comment" not in out and out.startswith("Now: ")
    assert "## Calendar\n- 10:00 Dentist" in out and calls == [{"start": "today", "days": 1}]
    assert "Nothing here" not in out  # heading with an empty tag under it is dropped
    assert "Gym at 6." in out
    assert "isn't an auto tool" in out  # tags never run tools that ask or change things
    assert "outside the agent's home folder" in out  # nor read outside the home folder

    run(r.context.render(text, "", tools))
    assert len(calls) == 1  # every=1h reused the result


def test_context_keeps_headings_that_come_from_a_tag(home):
    r = Runner(home, McpHub(home), Surface())
    tools = {"kb": tool("kb", "## people/sam.md\nSam's birthday is 2026-11-14.")}
    out = run(r.context.render("## Relevant knowledge\n{tool:kb}\n\n## Empty\n{jobs}", "", tools))
    assert out == "## Relevant knowledge\n## people/sam.md\nSam's birthday is 2026-11-14."


def test_tool_tag_arguments():
    assert tool_args('messages__search query="dinner plans" since=2d limit=5 only=true') == (
        "messages__search", {"query": "dinner plans", "since": "2d", "limit": 5, "only": True})


def test_a_failing_tag_never_stops_the_turn(home):
    r = Runner(home, McpHub(home), Surface())

    async def broken(_args):
        raise RuntimeError("calendar offline")
    tools = {"cal": Tool("cal", "", {}, broken, lambda _a: "auto")}
    out = run(r.context.render("{now}\n{tool:cal}\n{nosuchtag}\n{tool:missing}", "", tools))
    assert "calendar offline" in out and "unknown tag" in out and "no tool named missing" in out


def test_old_homes_get_context_md(home):
    (home.root / "CONTEXT.md").unlink()
    assert home.add_missing("CONTEXT.md") and "{now}" in home.read("CONTEXT.md")
    assert not home.add_missing("CONTEXT.md")


# ---- what the model sees ----------------------------------------------------------------------------------

def test_system_prompt_stays_the_same_and_context_rides_on_the_message(home, monkeypatch):
    home.write("CONTEXT.md", "{now}\n\n## Calendar\n{tool:cal}\n")
    model = script(monkeypatch, {"main": [call("cal"), "Dentist at 10.", "Nothing else."]})
    r = Runner(home, McpHub(home), Surface(), main_extra=lambda: [tool("cal", "- 10:00 Dentist")])
    thread = Thread(home.root / "threads" / "main")

    async def go():
        await r.turn(thread, "What's on today?")
        await r.turn(thread, "Anything else?")
    run(go())
    steps = model.seen["main"]
    assert len({s[0]["content"] for s in steps}) == 1  # one system prompt across steps and turns: cacheable
    assert "Now:" not in steps[0][0]["content"]
    first = steps[0][1]["content"]
    assert first.startswith("<turn-context>\nNow: ") and "- 10:00 Dentist" in first and "] What's on today?" in first
    # a later turn: the context moved to its own message, the earlier message is back to just its text and time
    later = steps[2]
    assert later[1]["content"].startswith("[") and "<turn-context>" not in later[1]["content"]
    assert "<turn-context>" in later[-1]["content"] and later[-1]["content"].endswith("Anything else?")
    assert all("ts" not in m for m in later)  # Daimon's own fields never reach the provider
    assert "ts" in thread.messages[0]


# ---- background jobs -------------------------------------------------------------------------------------

def test_slow_call_becomes_a_job_and_job_wait_gets_it(home, monkeypatch):
    set_config(home, tools={"background_after_s": 0.2})
    script(monkeypatch, {"main": [call("slow"), call("job_wait"), "All done."]})
    r = Runner(home, McpHub(home), Surface(), main_extra=lambda: [tool("slow", "found 3 plans", delay=0.6)])
    thread = Thread(home.root / "threads" / "main")
    reply = run(r.turn(thread, "Test the watcher."))
    assert reply == "All done."
    results = [m["content"] for m in thread.messages if m["role"] == "tool"]
    assert "background as job job1" in results[0]
    assert "[Job job1 (slow {})" in results[1] and "found 3 plans" in results[1]
    assert not r.jobs.jobs


def test_a_finished_job_comes_back_in_the_same_turn(home, monkeypatch):
    set_config(home, tools={"background_after_s": 0.2})
    model = script(monkeypatch, {"main": [call("slow"), "Started it.", "It found 3 plans."]})
    r = Runner(home, McpHub(home), Surface(), main_extra=lambda: [tool("slow", "found 3 plans", delay=0.5)])
    thread = Thread(home.root / "threads" / "main")
    # No inbox to hand it to (on_done unset), so the turn waits for its job and carries on with the result.
    assert run(r.turn(thread, "Test the watcher.")) == "It found 3 plans."
    last = model.seen["main"][-1][-1]["content"]
    assert "[Job job1 (slow {}) finished after" in last and "found 3 plans" in last


def test_main_hears_about_a_job_that_finishes_after_its_turn(home, monkeypatch):
    set_config(home, tools={"background_after_s": 0.2})
    model = script(monkeypatch, {"main": [call("slow"), "Testing it; I'll tell you.", "The test found 3 plans."]})
    slow = tool("slow", "found 3 plans", delay=0.6)
    original = Agent.main_extra
    monkeypatch.setattr(Agent, "main_extra", lambda self: original(self) + [slow])

    async def go():
        s = Surface()
        a = Agent(home, s)
        await a.start(scheduler=False)
        try:
            assert await a.send("Test the plans watcher.") == "Testing it; I'll tell you."
            await until(lambda: s.said)
            assert s.said == ["The test found 3 plans."]
            last = model.seen["main"][-1][-1]["content"]
            assert "[Background job finished]" in last and "found 3 plans" in last
        finally:
            await a.stop()
    run(go())


def test_an_unanswered_approval_becomes_a_job(home, monkeypatch):
    set_config(home, tools={"background_after_s": 0.2})
    class Slow(Surface):
        async def approve(self, tool, args, can_always, who="main"):
            await asyncio.sleep(0.6)  # the user answers on their phone a bit later
            return await super().approve(tool, args, can_always, who)

    script(monkeypatch, {"main": [call("add_event"), "Waiting for your OK.", "Added."]})
    r = Runner(home, McpHub(home), Slow(), main_extra=lambda: [tool("add_event", "added", level="ask")])
    thread = Thread(home.root / "threads" / "main")
    assert run(r.turn(thread, "Add dinner Friday.")) == "Added."
    assert "background as job job1" in [m["content"] for m in thread.messages if m["role"] == "tool"][0]


def test_job_tools_start_and_cancel(home, monkeypatch):
    script(monkeypatch, {"main": [call("start_job", tool="slow", args={"q": 1}), call("job_cancel", id="job1"),
                                          call("job_wait"), "Stopped it."]})
    r = Runner(home, McpHub(home), Surface(), main_extra=lambda: [tool("slow", delay=30)])
    thread = Thread(home.root / "threads" / "main")
    assert run(r.turn(thread, "Go.")) == "Stopped it."
    results = [m["content"] for m in thread.messages if m["role"] == "tool"]
    assert results[0].startswith("Started job job1") and results[1] == "Stopping job1."
    assert "job was stopped" in results[2]


# ---- slow models -----------------------------------------------------------------------------------------

def test_ollama_calls_keep_the_model_loaded_with_a_fixed_window():
    s = llm.Settings.from_config({"model_calls": {"ollama": {"keep_alive": -1}}})
    assert llm.prepare("ollama_chat/qwen3.5:9b", {}, s) == {"keep_alive": -1, "num_ctx": 16384}
    assert llm.prepare("ollama_chat/qwen3.5:9b", {"num_ctx": 4096}, s)["num_ctx"] == 4096  # the model's own wins
    assert llm.prepare("anthropic/claude-haiku-5-5", {}, s) == {}


def test_a_model_that_never_starts_answering_times_out_and_is_retried(monkeypatch):
    attempts = []

    async def stuck(**kwargs):
        attempts.append(kwargs)
        await asyncio.Event().wait()  # never answers
    monkeypatch.setattr(litellm, "acompletion", stuck)
    monkeypatch.setattr(asyncio, "sleep", _fast_backoff(asyncio.sleep))  # skip the backoff between attempts
    s = llm.Settings(first_token_timeout_s=0.2, retries=1)
    with pytest.raises(llm.ModelTimeout, match="didn't start answering within"):
        run(llm.call("openai/x", [{"role": "user", "content": "hi"}], None, {"num_retries": 5}, s, feed=lambda c: None))
    assert len(attempts) == 2 and all(a["num_retries"] == 0 for a in attempts)  # our retries, not LiteLLM's


def test_a_stream_that_goes_silent_times_out(monkeypatch):
    class Stalls:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not hasattr(self, "sent"):
                self.sent = True
                return NS(choices=[NS(delta=NS(content="Hel"))])
            await asyncio.sleep(10)

    async def stalls(**kwargs):
        return Stalls()
    monkeypatch.setattr(litellm, "acompletion", stalls)
    s = llm.Settings(stall_timeout_s=0.2, retries=0)
    with pytest.raises(llm.ModelTimeout, match="went silent"):
        run(llm.call("openai/x", [{"role": "user", "content": "hi"}], None, {}, s, feed=lambda c: None))


def test_a_failed_local_model_call_says_what_is_wrong(home, monkeypatch):
    home.write("models.yaml", "models:\n  - id: ollama_chat/tiny\n    params: {api_base: 'http://127.0.0.1:9'}\n")
    set_config(home, model_calls={"first_token_timeout_s": 2, "retries": 0, "ollama": {"warm_up": False}})
    r = Runner(home, McpHub(home), Surface())
    with pytest.raises(ModelError, match="Ollama isn't answering at http://127.0.0.1:9"):
        run(r.complete("ollama_chat/tiny", [{"role": "user", "content": "hi"}], None, "main"))


def test_claude_prompts_are_marked_for_caching(monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None))])
    monkeypatch.setattr(litellm, "acompletion", fake)
    run(llm.call("anthropic/claude-haiku-5-5", [{"role": "user", "content": "hi"}], None, {}, llm.Settings()))
    assert {"location": "message", "role": "system"} in seen["cache_control_injection_points"]
    seen.clear()
    run(llm.call("openai/gpt-5", [{"role": "user", "content": "hi"}], None, {}, llm.Settings()))
    assert "cache_control_injection_points" not in seen


def test_watchers_judge_through_the_same_calls(tmp_path):
    from daimon_messages.judge import ask
    verdict = run(ask("openai/test", {"mock_response": '{"match": true, "evidence": ["m1"], "summary": "Dinner Fri"}'},
                      [{"role": "user", "content": "texts"}], home=tmp_path))
    assert verdict["match"] and verdict["summary"] == "Dinner Fri"


def _fast_backoff(real_sleep):
    async def sleep(seconds, *a, **k):
        return await real_sleep(min(seconds, 0.01) if seconds >= 1 else seconds, *a, **k)
    return sleep
