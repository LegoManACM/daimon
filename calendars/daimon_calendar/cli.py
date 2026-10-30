"""daimon-calendar: the calendar MCP server, and the commands for connecting calendars to it."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .caldav import PRESETS, check_feed
from .config import Home, account_name, resolve_home
from .model import CalendarError

KINDS = ["google", "microsoft", "apple", "fastmail", "yahoo", "caldav", "ics"]

GOOGLE_HELP = """Google needs an OAuth client of your own (free, once):
  1. console.cloud.google.com → create a project → APIs & Services → Library → enable "Google Calendar API".
  2. OAuth consent screen: External, add yourself as a test user (or publish it, so sign-ins don't expire weekly).
  3. Credentials → Create credentials → OAuth client ID → Desktop app → download the JSON."""

MICROSOFT_HELP = """Microsoft needs an app registration of your own (free, once):
  1. entra.microsoft.com → App registrations → New registration. Accounts: "any organizational directory and
     personal Microsoft accounts". No redirect URI.
  2. Authentication → Advanced settings → Allow public client flows: Yes.
  3. Copy its Application (client) ID."""


def verify(home: Home, name: str, data: dict) -> list[str]:
    """Lists the account's calendars with the new credentials (saving anything learned). Raises if it fails."""
    from .service import KINDS as PROVIDERS

    async def go() -> list[str]:
        p = PROVIDERS[data["kind"]](name, data, lambda d: None, home.config().tz())
        try:
            found = [c.name for c in await p.calendars()]
            data.setdefault("user", await p.whoami())
            return found
        finally:
            await p.aclose()
    return asyncio.run(go())


def ask(prompt: str, given: str | None = None, secret: bool = False) -> str:
    if given:
        return given.strip()
    if not secret:
        return input(prompt).strip()
    value = secret_input(prompt).strip()
    if value:
        print(f"Got {len(value)} characters: {masked(value)}")
    return value

def secret_input(prompt: str) -> str:
    """Read a secret, showing * for each character (getpass shows nothing at all, which looks broken)."""
    if not sys.stdin.isatty():
        return getpass.getpass(prompt)
    from prompt_toolkit import prompt as ask_hidden
    return ask_hidden(prompt, is_password=True)


def masked(secret: str) -> str:
    """Enough of a secret to recognise it, never enough to use it."""
    return f"{secret[:4]}…{secret[-4:]}" if len(secret) > 12 else "*" * len(secret)



def add(home: Home, args: argparse.Namespace) -> int:
    kind, name = args.kind, account_name(args.name or args.kind)
    if name in home.accounts() and not args.replace:
        print(f"There's already an account called {name}. Pick another --name, or --replace it.")
        return 1
    try:
        if kind == "google":
            from .google import sign_in
            client_id, secret = args.client_id, args.client_secret
            if args.client_file:
                raw = json.loads(Path(args.client_file).expanduser().read_text(encoding="utf-8"))
                raw = raw.get("installed") or raw.get("web") or raw
                client_id, secret = raw["client_id"], raw.get("client_secret", "")
            if not client_id:
                print(GOOGLE_HELP)
                path = ask("Path to the downloaded client JSON (or press Enter to paste the id and secret): ")
                if path:
                    raw = json.loads(Path(path.strip('"')).expanduser().read_text(encoding="utf-8"))
                    raw = raw.get("installed") or raw.get("web") or raw
                    client_id, secret = raw["client_id"], raw.get("client_secret", "")
                else:
                    client_id = ask("Client ID: ")
                    secret = ask("Client secret (shows as *): ", secret=True)
            data = sign_in(client_id, secret or "")
        elif kind == "microsoft":
            from .microsoft import sign_in
            client_id = args.client_id
            if not client_id:
                print(MICROSOFT_HELP)
                client_id = ask("Application (client) ID: ")
            data = sign_in(client_id, args.tenant)
        elif kind == "ics":
            url = ask("The calendar's .ics or webcal:// address: ", args.url)
            title = asyncio.run(check_feed(url))
            data = {"kind": "ics", "url": url, "title": args.title or title or name}
        else:
            url, how = PRESETS.get(kind, ("", "its password, or an app password if it has them"))
            url = args.url or url or ask("The CalDAV server's address (like https://cloud.example.com/remote.php/dav): ")
            if not url.startswith("http"):
                url = "https://" + url
            username = ask("Username (usually your email): ", args.username)
            print(f"Password: {how}.")
            password = ask("Password (shows as *): ", secret=True)
            data = {"kind": "caldav", "preset": kind, "url": url, "username": username, "password": password,
                    "user": username}
        found = verify(home, name, data)
    except (CalendarError, KeyError, OSError, ValueError) as e:
        print(f"That didn't work: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - network trouble, a bad answer from the server...
        print(f"That didn't work: {type(e).__name__}: {e}")
        return 1
    home.save_account(name, data)
    print(f"Connected {name} ({data.get('user') or kind}): " + (", ".join(found) or "no calendars yet") + ".")
    print(f"Saved in {home.accounts_dir}. Restart the agent (or its calendar server) to pick it up.")
    return 0


def run_tool(home: Home, tool: str, args: dict) -> str:
    from mcp import Client

    from .mcp_server import build

    async def go() -> str:
        async with Client(build(home)) as client:
            result = await client.call_tool(tool, args)
            return "\n".join(c.text for c in result.content)
    return asyncio.run(go())


def main() -> None:
    parser = argparse.ArgumentParser(prog="daimon-calendar", description=__doc__)
    parser.add_argument("--home", help="the server's folder (default $DAIMON_CALENDAR_HOME or ~/.daimon-calendar)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mcp", help="the MCP server, over stdio (put this in the agent's mcp.json)")
    p_add = sub.add_parser("add", help="connect a calendar account")
    p_add.add_argument("kind", choices=KINDS, help="apple, fastmail and yahoo are CalDAV with the address filled in")
    p_add.add_argument("--name", help="what to call this account (default: the kind)")
    p_add.add_argument("--replace", action="store_true", help="replace an account with the same name")
    p_add.add_argument("--client-id", help="google/microsoft: your OAuth client or app registration id")
    p_add.add_argument("--client-secret", help="google: the client secret")
    p_add.add_argument("--client-file", help="google: the client JSON downloaded from the Cloud console")
    p_add.add_argument("--tenant", default="common", help="microsoft: common, consumers, organizations or a tenant id")
    p_add.add_argument("--url", help="caldav: the server address; ics: the feed address")
    p_add.add_argument("--username", help="caldav/apple/...: the sign-in name")
    p_add.add_argument("--title", help="ics: a name for the feed")
    p_rm = sub.add_parser("remove", help="disconnect an account (its calendars aren't touched)")
    p_rm.add_argument("name")
    sub.add_parser("status", help="the connected accounts and their calendars")
    p_agenda = sub.add_parser("agenda", help="what's on, as the agent would see it")
    p_agenda.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    home = Home(resolve_home(args.home))

    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main(home)
    elif args.cmd == "add":
        sys.exit(add(home, args))
    elif args.cmd == "remove":
        ok = home.remove_account(args.name)
        print(f"Removed {args.name}." if ok else f"No account called {args.name}.")
        sys.exit(0 if ok else 1)
    elif args.cmd == "status":
        print(f"Home: {home.root}")
        if not home.accounts():
            print("No calendar connected: run `daimon-calendar add google` (or microsoft, apple, caldav, ics).")
            sys.exit(1)
        print(run_tool(home, "calendar_status", {}))
    elif args.cmd == "agenda":
        print(run_tool(home, "calendar_events", {"start": "now", "end": (datetime.now() + timedelta(days=args.days))
                                                 .strftime("%Y-%m-%dT%H:%M")}))
