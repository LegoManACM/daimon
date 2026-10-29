"""A stand-in calendar add-on for reaction tests: calendar_add_event writes its arguments to the file named on the
command line, one JSON object per line, and upserts by source the way the real one does."""
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("calendar")
LOG = Path(sys.argv[1])


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def calendar_add_event(title: str, start: str, end: str = "", all_day: bool = False, location: str = "",
                       notes: str = "", source: str = "") -> str:
    """Add an event to the user's calendar. source: a key for where it comes from; a repeat updates it."""
    seen = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()] if LOG.exists() else []
    verb = "Updated" if source and any(e.get("source") == source for e in seen) else "Added"
    args = {k: v for k, v in dict(title=title, start=start, end=end, all_day=all_day, location=location,
                                   notes=notes, source=source).items() if v}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
    return f"{verb}: {title} at {start}"


if __name__ == "__main__":
    server.run("stdio")
