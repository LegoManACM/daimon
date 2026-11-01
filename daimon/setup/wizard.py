"""The `daimon setup` wizard: one question at a time. Prompting lives in Wizard; the work is in plain functions."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.markup import escape as esc

from ..home import Home, git, resolve_home
from . import addons, service, update
from .providers import (OLLAMA, PROVIDERS, KeyProblem, Model, Provider, clean_key, fetch_models, masked,
                        ollama_models, search)

TELEGRAM_WAIT_S = 120
SHOWN = 15  # models listed at once; search reaches the rest


# ---- the work ----------------------------------------------------------------------------------------------
def check_model(model: str) -> str:
    """A tiny real call. Returns '' if it worked, else the error's first line."""
    try:
        import litellm
        litellm.suppress_debug_info = True
        litellm.completion(model=model, messages=[{"role": "user", "content": "Reply with OK."}], max_tokens=5,
                           timeout=60)
    except Exception as e:  # noqa: BLE001
        lines = str(e).strip().splitlines()
        first = (lines[0] if lines else type(e).__name__)[:300]
        if re.search(r"AuthenticationError|User not found|invalid.{0,10}api.?key|401", first, re.I):
            return f"the provider didn't accept the API key ({first})"
        if re.search(r"NotFoundError|not found|does not exist|404|not a valid model", first, re.I):
            return f"the provider doesn't have that model ({first})"
        return first
    return ""


def use_model(home: Home, model: str, price: tuple[float, float] | None = None) -> None:
    """What `init --model` does: list the model in models.yaml and make it the main worker's."""
    home._use_model(model, price)
    home.commit(f"setup: main model {model}", ["models.yaml", "workers/main.md"])


def telegram(token: str, method: str, wait: float = 15, **params: Any) -> Any:
    """Call the Bot API. Raises ValueError with a message that never contains the token."""
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=wait)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ValueError(f"{type(e).__name__}: {str(e).replace(token, '<token>')}") from None
    if not data.get("ok"):
        code, text = data.get("error_code"), data.get("description", "request failed")
        if code in (401, 404):
            text = "Telegram doesn't know that token. Copy it again from @BotFather."
        elif code == 409:
            text = "something else is reading this bot's messages (is `daimon run` or `daimon chat` running?)"
        raise ValueError(text)
    return data["result"]


def wait_for_chat(token: str, seconds: float = TELEGRAM_WAIT_S, api=telegram) -> tuple[int, str] | None:
    """Wait for any message to the bot; return its (chat id, sender name). Marks it read, so the agent won't
    answer it later. None if nothing arrives in time."""
    end, offset, found = time.monotonic() + seconds, None, None
    while time.monotonic() < end and not found:
        poll = max(1, min(10, int(end - time.monotonic())))
        for u in api(token, "getUpdates", wait=poll + 10, offset=offset, timeout=poll) or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if chat := msg.get("chat"):
                found = (chat["id"], chat.get("first_name") or chat.get("title") or chat.get("username") or "")
    if found and offset is not None:
        api(token, "getUpdates", offset=offset, timeout=0)
    return found


def web_line(home: Home) -> str:
    cfg = home.config().get("web") or {}
    if not cfg.get("enabled"):
        return "off (`daimon config web`)"
    from ..web.channel import DEFAULT_PORT
    return f"{cfg.get('listen', 'localhost')}, port {cfg.get('port', DEFAULT_PORT)}, "            f"{cfg.get('auth') or ('tailscale' if cfg.get('listen') == 'tailscale' else 'token')}"


def claude_path() -> str | None:
    return shutil.which("claude")


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---- the prompts -------------------------------------------------------------------------------------------
class Ask:
    """Questions, through questionary. Tests swap in a scripted version with the same methods."""

    def __init__(self):
        import questionary
        self.q = questionary
        self.style = questionary.Style([("qmark", "fg:#c678dd bold"), ("question", "bold"),
                                        ("pointer", "fg:#c678dd bold"), ("highlighted", "fg:#c678dd bold"),
                                        ("selected", "fg:#98c379"), ("answer", "fg:#61afef bold")])

    def select(self, message: str, choices: list[tuple[str, Any]], default: Any = None) -> Any:
        options = [self.q.Choice(title, value) for title, value in choices]
        start = next((c for c in options if c.value == default), None)
        return self.q.select(message, options, default=start, style=self.style).unsafe_ask()

    def pick(self, message: str, choices: list[tuple[str, Any, str]], default: Any = None) -> Any:
        """A menu whose entries have a description line; typing filters it."""
        options = [self.q.Choice(title, value, description=desc or None) for title, value, desc in choices]
        start = next((c for c in options if c.value == default), None)
        return self.q.select(message, options, default=start, style=self.style, use_search_filter=True,
                             use_jk_keys=False, show_description=True,
                             instruction="(arrows to move, type to filter)").unsafe_ask()

    def text(self, message: str, default: str = "") -> str:
        return self.q.text(message, default=default, style=self.style).unsafe_ask().strip()

    def secret(self, message: str) -> str:
        return self.q.password(message, style=self.style).unsafe_ask().strip()

    def confirm(self, message: str, default: bool = True) -> bool:
        return self.q.confirm(message, default=default, style=self.style).unsafe_ask()

    def checkbox(self, message: str, choices: list[tuple[str, Any, bool]]) -> list[Any]:
        options = [self.q.Choice(title, value, checked=on) for title, value, on in choices]
        return self.q.checkbox(message, options, style=self.style).unsafe_ask()


@dataclass
class Done:
    knowledge: str = ""
    model: str = ""
    telegram: str = ""
    claude: bool = False
    addons: list[str] = field(default_factory=list)
    background: list[str] = field(default_factory=list)
    running: bool = False


class Wizard:
    STEPS = 8
    SECTIONS = {"model": "Provider and main model", "key": "An API key", "knowledge": "Where knowledge lives",
                "telegram": "Telegram", "web": "Web page (chat and watch it work in a browser)",
                "claude": "Claude Code", "addons": "Add-ons (Canvas, text messages)",
                "background": "Running in the background", "updates": "Updates",
                "show": "Show the current settings"}

    def __init__(self, ask, console=None, home_flag: str | None = None):
        from rich.console import Console
        self.ask, self.out = ask, console or Console(highlight=False)
        self.home_flag = home_flag
        self.history: list[str] = []
        self.numbered = True  # `daimon config` runs single steps, without the "n/7"
        self.home: Home | None = None
        self.done = Done()
        self.messages_proc: subprocess.Popen | None = None
        self.models: list[Model] | None = None

    # ---- output --------------------------------------------------------------------------------------
    def top(self) -> None:
        """A fresh screen for each step, read from the top: the title, then what's done so far."""
        self.out.clear()
        self.out.print(f"[bold magenta]Daimon[/] [dim]{'setup' if self.numbered else 'config'}[/]")
        for line in self.history[-12:]:
            self.out.print(f"[green]✓[/] {line}")

    def step(self, n: int, title: str) -> None:
        self.top()
        self.out.print()
        label = f"[bold magenta]{n}/{self.STEPS}[/]  [bold]{title}[/]" if self.numbered else f"[bold]{title}[/]"
        self.out.rule(label, align="left", style="magenta")

    def say(self, text: str) -> None:
        self.out.print(text)

    def ok(self, text: str) -> None:
        self.history.append(text)
        self.out.print(f"[green]✓[/] {text}")

    def warn(self, text: str) -> None:
        self.out.print(f"[yellow]![/] {text}")

    def panel(self, body: Any, title: str, style: str = "magenta") -> None:
        from rich.panel import Panel
        self.out.print(Panel(body, title=title, title_align="left", border_style=style, padding=(1, 2)))

    # ---- the steps -----------------------------------------------------------------------------------
    def run(self) -> int:
        try:
            self.welcome()
            self.knowledge()
            self.model()
            self.telegram()
            self.claude()
            self.addons()
            self.background()
            self.finish()
        except KeyboardInterrupt:
            self.out.print("\n[yellow]Setup stopped.[/] What's done so far is saved; run [bold]daimon setup[/] "
                           "again to pick up where you left off, or [bold]daimon config[/] to change one part.")
            return 1
        return 0

    def configure(self, home: Home, section: str | None = None) -> int:
        """`daimon config`: change one part of an existing agent, or pick parts from a menu until Done."""
        self.home, self.numbered = home, False
        home.load_secrets()
        try:
            while True:
                pick = section or self.ask.select("What do you want to change?",
                                                  [*((v, k) for k, v in self.SECTIONS.items()), ("Done", "done")])
                if pick == "done":
                    return 0
                self.section(pick)
                if section:
                    return 0
        except KeyboardInterrupt:
            self.out.print("\n[yellow]Stopped.[/] What's done so far is saved.")
            return 1

    def section(self, name: str) -> None:
        if name == "key":
            self.step(0, "API key")
            p = PROVIDERS[self.ask.select("Which provider's key?",
                                          [(v.title, k) for k, v in PROVIDERS.items() if v.env])]
            self.api_key(p, force=True)
        else:
            getattr(self, name)()

    def welcome(self) -> None:
        self.out.clear()
        self.panel("[bold]Daimon[/] is a personal agent that keeps working in the background. Everything it is "
                   "(its instructions, memory, workers and settings) lives in markdown files in one folder, which "
                   "you and it can both edit.\n\nThis takes a few minutes. Use the arrow keys and Enter; Ctrl+C stops "
                   "at any point. Later, [bold]daimon config[/] changes any one part.",
                   "Welcome")
        self.step(1, "Home folder")
        while True:
            root = Path(self.ask.text("Where should the agent live?", str(resolve_home(self.home_flag))))
            home = Home(resolve_home(str(root)))
            if not home.exists():
                home.init()
                self.ok(f"Created {esc(str(home.root))}")
                break
            choice = self.ask.select(f"There's already an agent at {home.root}.",
                                     [("Reconfigure it (its files and memory are kept)", "keep"),
                                      ("Use a different folder", "other")])
            if choice == "keep":
                break
        self.home = home
        home.load_secrets()
        self.say(f"[dim]Secrets (API keys, tokens) go in {esc(str(home.secrets_file))}, outside the agent's folder, so the "
                 "agent itself can never read them.[/]")

    def knowledge(self) -> None:
        home = self.home
        self.step(2, "Knowledge")
        self.say("The agent keeps what it learns (people, projects, preferences) as markdown notes. They can live in "
                 "its own folder, or in one you already have, like an Obsidian vault or the notes another agent "
                 "keeps: point several agents at one folder and they share what they know.")
        current, own = home.knowledge, home.root / "knowledge"
        options = [(f"In the agent's folder ({own})", "home"), ("Another folder...", "other")]
        if current.resolve() != own.resolve():
            options.insert(0, (f"Keep using {current}", "keep"))
        choice = self.ask.select("Where should its knowledge live?", options, default=options[0][1])
        if choice == "keep":
            self.done.knowledge = str(current)
            return
        if choice == "home":
            folder, path = "knowledge", own
            path.mkdir(parents=True, exist_ok=True)
        else:
            while True:
                raw = self.ask.text("Folder (an existing one, or a new one to create):")
                if not raw:
                    return
                path = Path(raw).expanduser().resolve()
                if path.is_file():
                    self.warn(f"{esc(str(path))} is a file. Pick a folder.")
                    continue
                if not path.exists():
                    if not self.ask.confirm(f"{path} doesn't exist yet. Create it?", True):
                        continue
                    path.mkdir(parents=True)
                break
            try:
                folder = "~/" + path.relative_to(Path.home()).as_posix()
            except ValueError:
                folder = path.as_posix()
        if path.resolve() != current.resolve():
            home.set_knowledge_folder(folder)
            home.commit(f"setup: knowledge in {folder}", ["config.yaml"])
        notes = len(list(path.rglob("*.md")))
        self.ok(f"Knowledge: {path} ({notes} note{'' if notes == 1 else 's'} there now)")
        self.done.knowledge = str(path)
        if path.resolve() != own.resolve() and not (path / ".git").exists() and \
                git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            self.say("[dim]It isn't a git repo, so the agent's edits there have no history to undo with. Run "
                     "`git init` in it if you'd like that.[/]")
        old = [n for n in current.rglob("*.md") if n.name not in ("index.md", "log.md")] if current.exists() else []
        if old and current.resolve() != path.resolve():
            self.say(f"[dim]The {len(old)} notes in {esc(str(current))} stay there; copy them over to keep them.[/]")

    def model(self) -> None:
        home = self.home
        self.step(3, "Model provider")
        current = home.worker("main").model
        p = PROVIDERS[self.ask.select("Which AI provider should the agent use?",
                                      [(v.title, k) for k, v in PROVIDERS.items()],
                                      default=next((k for k in PROVIDERS if current.startswith(k)), "anthropic"))]
        self.models = self.api_key(p) if p.env else self.ollama() if p.key == "ollama" else None
        self.step(4, "Main model")
        while True:
            model = self.pick_model(p, current)
            if not model:
                self.warn("No model chosen; the main model stays " + current)
                return
            with self.out.status(f"Checking {model} with a tiny request..."):
                problem = check_model(model)
            if not problem:
                break
            self.warn(f"That didn't work: {esc(problem)}")
            again = self.ask.select("What now?", [("Try again", "retry"), ("Enter the API key again", "key"),
                                                  ("Pick another model", "model"), ("Use it anyway", "keep")]
                                    if p.env else [("Try again", "retry"), ("Pick another model", "model"),
                                                   ("Use it anyway", "keep")])
            if again == "key":
                self.models = self.api_key(p, force=True) or self.models
            if again == "keep":
                break
        chosen = next((m for m in self.models or [] if m.id == model), None)
        use_model(home, model, chosen.price if chosen else None)
        self.done.model = model
        self.ok(f"Main model: {model} (change it with [bold]daimon config model[/], or ask the agent)")

    def api_key(self, p: Provider, force: bool = False) -> list[Model] | None:
        """Get a working key for `p` into the secrets file. Returns the provider's models when the key checked out."""
        have = os.environ.get(p.env, "")
        if have and not force and self.ask.confirm(f"{p.env} is already set ({masked(have)}). Keep it?", True):
            try:
                with self.out.status(f"Checking the key with {p.title}..."):
                    return fetch_models(p, have)
            except KeyProblem as e:
                self.warn(f"The saved key doesn't work: {esc(str(e))}")
            except ConnectionError as e:
                self.warn(f"{esc(str(e))}; the key wasn't checked.")
                return None
        self.say(f"Get a key at [link={p.where}]{p.where}[/link]. Paste it with Ctrl+V or a right-click. It shows "
                 "as dots while you type; you'll see its first and last characters afterwards.")
        while True:
            key = clean_key(self.ask.secret(f"{p.title} API key:"), p.env)  # a key is never shown as you type it
            if not key:
                choice = self.ask.select("Nothing came through.", [("Paste again", "again"),
                                                                   ("I'll put it in the secrets file myself", "file"),
                                                                   ("Skip for now", "skip")])
                if choice != "again":
                    self.key_by_hand(p) if choice == "file" else \
                        self.warn("No key saved. Add it later with [bold]daimon config key[/].")
                    return None
                continue
            self.say(f"Got {len(key)} characters: [bold]{esc(masked(key))}[/]")
            if p.prefix and not key.startswith(p.prefix):
                self.warn(f"{p.title} keys usually start with {p.prefix}. Check it's the right one.")
            try:
                with self.out.status(f"Checking the key with {p.title}..."):
                    models = fetch_models(p, key)
            except KeyProblem as e:
                self.warn(f"{p.title} turned it down: {esc(str(e))}")
                choice = self.ask.select("What now?", [("Paste it again", "again"), ("Save it anyway", "save"),
                                                       ("I'll put it in the secrets file myself", "file"),
                                                       ("Skip for now", "skip")])
                if choice in ("skip", "file"):
                    if choice == "file":
                        self.key_by_hand(p)
                    return None
                if choice == "again":
                    continue
                models = None
            except ConnectionError as e:
                self.warn(f"{esc(str(e))}; saving the key without checking it.")
                models = None
            self.home.set_secret(p.env, key)
            self.ok(f"{'Key works. ' if models is not None else ''}Saved {p.env} to the secrets file")
            return models

    def key_by_hand(self, p: Provider) -> None:
        """For when pasting into the hidden prompt doesn't work: the key goes straight into the file, never on screen."""
        self.say(f"Open [bold]{esc(str(self.home.secrets_file))}[/] in a text editor, add a line "
                 f"[bold]{p.env}=[/][dim]your key[/], and save it. Then run [bold]daimon config key[/] to check it.")

    def ollama(self) -> list[Model]:
        while (local := ollama_models()) is None:
            self.warn(f"Ollama isn't answering at {OLLAMA}. Install it from https://ollama.com and start it.")
            if self.ask.select("What now?", [("Check again", "retry"), ("Continue anyway", "go")]) == "go":
                return []
        if local:
            self.ok(f"Ollama is running with {len(local)} model(s)")
        else:
            self.say("[dim]No models pulled yet: run `ollama pull qwen3.5:9b` (or any model) before chatting.[/]")
        return [Model(m) for m in local]

    def pick_model(self, p: Provider, current: str) -> str:
        """Pick from the provider's live list (searchable), or its suggestions when the list isn't available."""
        mine = current if current.startswith(p.key) else ""
        models = self.models or [Model(m) for m in p.models]
        if not self.models and p.key != "ollama":
            self.say("[dim]Couldn't get the provider's model list, so here are some suggestions.[/]")
        by_id = {m.id: m for m in models}
        top = [by_id[m] for m in p.models if m in by_id] + [m for m in models if m.id not in p.models]
        query = ""
        while True:
            found = search(models, query) if query else top
            if query:
                self.say(f"[dim]{len(found)} match{'' if len(found) == 1 else 'es'} for \"{esc(query)}\"[/]")
            choices = [(f"{mine} (current)", mine, by_id[mine].describe() if mine in by_id else "")] \
                if mine and not query else []
            choices += [(m.id, m.id, m.describe()) for m in found[:SHOWN] if m.id != mine or query]
            if len(models) > SHOWN or query:
                choices.append((f"Search all {len(models)} models...", "?search", "by name or id, e.g. sonnet, gpt, free"))
            if query:
                choices.append(("Back to the top of the list", "?top", ""))
            choices.append(("Type a model id", "?type", "any LiteLLM id, like openrouter/<maker>/<model>"))
            picked = self.ask.pick("Main model:", choices, default=mine or (choices[0][1] if choices else None))
            if picked == "?search":
                query = self.ask.text("Search for:")
            elif picked == "?top":
                query = ""
            elif picked == "?type":
                return self.ask.text("Model id (provider/model):")
            else:
                return picked

    def telegram(self) -> None:
        home = self.home
        self.step(5, "Telegram (optional)")
        self.say("With a Telegram bot you can talk to your agent from your phone, get its notices, and approve "
                 "what it wants to do while it runs in the background.")
        have = os.environ.get("TELEGRAM_TOKEN")
        options = [("Set up a Telegram bot", "yes"), ("Skip", "no")]
        if have:
            options.insert(0, ("Keep the current bot", "keep"))
        choice = self.ask.select("Telegram:", options, default="keep" if have else "yes")
        if choice == "keep":
            self.done.telegram = "already set up"
            return
        if choice == "no":
            return
        self.say("In Telegram, open [bold]@BotFather[/], send [bold]/newbot[/], pick a name and a username. It "
                 "replies with a token like [dim]123456789:AA...[/].")
        while True:
            token = self.ask.secret("Bot token (empty to skip):")
            if not token:
                return
            try:
                bot = telegram(token, "getMe")
                break
            except ValueError as e:
                self.warn(f"That didn't work: {esc(str(e))}")
        name = bot.get("username", "your bot")
        self.ok(f"Connected to @{name}")
        home.set_secret("TELEGRAM_TOKEN", token)
        while True:
            self.say(f"Now send [bold]@{name}[/] any message from your phone (Ctrl+C to skip).")
            try:
                with self.out.status("Waiting for your message..."):
                    found = wait_for_chat(token)
            except KeyboardInterrupt:
                found = None
            except ValueError as e:
                self.warn(f"Telegram: {esc(str(e))}")
                found = None
            if found:
                chat_id, who = found
                break
            choice = self.ask.select("No message yet.", [("Wait again", "retry"), ("Type the chat id", "type"),
                                                         ("Skip (the bot tells you your chat id later)", "skip")])
            if choice == "type":
                typed = self.ask.text("Chat id:")
                if typed.lstrip("-").isdigit():
                    chat_id, who = int(typed), ""
                    break
            if choice == "skip":
                self.done.telegram = f"@{name} (no chat id yet)"
                return
        home.set_secret("TELEGRAM_CHAT_ID", str(chat_id))
        self.ok(f"Got it{f', {who}' if who else ''}: chat id {chat_id}")
        try:
            telegram(token, "sendMessage", chat_id=chat_id, text="Daimon is connected. I'll talk to you here.")
        except ValueError:
            pass
        self.done.telegram = f"@{name}"

    def claude(self) -> None:
        self.step(6, "Claude Code")
        if path := claude_path():
            self.done.claude = True
            self.ok(f"Found Claude Code ({esc(path)}). The claude-code harness is ready: the agent can hand coding and "
                    "long-running work to it, on your Claude subscription.")
            self.say("[dim]If you haven't yet, run `claude` once and log in.[/]")
        else:
            self.say("Claude Code isn't installed. It's optional: with it, the agent can hand coding and long-running "
                     "work to Claude Code on your Claude subscription. To add it later, install it from "
                     "[link=https://claude.com/claude-code]https://claude.com/claude-code[/link], run `claude` once "
                     "to log in, and restart the agent.")

    AGAIN = {"canvas": "log in again", "messages": "pair your phone again", "calendar": "add another calendar"}

    def addons(self) -> None:
        """Setup offers them all at once; `daimon config addons` works on one at a time, so changing one never
        means redoing the others."""
        home = self.home
        self.step(7, "Add-ons")
        if not self.numbered:
            while True:
                servers = home.mcp().get("mcpServers", {})
                pick = self.ask.select("Which add-on?", [
                    *((f"{a.title}: {self.AGAIN[a.key]}" if a.key in servers else f"{a.title}: set up ({a.blurb})",
                       a.key) for a in addons.ADDONS.values()), ("Done", "done")])
                if pick == "done":
                    return
                self.addon(addons.ADDONS[pick])
        servers = home.mcp().get("mcpServers", {})
        picked = self.ask.checkbox("Add-ons to set up (Space to tick, Enter to go on):", [
            (f"{a.title}: {a.blurb}" + (" [already added: tick to set it up again]" if a.key in servers else ""),
             a.key, False) for a in addons.ADDONS.values()])
        for key in picked:
            self.addon(addons.ADDONS[key])

    def addon(self, a: addons.Addon) -> None:
        self.say(f"\n[bold]{a.title}[/]")
        if not addons.installed(a):
            while problem := addons.install(a):
                self.warn(f"Installing {a.dist} failed: {esc(problem)}")
                if self.ask.select("What now?", [("Try again", "retry"), ("Skip it", "skip")]) == "skip":
                    self.say(f"[dim]To retry later: {esc(' '.join(addons.install_command(a)))}[/]")
                    return
            self.ok(f"{a.dist} installed")
        if addons.add_server(self.home, a):
            self.ok(f"Added the \"{a.key}\" server to mcp.json")
        if self.setup_addon(a):
            self.done.addons.append(a.key)

    def setup_addon(self, a: addons.Addon) -> bool:
        py = sys.executable
        if a.key == "canvas":
            argv, retry = [py, "-m", "daimon_canvas", "login"], "daimon-canvas login"
        elif a.key == "calendar":
            kind = self.ask.select("Which calendar? (Run `daimon-calendar add` again later for more.)", [
                ("Google Calendar", "google"), ("Outlook / Microsoft 365", "microsoft"), ("Apple iCloud", "apple"),
                ("Fastmail", "fastmail"), ("Yahoo", "yahoo"), ("Another CalDAV server (Nextcloud...)", "caldav"),
                ("A read-only .ics / webcal address", "ics")])
            argv, retry = [py, "-m", "daimon_calendar", "add", kind], f"daimon-calendar add {kind}"
        else:
            if not self.ensure_messages():
                return False
            argv, retry = [py, "-m", "daimon_messages", "pair"], "daimon-messages pair"
        while addons.run_interactive(argv) != 0:
            if self.ask.select(f"{a.title} isn't set up yet.", [("Try again", "retry"), ("Skip for now", "skip")]) \
                    == "skip":
                self.say(f"[dim]Finish it later with `{retry}`.[/]")
                return False
        self.ok(f"{a.title} is set up")
        return True

    def ensure_messages(self) -> bool:
        if addons.messages_running():
            return True
        log = self.home.cache / "daimon-messages.log"
        self.messages_proc = addons.start_messages(log)
        with self.out.status("Starting the messaging service (the first start builds a small helper)..."):
            end = time.monotonic() + 180
            while time.monotonic() < end and self.messages_proc.poll() is None:
                if addons.messages_running():
                    return True
                time.sleep(2)
        self.warn(f"The messaging service didn't start; see {esc(str(log))}. Start it with `daimon-messages run`, then "
                  "`daimon-messages pair`.")
        return False

    def background(self) -> None:
        home = self.home
        self.step(8, "Keep it running")
        has_tg = bool(os.environ.get("TELEGRAM_TOKEN"))
        messages = "messages" in self.done.addons or "messages" in home.mcp().get("mcpServers", {})
        self.say("Daimon can start at login and keep working in the background: reminders, routines and tasks run "
                 "even with no terminal open.")
        if not has_tg:
            self.warn("Running in the background needs Telegram, so it can reach you and ask before acting. Run "
                      "`daimon config telegram`, then `daimon config background`.")
            if not messages or not self.ask.confirm("Start just the text messaging service at login?", True):
                self.close_messages()
                return
        elif not self.ask.confirm("Start Daimon at login and keep it running in the background?", True):
            self.close_messages()
            return
        if has_tg:
            self.say("[dim]While it runs in the background, `daimon chat` can't open the same agent (only one copy "
                     "runs at a time): talk to it on Telegram, or run `daimon service stop` first.[/]")
        start = self.ask.confirm("Start it now?", True)
        if start and self.messages_proc:
            stop_process(self.messages_proc)  # the service starts its own copy
            self.messages_proc = None
        # the Canvas watcher only feeds the agent's inbox, so it runs when the agent does
        canvas = has_tg and ("canvas" in self.done.addons or "canvas" in home.mcp().get("mcpServers", {}))
        lines = service.install(home, messages=messages, start_now=start, agent=has_tg, canvas=canvas)
        for line in lines:
            self.ok(esc(line))
        self.done.background = lines
        self.done.running = start
        if has_tg:
            auto = self.ask.confirm("Install new Daimon releases by itself when it's idle? (Otherwise it tells you and "
                                    "`daimon update` installs them.)", False)
            self.set_updates("auto" if auto else "notify")
        if service.platform() == "linux":
            self.say("[dim]To keep it running while you're logged out: `loginctl enable-linger`.[/]")

    def close_messages(self) -> None:
        if self.messages_proc:
            self.say(f"[dim]The messaging service keeps running for now (pid {self.messages_proc.pid}). After a "
                     "restart, start it with `daimon-messages run`.[/]")

    def web(self) -> None:
        """The web page: where it listens and who gets in (daimon/web/access.py)."""
        from ..web import access
        from ..web.channel import DEFAULT_PORT
        home = self.home
        self.step(0, "Web page")
        self.say("A page for chatting with the agent and watching it work: what each worker is doing right now, "
                 "tool calls, tasks, spend and its change history. The running agent serves it (`daimon run` or "
                 "`daimon chat`).")
        cfg = home.config().get("web") or {}
        has_ts = bool(access.tailscale_cli())
        choices = [("Only on this computer (also for a reverse proxy or tunnel you run here)", "localhost"),
                   ("My devices on Tailscale" + ("" if has_ts else " (Tailscale isn't installed here)"), "tailscale"),
                   ("Anything on my network (home Wi-Fi, a VPN); browsers sign in with a token", "all"),
                   ("A specific address of this computer...", "address"), ("Off", "off")]
        current = cfg.get("listen", "localhost") if cfg.get("enabled") else "off"
        pick = self.ask.select("Where should the page be reachable?", choices,
                               default=current if current in ("localhost", "tailscale", "all", "off") else "address")
        if pick == "off":
            home.set_option("web", "enabled", False)
            home.commit("setup: web page off", ["config.yaml"])
            self.ok("Web page: off")
            return
        listen = pick
        if pick == "address":
            listen = self.ask.text("Address (an IP of this computer):", cfg.get("listen", ""))
        try:
            bound = access.resolve_listen(listen)
        except access.AccessError as e:
            self.warn(f"{e}. Nothing changed.")
            return
        auth = "token"
        if listen == "tailscale":
            auth = self.ask.select("Who gets in?", [
                ("Devices logged in to my Tailscale account (no password)", "tailscale"),
                ("Anyone on the tailnet with the sign-in token", "token")], default=cfg.get("auth", "tailscale"))
        elif listen == "localhost":
            auth = self.ask.select("Ask for a sign-in token?", [
                ("Yes, a token, once per browser (recommended, and needed behind a tunnel or proxy)", "token"),
                ("No: anyone who can open it on this computer gets in", "none")], default=cfg.get("auth", "token"))
        port = self.ask.text("Port:", str(cfg.get("port", DEFAULT_PORT)))
        if not port.isdigit():
            self.warn("That isn't a port number. Nothing changed.")
            return
        for key, value in (("enabled", True), ("listen", listen), ("port", int(port)), ("auth", auth)):
            home.set_option("web", key, value)
        home.commit(f"setup: web page on {listen}, {auth}", ["config.yaml"])
        if auth == "token" and not os.environ.get("WEB_TOKEN"):
            home.set_secret("WEB_TOKEN", access.new_token())
        url = access.page_urls(listen, bound, int(port))[0]
        who = {"token": "sign-in token", "tailscale": "your Tailscale devices", "none": "no sign-in"}[auth]
        self.ok(f"Web page: {url} ({who})")
        lines = [f"[bold]{esc(url)}[/]"]
        if auth == "token":
            lines.append(f"\nSign-in link for each browser (keep it private):\n{esc(url)}/?token={esc(os.environ['WEB_TOKEN'])}")
        if listen == "localhost":
            lines.append("\nTo reach it from elsewhere, point a reverse proxy or tunnel (Caddy, nginx, Cloudflare Tunnel) "
                         "at it, or pick Tailscale or your network here.")
        lines.append("\nIt starts with the agent: restart it if it's running (`daimon service restart`). "
                     "After that, `daimon web` opens it.")
        self.panel("\n".join(lines), "Web page")

    def updates(self) -> None:
        self.step(0, "Updates")
        check, auto = update.settings(self.home)
        self.say(f"You have Daimon {update.current()}. `daimon update` installs a newer release any time.")
        pick = self.ask.select("When there's a new release:", [
            ("Tell me, and I'll run `daimon update`", "notify"),
            ("Install it by itself when it's idle (when it runs in the background)", "auto"),
            ("Don't check", "off")], default="auto" if auto else "notify" if check else "off")
        self.set_updates(pick)

    def set_updates(self, mode: str) -> None:
        self.home.set_option("updates", "check", mode != "off")
        self.home.set_option("updates", "auto", mode == "auto")
        self.home.commit(f"setup: updates {mode}", ["config.yaml"])
        self.ok({"notify": "Updates: it tells you when there's a new release",
                 "auto": "Updates: installed by itself when it's idle",
                 "off": "Updates: not checked; run `daimon update` yourself"}[mode])

    def show(self) -> None:
        home = self.home
        self.step(0, "Current settings")
        chat = os.environ.get("TELEGRAM_CHAT_ID")
        rows = [("Home folder", str(home.root)), ("Secrets file", str(home.secrets_file)),
                ("Main model", home.worker("main").model), ("Knowledge", str(home.knowledge)),
                ("API keys", ", ".join(f"{p.env} ({masked(os.environ[p.env])})" for p in PROVIDERS.values()
                                       if p.env and os.environ.get(p.env)) or "none"),
                ("Telegram", ("set up" + (f", chat {chat}" if chat else ", no chat id yet"))
                 if os.environ.get("TELEGRAM_TOKEN") else "not set up"),
                ("Web page", web_line(home)),
                ("Claude Code", "ready" if claude_path() else "not installed (optional)"),
                ("Add-ons", ", ".join(a.title for a in addons.ADDONS.values()
                                      if a.key in home.mcp().get("mcpServers", {})) or "none"),
                ("Background", "; ".join(service.status(home)) or "off"),
                ("Updates", {(True, True): "installed by itself", (True, False): "tells you",
                             (False, False): "not checked", (False, True): "not checked"}[update.settings(home)]
                 + f" (you have {update.current()})")]
        self.out.print(self.table(rows))

    def table(self, rows: list[tuple[str, str]]):
        from rich.table import Table
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(overflow="fold")
        for k, v in rows:
            grid.add_row(k, esc(v))
        return grid

    def finish(self) -> None:
        home, d = self.home, self.done
        rows = [("Home folder", str(home.root)), ("Secrets file", str(home.secrets_file)),
                ("Main model", d.model or home.worker("main").model),
                ("Knowledge", d.knowledge or str(home.knowledge)),
                ("Telegram", d.telegram or "not set up"),
                ("Claude Code", "ready" if d.claude else "not installed (optional)"),
                ("Add-ons", ", ".join(addons.ADDONS[k].title for k in d.addons) or "none"),
                ("Background", "running, starts at login" if d.running else
                 "starts at login" if d.background else "off")]
        from rich.console import Group
        grid = self.table(rows)
        self.out.clear()
        if d.running and d.telegram:
            nxt = "Message your bot on Telegram: it's running now."
        elif d.running:
            nxt = "The messaging service is running. Run [bold]daimon chat[/] to talk to your agent."
        else:
            nxt = "Run [bold]daimon chat[/] to talk to your agent."
        self.panel(Group(grid, f"\n{nxt}\nChange anything later with [bold]daimon config[/]; background control: "
                               "[bold]daimon service status|start|stop[/]."), "Ready", "green")


def preflight(command: str) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"`{command}` asks questions, so it needs an interactive terminal. For scripts, use `daimon init`.")
        return False
    if not shutil.which("git"):  # the home folder is a git repo: every change the agent makes is a commit
        print(f"Daimon needs git, which keeps the history of everything it changes. Install it, open a new terminal "
              f"and run `{command}` again:\n  Windows: winget install Git.Git\n  macOS:   xcode-select --install\n"
              "  Linux:   your package manager, e.g. sudo apt install git")
        return False
    return True


def interactive(command: str, go) -> int:
    try:
        return go(Ask())
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "NoConsoleScreenBufferError":
            print(f"This terminal can't show the menus. Run `{command}` in Windows Terminal, PowerShell or cmd (or "
                  "prefix it with `winpty` in Git Bash).")
            return 1
        raise


def run(home_flag: str | None = None) -> int:
    if not preflight("daimon setup"):
        return 1
    return interactive("daimon setup", lambda ask: Wizard(ask, home_flag=home_flag).run())


def configure(home_flag: str | None = None, section: str | None = None) -> int:
    home = Home(resolve_home(home_flag))
    if not home.exists():
        print(f"No agent at {home.root} yet. Run `daimon setup` to make one.")
        return 1
    if not preflight("daimon config"):
        return 1
    return interactive("daimon config", lambda ask: Wizard(ask, home_flag=home_flag).configure(home, section))
