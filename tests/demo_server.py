"""A tiny MCP server for trying the agent: one read-only tool and one 'real-world' action."""
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("demo")
OUTBOX = Path(__file__).parent / "demo_outbox.txt"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_weather(city: str) -> str:
    """Get today's weather for a city."""
    return f"{city}: 18°C, light rain until noon, then clearing."


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
def send_message(to: str, text: str) -> str:
    """Send a text message to someone."""
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(f"to {to}: {text}\n")
    return f"Sent to {to}."


if __name__ == "__main__":
    server.run("stdio")
