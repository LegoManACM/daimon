"""Guides: the full reference for each thing the agent can set up, read on demand instead of sitting in every prompt.

The prompt carries only an index (one line per topic). `guide(topic)` returns the whole text. Topics are:
  - the built-in guides in daimon/guides/*.md (frontmatter `description` is the index line), shipped with the code
    so they stay true to it;
  - every connected MCP server that describes itself: its instructions, the events it sends, its tools and their
    consent levels;
  - add-ons Daimon knows about that aren't connected yet, with how the user connects them.
"""
from __future__ import annotations

from pathlib import Path

from .home import Home, split_frontmatter
from .mcp_hub import McpHub

FOLDER = Path(__file__).parent / "guides"


def builtin() -> dict[str, tuple[str, str]]:
    out = {}
    for p in sorted(FOLDER.glob("*.md")):
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        out[p.stem] = (str(meta.get("description", "")), body.strip())
    return out


def missing_addons(home: Home) -> dict[str, str]:
    """Add-ons Daimon ships that aren't in mcp.json: name -> what it does."""
    from .setup.addons import ADDONS  # setup pulls in more than the runtime needs; only load it here
    servers = home.mcp().get("mcpServers", {})
    return {a.key: a.blurb for a in ADDONS.values() if a.key not in servers}


def index(home: Home, hub: McpHub) -> str:
    """The prompt section: what each connected add-on is for, what isn't connected, and the guide topics."""
    lines = ["# Add-ons and guides", ""]
    for s in hub.servers.values():
        if s.error or not s.instructions:
            continue
        more = f"Its tools{', and the events it sends' if s.events() else ''}: `guide({s.name!r})`."
        lines += [f"## {s.name}", s.overview(), more, ""]
    missing = missing_addons(home)
    if missing:
        lines.append("Not connected yet (the user connects one with `daimon setup`): " +
                     "; ".join(f"{k} ({v})" for k, v in missing.items()))
        lines.append("")
    lines.append("Guides: before setting up or changing any of these, read the guide with `guide(topic)`. Each has "
                 "the rules and worked examples.")
    lines += [f"- {name}: {desc}" for name, (desc, _) in builtin().items()]
    return "\n".join(lines)


def read(home: Home, hub: McpHub, topic: str) -> str:
    topic = topic.strip().lower()
    guides = builtin()
    if topic in guides:
        return guides[topic][1]
    server = hub.servers.get(topic)
    if server:
        if server.error:
            return f"{topic} is in mcp.json but failed to connect: {server.error}"
        parts = [f"# {topic}", server.overview() or "(it doesn't describe itself)"]
        if server.events():
            parts += ["## Events it sends", "Each arrives as an event you can write a reaction for (guide "
                      "'reactions'); the names and the fields in its data:", server.events()]
        parts += ["## Tools", *(f"- {topic}__{t.name} [{hub.level(topic, t)}]: "
                                f"{(t.description or '').strip().splitlines()[0] if t.description else ''}"
                                for t in server.tools)]
        return "\n\n".join(parts)
    missing = missing_addons(home)
    if topic in missing:
        return (f"{topic} ({missing[topic]}) isn't connected. The user connects it by running `daimon setup` and "
                "picking it; that installs it, signs in to the account and adds it to mcp.json. You can't do the "
                "sign-in for them. Once it's connected, `guide('" + topic + "')` shows its tools and events.")
    known = list(guides) + [s for s in hub.servers] + list(missing)
    return f"No guide called {topic!r}. Topics: {', '.join(known)}."
