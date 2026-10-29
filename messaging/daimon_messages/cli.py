"""daimon-messages: the messaging service, its MCP server, and a few commands for setting it up."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from . import cookies, render
from .config import Home, resolve_home
from .control import ServiceDown, call
from .store import Store

PAIR_WAIT_S = 4 * 60


def setup_logging(home: Home, verbose: bool) -> None:
    home.logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file = logging.handlers.RotatingFileHandler(home.logs / "service.log", maxBytes=1_000_000, backupCount=3,
                                                encoding="utf-8")
    file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(file)
    root.addHandler(console)


def show_qr(url: str) -> None:
    try:
        import segno
        segno.make(url, error="l").terminal(compact=True)
    except Exception:  # noqa: BLE001
        print("(Install `segno` to see the QR code here.)")
    print(url)


def clipboard() -> str:
    """The clipboard's text, or '' if it can't be read (no display, no Tk)."""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001
        return ""


def read_pasted() -> str:
    lines = []
    for line in sys.stdin:
        if not line.strip() and lines:
            break
        lines.append(line)
    return "".join(lines)


def read_cookies(cookies_file: str | None) -> dict | None:
    if cookies_file:
        text = Path(cookies_file).read_text(encoding="utf-8")
    else:
        print("Google Messages pairs through your Google account now (it dropped QR codes). To get the cookies:\n"
              "  1. In a private/incognito window, open devtools (F12) → Network, then go to\n"
              "     https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config\n"
              "     and sign in (only one Google account in that window; don't go anywhere else in it).\n"
              "  2. It lands on messages.google.com/web/config. Right-click that `config` request in the Network\n"
              "     list (reload if it isn't there) → Copy → Copy as cURL (bash).\n"
              "     Firefox works as is; in Chrome, cookies can be device-bound, so Firefox is the safer bet.\n"
              "  3. Press Enter here: the copied command is read from the clipboard.\n"
              "Close that private window afterwards without signing out: signing out kills the cookies.\n")
        try:
            input()
            text = clipboard()
            if not text.strip():
                print("Couldn't read the clipboard. Paste it here instead, then press Enter on an empty line:")
                text = read_pasted()
        except (KeyboardInterrupt, EOFError):
            return None
    try:
        found = cookies.parse(text)
    except cookies.CutOff:
        print("The cURL command is cut off: terminals truncate very long pastes. Press Enter at the prompt to read "
              "it from the clipboard, or save it to a file and use `daimon-messages pair --cookies FILE`.")
        return None
    except ValueError as e:
        print(f"That JSON didn't parse: {e}")
        return None
    if not found:
        print("No cookies found in that. Copy the whole cURL command, a Cookie header, or a JSON object.")
        return None
    if gone := cookies.missing(found):
        print(f"Missing cookies: {', '.join(gone)} (found: {', '.join(sorted(found))}). Make sure you copied the "
              "messages.google.com `config` request after signing in.")
        return None
    return found


def pair(home: Home, cookies_file: str | None, qr: bool = False) -> int:
    if qr:
        body: dict = {"method": "qr"}
    else:
        found = read_cookies(cookies_file)
        if found is None:
            return 1
        body = {"method": "google", "cookies": found}
    result = call(home, "POST", "/pair", body)
    if not result.get("ok"):
        print(result.get("error", "Pairing didn't start."))
        return 1
    print("In Google Messages on your phone: profile picture → Device pairing → QR code scanner (older app versions "
          "only)." if qr else "Google Messages on your phone will show an emoji: tap the matching one.")
    shown = ""
    end = time.monotonic() + PAIR_WAIT_S
    try:
        while time.monotonic() < end:
            s = call(home, "GET", "/status", timeout=10)
            if s.get("pair_error"):
                print(f"Pairing failed: {s['pair_error']}")
                return 1
            if s.get("qr") and s["qr"] != shown:
                shown = s["qr"]
                print("\nScan this (it refreshes every 30 seconds):")
                show_qr(shown)
            if s.get("emoji") and s["emoji"] != shown:
                shown = s["emoji"]
                print(f"\nTap this emoji on your phone: {shown}")
            if shown and s.get("state") == "logged_out":  # paired, then signed straight out: don't wait it out
                print("Pairing went through, but Google signed this computer straight back out"
                      + (f" ({s['detail']})" if s.get("detail") else "") + ". Pair again; if it keeps happening, "
                      "remove old computers in Google Messages → Device pairing first.")
                return 1
            if s.get("paired") and s.get("state") in ("connecting", "connected"):
                print("Paired. Messages from the last few days are being fetched now; new ones are stored as they "
                      "arrive.")
                return 0
            time.sleep(1)
    except KeyboardInterrupt:
        call(home, "POST", "/cancel")
        print("Cancelled.")
        return 1
    print("Timed out waiting for the phone.")
    return 1


def status(home: Home) -> int:
    store = Store(home.db)
    tz = home.config().tz()
    counts = store.counts()
    print(f"Home: {home.root}")
    print(f"Stored: {counts['n']} messages in {counts['chats']} chats "
          f"({render.local(counts['first'], tz)} to {render.local(counts['last'], tz)})")
    try:
        s = call(home, "GET", "/status", timeout=5)
    except ServiceDown as e:
        print(e)
        print("Paired: " + ("yes" if home.session.exists() else "no"))
        return 1
    print(f"Service: running (pid {s['pid']})")
    print(f"Link: {s['state']}" + (f" ({s['detail']})" if s.get("detail") else ""))
    print("Paired: " + (f"yes, since {render.local(s['paired_at'], tz)}" if s.get("paired_at") else
                        "yes" if s.get("paired") else "no"))
    print(f"Since start: {s['received']} new, {s['dropped']} filtered out")
    if s.get("backfill", {}).get("running"):
        print("Catching up on history now.")
    return 0


def search(home: Home, args: argparse.Namespace) -> int:
    store = Store(home.db)
    tz = home.config().tz()
    since = render.parse_when(args.since, tz) if args.since else ""
    for m in reversed(store.search(args.query, args.person or "", since=since, limit=args.limit)):
        print(f"{store.chat_name(m['chat'])}: {render.line(m, tz, 200)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="daimon-messages", description=__doc__)
    parser.add_argument("--home", help="the service's folder (default $DAIMON_MESSAGES_HOME or ~/.daimon-messages)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="run the service: keeps the Google Messages link and stores messages")
    p_run.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("mcp", help="the MCP server, over stdio (put this in the agent's mcp.json)")
    p_pair = sub.add_parser("pair", help="pair with Google Messages on your phone (the service must be running)")
    p_pair.add_argument("--cookies", "--google-cookies", dest="cookies", metavar="FILE",
                        help="the Google sign-in cookies (cURL command, Cookie header or JSON) from a file instead of "
                             "pasting them")
    p_pair.add_argument("--qr", action="store_true",
                        help="pair with a QR code instead (older Google Messages versions only)")
    sub.add_parser("status", help="link state and what's stored")
    p_unpair = sub.add_parser("unpair", help="unpair this computer from the phone")
    p_unpair.add_argument("--wipe", action="store_true", help="also delete every stored message")
    p_search = sub.add_parser("search", help="search stored messages")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--person")
    p_search.add_argument("--since", help="e.g. 3d, today, 2026-10-20")
    p_search.add_argument("--limit", type=int, default=20)
    p_send = sub.add_parser("send", help="send a text (through the running service)")
    p_send.add_argument("text")
    target = p_send.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat", help="chat id")
    target.add_argument("--to", help="phone number")
    p_backfill = sub.add_parser("backfill", help="fetch recent history again (fills gaps)")
    p_backfill.add_argument("--days", type=int)
    p_build = sub.add_parser("build", help="build the Go sidecar from source now (needs Go; otherwise it's downloaded "
                                           "or built on first use)")
    p_build.add_argument("--download", action="store_true",
                         help="download the prebuilt sidecar for this version from GitHub instead")
    args = parser.parse_args()

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    home = Home(resolve_home(args.home))

    try:
        if args.cmd == "run":
            from .service import run
            setup_logging(home, args.verbose)
            try:
                asyncio.run(run(home))
            except KeyboardInterrupt:
                pass
        elif args.cmd == "mcp":
            from .mcp_server import main as mcp_main
            mcp_main(home)
        elif args.cmd == "pair":
            sys.exit(pair(home, args.cookies, args.qr))
        elif args.cmd == "status":
            sys.exit(status(home))
        elif args.cmd == "unpair":
            call(home, "POST", "/unpair", {"wipe": args.wipe})
            print("Unpaired." + (" Every stored message was deleted." if args.wipe else ""))
        elif args.cmd == "search":
            sys.exit(search(home, args))
        elif args.cmd == "send":
            r = call(home, "POST", "/send", {"text": args.text, "chat": args.chat or "", "to": args.to or ""})
            print("Sent." if r.get("ok") else f"Not sent: {r.get('error')}")
        elif args.cmd == "backfill":
            r = call(home, "POST", "/backfill", {"days": args.days} if args.days is not None else {})
            print("Fetching history; `daimon-messages status` shows progress." if r.get("ok") else r.get("error"))
        elif args.cmd == "build":
            from .sidecar import EXE, build, download
            err = asyncio.run((download if args.download else build)(home.bin / EXE))
            print(err or f"{'Downloaded' if args.download else 'Built'} {home.bin / EXE}")
            sys.exit(1 if err else 0)
    except ServiceDown as e:
        print(e)
        sys.exit(1)
