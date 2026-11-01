"""`daimon` command line: setup, config, init, chat, run, web, ask, inbox, service, update, uninstall."""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import os
import sys
import time
from typing import TYPE_CHECKING

from . import inbox
from .gateway import Gateway, Log, Message, configured
from .home import Home, resolve_home
from .tasks import OPEN, summary
from .terminal import Terminal, console, read_lines, status_table

if TYPE_CHECKING:
    from .agent import Agent  # imported where it's built: it pulls in litellm and mcp, ~10s before `daimon -h` answers


def version() -> str:
    try:
        return importlib.metadata.version("daimon")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def open_home(args: argparse.Namespace) -> Home:
    home = Home(resolve_home(args.home))
    if not home.exists():
        sys.exit(f"No agent at {home.root}. Run `daimon setup` first (or `daimon init` for just the files).")
    home.load_secrets()
    return home


def cmd_init(args: argparse.Namespace) -> None:
    home = Home(resolve_home(args.home))
    created = home.init(model=args.model)
    print(f"Agent home: {home.root}")
    for c in created:
        print(f"  created {c}")
    print(f"Secrets file (never committed): {home.secrets_file}")
    print(f"Main model: {home.worker('main').model}. Change it in workers/main.md, or ask the agent.")


def cmd_update(args: argparse.Namespace) -> None:
    from .setup import update
    home = Home(resolve_home(args.home))
    try:
        latest = update.latest()
    except ConnectionError as e:
        sys.exit(f"{e}. Try again later.")
    have = update.current()
    if not update.newer(latest, have):
        print(f"Daimon {have} is the latest version.")
        return
    print(f"Daimon {latest} is out (you have {have}). Release notes: "
          f"https://github.com/{update.REPO}/releases/tag/v{latest}")
    if args.check:
        print("`daimon update` installs it.")
        return
    if home.exists() and not update.running_jobs(home) and agent_running(home):
        sys.exit("The agent is open in `daimon chat`. Close it (/exit), then run `daimon update` again.")
    if not args.yes:
        try:
            answer = input(f"Update to {latest}? (y/n) ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            sys.exit("Not updated.")
    try:
        log = update.launch(home if home.exists() else None, latest, wait_pid=os.getpid())
    except RuntimeError as e:
        sys.exit(f"Can't update: {e}.")
    restarts = home.exists() and update.running_jobs(home)
    print(f"Updating to {latest} now; it takes a minute{', and the background service restarts after' if restarts else ''}. "
          f"If `daimon` isn't found meanwhile, give it a moment. Log: {log}")


def agent_running(home: Home) -> bool:
    """Whether something holds the agent's lock (`daimon chat` or `daimon run`)."""
    try:
        hold_lock(home).close()
        return False
    except SystemExit:
        return True


def cmd_service(args: argparse.Namespace) -> None:
    from .setup import service
    home = open_home(args)
    if args.action == "install":
        if not configured(home):
            sys.exit(f"Running in the background needs a way to reach you: the web page (`daimon config web`) or "
                     f"Telegram (TELEGRAM_TOKEN=... in {home.secrets_file}, or `daimon config telegram`).")
        messages = args.messages if args.messages is not None else "messages" in home.mcp().get("mcpServers", {})
        lines = service.install(home, messages=messages, start_now=args.start)
        lines.append("While it runs, `daimon chat` can't open this agent: talk to it on the web page or Telegram, "
                     "or `daimon service stop` first.")
    else:
        lines = getattr(service, args.action)(home)
    print("\n".join(lines) or "Nothing installed. `daimon service install` sets it up.")


def web_page(home: Home) -> tuple[str, str, tuple[str, int]]:
    """The page's address, a link that also signs a browser in, and where to check that it's up."""
    from .web import access
    from .web.channel import DEFAULT_PORT
    cfg = home.config().get("web") or {}
    if not cfg.get("enabled"):
        raise access.AccessError("The web page is off. `daimon config web` turns it on")
    listen, port = str(cfg.get("listen") or "localhost"), int(cfg.get("port", DEFAULT_PORT))
    bound = access.resolve_listen(listen)
    url = access.page_urls(listen, bound, port)[0]
    auth = access.make_auth(cfg, home)
    link = f"{url}/?token={auth.token()}" if isinstance(auth, access.TokenAuth) else url
    return url, link, ("127.0.0.1" if bound == "0.0.0.0" else bound, port)


def cmd_web(args: argparse.Namespace) -> None:
    import socket
    import webbrowser
    from .web.access import AccessError
    home = open_home(args)
    try:
        url, link, probe = web_page(home)
    except AccessError as e:
        sys.exit(f"{e}.")
    try:
        socket.create_connection(probe, timeout=2).close()
    except OSError:
        if agent_running(home):
            sys.exit(f"The agent is running but isn't serving {url}: it started before the page was turned on. "
                     f"Restart it (`daimon service restart`).")
        sys.exit(f"Nothing is serving {url}: the agent isn't running. `daimon service start` starts it in the "
                 f"background, or `daimon run` here.")
    if args.link:
        print(link if link == url else f"Sign-in link for another browser or your phone (keep it private):\n{link}")
    elif webbrowser.open(link):
        print(f"Opened {url}")
    else:
        print(f"Couldn't open a browser here. The page is at {url}; `daimon web --link` prints a link that signs in.")


def hold_lock(home: Home):
    """Only one running agent per home: two would double-run tasks and fight over the Telegram bot."""
    home.cache.mkdir(exist_ok=True)
    f = open(home.cache / "agent.lock", "a+")
    try:
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"The agent at {home.root} is already running (`daimon chat` or `daimon run`). Only one can run at a time.")
    return f


async def start(home: Home, channels: list) -> tuple[Agent, Gateway]:
    from .agent import Agent
    gateway = Gateway(channels)
    agent = Agent(home, gateway)
    await agent.start()
    for problem in await gateway.start(agent):
        gateway.event("error", f"channel {problem}")
    return agent, gateway


def summary_line(agent: Agent):
    """The status bar's last line: open tasks and today's spend. Spend means reading a log, so it's cached."""
    cache = {"at": 0.0, "text": ""}

    def line() -> str:
        if time.monotonic() - cache["at"] > 5:
            open_tasks = len(agent.tasks.all(OPEN))
            cache["text"] = "  ·  ".join(b for b in [
                f"{open_tasks} open task{'s' if open_tasks != 1 else ''}" if open_tasks else "",
                f"${agent.runner.spend_today():.2f} today", "/status · /help"] if b)
            cache["at"] = time.monotonic()
        return cache["text"]
    return line


async def chat(home: Home) -> None:
    lock = hold_lock(home)
    terminal = Terminal(interactive=True)
    agent, gateway = await start(home, [terminal, *configured(home)])
    terminal.summary = summary_line(agent)
    others = ", ".join(c.name for c in gateway.channels if c is not terminal)
    out = console()
    out.print(f"[bold magenta]◆ daimon[/] [dim]{version()} · {home.worker('main').model} · {home.root}[/]")
    out.print(f"[dim]It keeps working in the background while this is open"
              f"{f' and answers on {others}' if others else ''}. /help for commands, /status for what's running.[/]")
    replies: set[asyncio.Task] = set()
    try:
        async for line in read_lines(terminal):
            text = line.strip()
            if terminal.pending:
                terminal.answer_oldest(text)
            elif not text:
                continue
            elif text.startswith("/"):
                if not await command(text, agent):
                    break
            else:
                t = asyncio.create_task(terminal.on_message(Message(text, terminal.name)))
                replies.add(t)
                t.add_done_callback(replies.discard)
    finally:
        await gateway.close()
        await agent.stop()
        lock.close()


async def run(home: Home) -> None:
    """Headless: the agent works in the background and talks to you through its channels."""
    lock = hold_lock(home)
    channels = configured(home)
    if not channels:
        sys.exit(f"No channel to reach you on, so approvals would have nowhere to go. Turn on the web page "
                 f"(`daimon config web`) or Telegram (TELEGRAM_TOKEN=... from @BotFather in {home.secrets_file}), "
                 f"then run `daimon run` again.")
    agent, gateway = await start(home, [Log(), *channels])
    agent.auto_update = True  # headless only: an update restarts the agent, which would end a `daimon chat`
    try:
        if len(gateway.channels) == 1:
            sys.exit("No channel connected (see the errors above).")
        Log.print(f"Agent running ({home.root}) on {', '.join(c.name for c in gateway.channels[1:])}. Ctrl+C stops it.")
        await asyncio.Event().wait()
    finally:
        await gateway.close()
        await agent.stop()
        lock.close()


async def ask(home: Home, message: str) -> None:
    terminal = Terminal(interactive=False)
    from .agent import Agent
    gateway = Gateway([terminal])
    agent = Agent(home, gateway)
    await agent.start(scheduler=False)
    await gateway.start(agent)
    try:
        await terminal.on_message(Message(message, terminal.name))
    finally:
        await agent.stop()


HELP = """/help       this list
/status     what's running and waiting
/tasks      open tasks (/tasks all for everything)
/kg QUERY   search the knowledge graph
/tools      MCP servers and tool consent levels
/history    recent changes to the agent's files
/cost       spend today
/compact    fold older conversation into the summary now
/clear      start a fresh conversation (old one is archived)
/exit       quit (background work stops until you start it again)"""


async def command(text: str, agent: Agent) -> bool:
    parts = text.split()
    cmd = parts[0].lower()
    if cmd in ("/exit", "/quit"):
        return False
    if cmd == "/help":
        print(HELP)
    elif cmd == "/status":
        console().print(status_table(agent))
    elif cmd == "/tasks":
        print(summary(agent.tasks.all(None if parts[1:2] == ["all"] else OPEN)))
    elif cmd == "/kg":
        query = text[len(parts[0]):].strip()
        if not query:
            print(f"{agent.runner.kb.count()} notes. /kg QUERY to search.")
        for h in await agent.runner.kb.search(query) if query else []:
            print(f"  {h['path']} [{h['type']}] {h['title']}: {h['description'] or h['snippet'][:80]}")
    elif cmd == "/tools":
        await agent.hub.sync()
        for line in agent.hub.status() or ["No MCP servers configured."]:
            print(line)
        for s in agent.hub.servers.values():
            for t in s.tools:
                print(f"  {t.name} [{agent.hub.level(s.name, t)}]")
    elif cmd == "/history":
        print(agent.home.history() or "No history.")
    elif cmd == "/cost":
        print(f"${agent.runner.spend_today():.4f} today")
    elif cmd == "/compact":
        done = await agent.runner.maybe_compact(agent.thread, force=True)
        print("Compacted." if done else "Nothing to compact yet.")
    elif cmd == "/clear":
        agent.thread.clear()
        print("Started a fresh conversation.")
    else:
        print(f"Unknown command {cmd}. /help lists them.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="daimon", description="Daimon")
    parser.add_argument("--version", action="version", version=f"daimon {version()}")
    parser.add_argument("--home", help="agent home folder (default: $DAIMON_HOME or ~/.daimon)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="step-by-step setup: model, Telegram, add-ons, background running")
    p_config = sub.add_parser("config", help="change one part of the setup (no part: pick from a menu)")
    p_config.add_argument("section", nargs="?", choices=["model", "key", "knowledge", "telegram", "web", "claude",
                                                         "addons", "background", "updates", "show"])
    p_init = sub.add_parser("init", help="create the agent's home folder")
    p_init.add_argument("--model", help="LiteLLM model id for the main agent (added to models.yaml)")
    sub.add_parser("chat", help="run the agent and chat with it (plus any channels, like Telegram)")
    sub.add_parser("run", help="run the agent headless; you reach it through its channels (web page, Telegram)")
    p_web = sub.add_parser("web", help="open the web page in your browser, signed in")
    p_web.add_argument("--link", action="store_true", help="print a sign-in link for another device instead")
    p_ask = sub.add_parser("ask", help="send one message and print the reply (no background work)")
    p_ask.add_argument("message")
    p_inbox = sub.add_parser("inbox", help="add an item to the agent's inbox")
    p_inbox.add_argument("action", choices=["add"])
    p_inbox.add_argument("text", help="the item's content ('-' reads stdin)")
    p_inbox.add_argument("--source", help="where it came from: sms, email, calendar, ...")
    p_inbox.add_argument("--from", dest="sender", help="who it's from")
    p_inbox.add_argument("--title")
    p_inbox.add_argument("--for", dest="target", choices=["main"],
                         help="send straight to the main agent instead of the curator")
    p_update = sub.add_parser("update", help="install the newest Daimon release (asks first)")
    p_update.add_argument("--check", action="store_true", help="only say whether there's a newer one")
    p_update.add_argument("--yes", action="store_true", help="don't ask first")
    p_uninstall = sub.add_parser("uninstall", help="remove Daimon: start at login, its folders, keys and the program")
    p_uninstall.add_argument("--yes", action="store_true", help="don't ask first")
    p_service = sub.add_parser("service", help="run the agent in the background, started at login")
    p_service.add_argument("action", choices=["install", "uninstall", "status", "start", "stop", "restart"])
    p_service.add_argument("--messages", action=argparse.BooleanOptionalAction, default=None,
                           help="also run `daimon-messages run` (install only; default: if mcp.json has it)")
    p_service.add_argument("--start", action="store_true", help="start it now too (install only)")
    args = parser.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    if args.cmd is None:
        root = resolve_home(args.home)
        if not Home(root).exists():
            print(f"No agent at {root} yet. Run `daimon setup` to make one, step by step.")
        else:
            parser.print_help()
    elif args.cmd == "setup":
        from .setup.wizard import run as setup
        sys.exit(setup(args.home))
    elif args.cmd == "config":
        from .setup.wizard import configure
        sys.exit(configure(args.home, args.section))
    elif args.cmd == "update":
        cmd_update(args)
    elif args.cmd == "uninstall":
        from .setup.uninstall import run as uninstall
        sys.exit(uninstall(args.home, args.yes))
    elif args.cmd == "service":
        cmd_service(args)
    elif args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "chat":
        asyncio.run(chat(open_home(args)))
    elif args.cmd == "run":
        try:
            asyncio.run(run(open_home(args)))
        except KeyboardInterrupt:
            print("Stopped.")
    elif args.cmd == "web":
        cmd_web(args)
    elif args.cmd == "ask":
        asyncio.run(ask(open_home(args), args.message))
    elif args.cmd == "inbox":
        text = sys.stdin.read() if args.text == "-" else args.text
        path = inbox.drop(open_home(args), text, source=args.source, **{"from": args.sender},
                          title=args.title, **{"for": args.target})
        print(f"Added {path.name}. The running agent picks it up within a minute.")


if __name__ == "__main__":
    main()
