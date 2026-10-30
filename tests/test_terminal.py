"""Streaming model calls with live progress, and the terminal that shows it."""
import asyncio
import time

import pytest

from daimon import progress
from daimon.gateway import Prompt
from daimon.home import Home
from daimon.mcp_hub import McpHub
from daimon.runner import Runner
from daimon.terminal import Terminal, stats

from test_agent import Surface


class Watching(Surface):
    def __init__(self):
        super().__init__()
        self.states = []

    def progress(self, who, state):
        self.states.append((who, dict(state)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    h.write("models.yaml", "models:\n  - id: openai/test-model\n    params: {mock_response: 'Hello there, how can I help "
                           "you today with your plans?'}\n")
    return h


def test_streamed_call_reports_progress_and_returns_the_whole_reply(home):
    s = Watching()
    runner = Runner(home, McpHub(home), s)

    async def go():
        token = progress.WHO.set("researcher · 20261029-1234-abc")
        try:
            return await runner.complete("openai/test-model", [{"role": "user", "content": "hi"}], None, "researcher")
        finally:
            progress.WHO.reset(token)
    resp = asyncio.run(go())
    assert resp.choices[0].message.content == "Hello there, how can I help you today with your plans?"
    phases = [st["phase"] for who, st in s.states]
    assert phases[0] == "waiting" and "writing" in phases and phases[-1] == "done"
    assert {who for who, _ in s.states} == {"researcher · 20261029-1234-abc"}
    done = s.states[-1][1]
    assert done["tokens"] > 0 and done["seconds"] >= 0 and "Hello" in done["tail"]
    assert (home.cache / "spend.jsonl").exists()  # still recorded


def test_surfaces_without_progress_get_a_plain_call(home):
    s = Surface()  # no progress method: nothing streams
    resp = asyncio.run(Runner(home, McpHub(home), s).complete(
        "openai/test-model", [{"role": "user", "content": "hi"}], None, "main"))
    assert resp.choices[0].message.content.startswith("Hello there")


def test_status_bar_shows_everything_in_flight():
    t = Terminal(interactive=True)
    t.summary = lambda: "2 open tasks  ·  $0.12 today"
    now = time.monotonic()
    t.progress("main", {"phase": "writing", "model": "anthropic/claude-sonnet-5-5", "started": now - 3,
                        "tokens": 120, "tps": 41.7, "tail": "Sure, here's your week: Monday you have"})
    t.progress("researcher · 20261029-142233-xyzabc", {"phase": "tool", "tool": "canvas__canvas_assignments",
                                                       "started": now - 2})
    t.progress("reaction:plans", {"phase": "waiting", "model": "", "started": now})
    lines = [text for _, text in t.status_lines(width=200)]
    assert "main" in lines[0] and "sonnet-5-5" in lines[0] and "42 tok/s" in lines[0] and "Monday you have" in lines[0]
    assert "researcher ·xyzabc" in lines[1] and "running canvas__canvas_assignments" in lines[1]
    assert "reaction:plans" in lines[2]
    assert "● working" in lines[-1] and "2 working in the background" in lines[-1] and "$0.12 today" in lines[-1]

    t.progress("main", {"phase": "done", "model": "anthropic/claude-sonnet-5-5", "tokens": 130, "tps": 40.0,
                        "seconds": 3.4, "ttft": 0.6, "usd": 0.0021})
    t.progress("researcher · 20261029-142233-xyzabc", {"phase": "idle"})
    t.progress("reaction:plans", {"phase": "idle"})
    assert [text for _, text in t.status_lines(width=200)][0].startswith("● ready")
    assert stats(t.turn) == "claude-sonnet-5-5 · 130 tokens · 40 tok/s · first token 0.6s · 3.4s · $0.0021"


def test_status_bar_shows_waiting_approvals():
    async def go():
        t = Terminal(interactive=True)
        asking = asyncio.create_task(t.prompt(Prompt("calendar__calendar_add_event", {"title": "Dinner"},
                                                     [("y", "Yes"), ("n", "No")], "reaction:plans")))
        await asyncio.sleep(0)
        lines = [text for _, text in t.status_lines(width=200)]
        t.answer_oldest("y")
        return lines, await asking
    lines, answer = asyncio.run(go())
    assert any("waiting for your answer: calendar__calendar_add_event (reaction:plans)" in line for line in lines)
    assert answer == "y"


def test_reply_renders_markdown_with_stats(capsys):
    t = Terminal(interactive=True)
    t.turn = {"model": "openai/gpt-5", "tokens": 12, "tps": 30.0, "seconds": 0.9, "ttft": 0.3}
    asyncio.run(t.send("**Done.** Added *Dinner with Sam* to Friday."))
    out = capsys.readouterr().out
    assert "◆ daimon" in out and "Done." in out and "**" not in out
    assert "gpt-5 · 12 tokens · 30 tok/s" in out and t.turn == {}
