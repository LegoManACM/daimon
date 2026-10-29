"""Guides: the on-demand reference, the add-on overviews in the prompt, and that the guides' examples are valid."""
import re
from types import SimpleNamespace as NS

import pytest
import yaml

from daimon import guide, reactions
from daimon.home import Home
from daimon.mcp_hub import McpHub, Server

INSTRUCTIONS = """Texts, stored locally. Set watchers when useful.

Events:
- demo.ping: something happened. who, when."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DAIMON_SECRETS", str(tmp_path / "secrets.env"))
    h = Home(tmp_path / "home")
    h.init(model="test/model")
    data = h.mcp()
    data["mcpServers"]["demo"] = {"command": "x"}
    h.write_mcp(data)
    return h


def hub_with(home):
    hub = McpHub(home)
    tool = NS(name="ping", description="Ping someone.\nMore.", annotations=None)
    hub.servers["demo"] = Server("demo", tools=[tool], instructions=INSTRUCTIONS)
    return hub


def test_every_guide_has_an_index_line():
    found = guide.builtin()
    assert {"reactions", "consent", "models", "workers", "routines", "connections", "config", "files"} <= set(found)
    for name, (desc, body) in found.items():
        assert desc and body.startswith("# "), name


def test_prompt_shows_overviews_not_event_lists(home):
    text = guide.index(home, hub_with(home))
    assert "## demo\nTexts, stored locally. Set watchers when useful." in text
    assert "demo.ping" not in text and "events it sends: `guide('demo')`" in text  # events stay out of the prompt
    assert "Not connected yet" in text and "canvas" in text and "calendar" in text
    assert "- consent: " in text


def test_guide_reads_topics_and_add_ons(home):
    hub = hub_with(home)
    assert guide.read(home, hub, "Consent").startswith("# Consent")
    demo = guide.read(home, hub, "demo")
    assert "demo.ping: something happened" in demo and "- demo__ping [ask]: Ping someone." in demo
    assert "daimon config addons" in guide.read(home, hub, "canvas")
    assert "Topics:" in guide.read(home, hub, "nope")


def test_reaction_examples_in_the_guide_are_valid():
    """Every reaction example in guides/reactions.md must parse and fill, so the guide can't drift from the code."""
    text = (guide.FOLDER / "reactions.md").read_text(encoding="utf-8")
    blocks = [b for b in re.findall(r"```markdown\n(.*?)```", text, re.S) if b.startswith("---\non:")]
    assert len(blocks) >= 5
    for b in blocks:
        r = reactions.parse("example", b)
        sample = {k: "x" for k in re.findall(r"\{([A-Za-z_][\w]*)", yaml.safe_dump(r.args))}
        sample.update(message_ids=["m1"], reaction="plans-to-calendar")
        assert isinstance(r.render(sample), dict)


def test_filters():
    r = reactions.parse("f", "---\non: a\ncall: t\nargs: {p: 'grades/{id|slug}.md', d: '{at|date}', t: '{at|time}', "
                             "w: '{who|join}', o: '{gone?|upper}'}\n---\n")
    got = r.render({"id": "assignment:9:1", "at": "2026-10-30T19:00", "who": ["Sam", "Ana"]})
    assert got == {"p": "grades/assignment-9-1.md", "d": "2026-10-30", "t": "19:00", "w": "Sam, Ana"}
    with pytest.raises(ValueError, match="no filter"):
        reactions.parse("g", "---\non: a\ncall: t\nargs: {x: '{a|nope}'}\n---\n").render({"a": 1})


def test_canvas_and_messages_describe_their_events():
    pytest.importorskip("daimon_canvas")
    from daimon_canvas import mcp_server as canvas
    from daimon_messages import mcp_server as messages
    for text in (f"{canvas.INSTRUCTIONS}\n\n{canvas.EVENTS}", messages.INSTRUCTIONS):
        s = Server("x", instructions=text)
        assert s.overview() and re.search(r"^- (canvas|messages)\.\w+:", s.events(), re.M)
