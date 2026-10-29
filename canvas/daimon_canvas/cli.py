"""daimon-canvas: the Canvas MCP server, and a few commands for setting it up."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from .client import CanvasClient
from .config import Home, resolve_home

def check(home: Home, url: str, token: str) -> str:
    """The student's name if the url + token work; raises CanvasError otherwise."""
    async def go() -> str:
        client = CanvasClient(url, token, home.config().tz())
        try:
            me = await client.me()
            courses = await client.courses()
        finally:
            await client.http.aclose()
        return f"{me.get('name', '?')}, {len(courses)} active classes"
    return asyncio.run(go())


def login(home: Home, url: str | None) -> int:
    url = (url or input("Your Canvas address (like https://school.instructure.com): ")).strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    print("Make a token in Canvas: Account → Settings → + New access token. Paste it here (it isn't shown).")
    token = getpass.getpass("Token: ").strip()
    if not token:
        print("No token given.")
        return 1
    try:
        who = check(home, url, token)
    except Exception as e:  # noqa: BLE001
        print(f"That didn't work: {e}")
        return 1
    home.save_login(url, token)
    print(f"Signed in as {who}. Saved to {home.root}.")
    return 0


def status(home: Home) -> int:
    cfg = home.config()
    print(f"Home: {home.root}")
    if not cfg.url or not home.token():
        print("Not set up: run `daimon-canvas login`.")
        return 1
    try:
        print(f"Canvas: {cfg.url}, signed in as {check(home, cfg.url, home.token())}")
    except Exception as e:  # noqa: BLE001
        print(f"Canvas: {cfg.url}, but it isn't working: {e}")
        return 1
    return 0


def todo(home: Home, days: int) -> int:
    """canvas_assignments, from the terminal: a quick way to see the server's output."""
    from mcp import Client

    from .mcp_server import build

    async def go() -> str:
        async with Client(build(home)) as client:
            result = await client.call_tool("canvas_assignments", {"days_ahead": days})
            return "\n".join(c.text for c in result.content)
    print(asyncio.run(go()))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="daimon-canvas", description=__doc__)
    parser.add_argument("--home", help="the server's folder (default $DAIMON_CANVAS_HOME or ~/.daimon-canvas)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="the MCP server, over stdio (put this in the agent's mcp.json)")
    p_login = sub.add_parser("login", help="connect your Canvas account (address + access token)")
    p_login.add_argument("--url", help="your school's Canvas address")
    sub.add_parser("status", help="check the login")
    p_todo = sub.add_parser("todo", help="what's due, as the agent would see it")
    p_todo.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    home = Home(resolve_home(args.home))

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main(home)
    elif args.cmd == "login":
        sys.exit(login(home, args.url))
    elif args.cmd == "status":
        sys.exit(status(home))
    elif args.cmd == "todo":
        sys.exit(todo(home, args.days))
