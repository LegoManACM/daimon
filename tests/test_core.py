"""Model-free tests: home folder, consent levels, self-edit tools, MCP hub, thread repair/compaction."""
import asyncio
import sys
from pathlib import Path

import pytest

from daimon import gate, selftools
from daimon.gate import Decision
from daimon.home import Home
from daimon.mcp_hub import McpHub
from daimon.thread import Thread

DEMO = str(Path(__file__).parent / "demo_server.py")


class FakeSurface:
    def __init__(self, answer=Decision(True)):
        self.answer, self.asked, self.events = answer, [], []

    async def approve(self, tool, args, can_always, who="main"):
        self.asked.append(tool.name)
        return self.answer

    def event(self, kind, text):
        self.events.append((kind, text))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="ollama_chat/test-model")
    return h


def tools_of(home, surface):
    return {t.name: t for t in selftools.build(home, McpHub(home), surface)}


def run(coro):
    return asyncio.run(coro)


def test_init_sets_model_and_keeps_comments(home):
    assert home.worker("main").model == "ollama_chat/test-model"
    assert home.model_entry("ollama_chat/test-model")
    assert "# Models the agent may use" in home.read("models.yaml")
    assert "init: starter files" in home.history()
    assert home.secrets_file.exists() and home.root not in home.secrets_file.parents


def test_edit_self_is_auto_and_committed(home):
    s = FakeSurface()
    t = tools_of(home, s)
    out = run(gate.call(t["edit_agent_file"], {"path": "SELF.md", "old_text": "Be direct and brief.",
                                               "new_text": "Be direct and brief. Never use bullet points.",
                                               "reason": "user dislikes bullets"}, s))
    assert "Saved" in out and not s.asked
    assert "Never use bullet points" in home.read("SELF.md")
    assert "agent: user dislikes bullets" in home.history()


def test_policy_edit_asks_and_decline_is_reported(home):
    s = FakeSurface(Decision(False, note="not now"))
    t = tools_of(home, s)
    out = run(gate.call(t["edit_agent_file"], {"path": "POLICY.md", "old_text": "Reading", "new_text": "Reading",
                                               "reason": "x"}, s))
    assert s.asked == ["edit_agent_file"]
    assert "declined" in out and "not now" in out


def test_self_edit_free_mode(home):
    home.write("config.yaml", home.read("config.yaml").replace("self_edit: policy", "self_edit: free"))
    s = FakeSurface()
    t = tools_of(home, s)
    run(gate.call(t["edit_agent_file"], {"path": "POLICY.md", "old_text": "# Policy", "new_text": "# Rules",
                                         "reason": "rename"}, s))
    assert not s.asked and home.read("POLICY.md").startswith("# Rules")


def test_memory_cap_and_paths(home):
    s = FakeSurface()
    t = tools_of(home, s)
    out = run(gate.call(t["write_agent_file"], {"path": "MEMORY.md", "content": "x" * 7000, "reason": "r"}, s))
    assert "cap" in out and len(home.read("MEMORY.md")) < 7000
    for bad in ("../outside.md", ".git/config", ".cache/spend.jsonl"):
        assert run(gate.call(t["read_agent_file"], {"path": bad}, s)).startswith("Error")


def test_worker_models_are_checked(home, monkeypatch):
    home.write("models.yaml", home.read("models.yaml").replace(
        "models:\n", "models:\n  - id: anthropic/claude-fable-5-1\n    tier: premium\n    approval: ask\n", 1))
    s = FakeSurface()
    t = tools_of(home, s)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = run(gate.call(t["create_worker"], {"name": "coder", "prompt": "Code.", "model": "openai/gpt-5",
                                             "reason": "r"}, s))
    assert "OPENAI_API_KEY" in out and not s.asked  # listed, but no key: refused before asking anyone
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    out = run(gate.call(t["create_worker"], {"name": "coder", "prompt": "Code.", "model": "made/up",
                                             "reason": "r"}, s))
    assert "isn't in models.yaml" in out
    run(gate.call(t["create_worker"], {"name": "deep", "prompt": "Think.", "model": "anthropic/claude-fable-5-1",
                                       "reason": "r"}, s))
    assert s.asked == ["create_worker"]  # premium model asks even though workers/ is normally auto
    run(gate.call(t["create_worker"], {"name": "cheap", "prompt": "Sort.", "reason": "r"}, s))
    assert s.asked == ["create_worker"] and home.worker("cheap").model == "ollama_chat/test-model"


def test_undo(home):
    s = FakeSurface()
    t = tools_of(home, s)
    run(gate.call(t["write_agent_file"], {"path": "MEMORY.md", "content": "# Memory\nLikes tea.\n",
                                          "reason": "tea"}, s))
    ref = home.history(1).split()[0]
    run(gate.call(t["undo_change"], {"change": ref, "reason": "wrong"}, s))
    assert "Likes tea" not in home.read("MEMORY.md")


def test_user_edits_get_own_commit(home):
    home.write("SELF.md", home.read("SELF.md") + "\nBe funny.\n")
    assert home.commit_user_edits()
    assert "user: edited files" in home.history(1)


def test_mcp_hub_levels_and_always(home):
    async def go():
        data = home.mcp()
        data["mcpServers"]["demo"] = {"command": sys.executable, "args": [DEMO]}
        home.write_mcp(data)
        hub = McpHub(home)
        await hub.sync()
        try:
            tools = {t.name: t for t in hub.tools()}
            assert set(tools) == {"demo__get_weather", "demo__send_message"}
            assert tools["demo__get_weather"].level({}) == "auto"
            assert tools["demo__send_message"].level({}) == "ask"
            s = FakeSurface(Decision(True, always=True))
            out = await gate.call(tools["demo__send_message"], {"to": "Sam", "text": "hi"}, s)
            assert out == "Sent to Sam." and s.asked == ["demo__send_message"]
            assert tools["demo__send_message"].level({}) == "auto"
            assert (await gate.call(tools["demo__get_weather"], {"city": "Oslo"}, s)).startswith("Oslo")
        finally:
            await hub.close()
            Path(DEMO).with_name("demo_outbox.txt").unlink(missing_ok=True)
    run(go())


def test_mcp_hub_survives_a_broken_server(home, monkeypatch):
    """A server that dies on start (like `docker mcp` without the plugin) must not take the others down, and
    changing mcp.json afterwards must reconnect the rest instead of leaving the hub stuck. What it printed is
    part of the error, so the agent can see why."""
    monkeypatch.setenv("PROGRAMDATA", "C:/ProgramData")
    monkeypatch.setenv("SOME_API_KEY", "secret")
    broken = ("import os, sys; sys.stderr.write('unknown flag: --profile ' + os.environ.get('PROGRAMDATA', '-') "
              "+ ' ' + os.environ.get('SOME_API_KEY', 'no-secret')); sys.exit(1)")

    async def go():
        data = home.mcp()
        data["mcpServers"] = {"a": {"command": sys.executable, "args": [DEMO]},
                              "broken": {"command": sys.executable, "args": ["-c", broken]},
                              "b": {"command": sys.executable, "args": [DEMO]}}
        home.write_mcp(data)
        hub = McpHub(home)
        try:
            await asyncio.wait_for(hub.sync(), 30)
            assert not hub.servers["a"].error and not hub.servers["b"].error
            assert "It said: unknown flag: --profile C:/ProgramData no-secret" in hub.servers["broken"].error
            del data["mcpServers"]["broken"]
            home.write_mcp(data)
            await asyncio.wait_for(hub.sync(), 30)
            assert hub.status() == ["a: connected, 2 tools", "b: connected, 2 tools"]
            tools = {t.name: t for t in hub.tools()}
            assert (await tools["b__get_weather"].run({"city": "Oslo"})).startswith("Oslo")
            await asyncio.wait_for(hub.sync(), 5)  # and it keeps answering
        finally:
            await asyncio.wait_for(hub.close(), 30)
    run(go())


def test_thread_repair_and_compact(tmp_path):
    th = Thread(tmp_path / "t")
    th.append({"role": "user", "content": "hi"})
    th.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                   "function": {"name": "x", "arguments": "{}"}}]})
    th.repair()
    assert th.messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "(interrupted)"}
    for i in range(10):
        th.append({"role": "user", "content": f"m{i}"})
        th.append({"role": "assistant", "content": f"r{i}"})

    async def summarize(prev, old):
        return f"summary of {len(old)}"

    assert run(th.compact(summarize, keep_recent=4))
    assert th.messages[0]["role"] == "user" and len(th.messages) == 4
    assert Thread(tmp_path / "t").messages == th.messages and "summary of" in th.summary
