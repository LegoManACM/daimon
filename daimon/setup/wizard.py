"""The `daimon setup` wizard: one question at a time. Prompting lives in Wizard; the work is in plain functions."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.markup import escape as esc

from ..home import Home, resolve_home
from . import addons, service

OLLAMA = "http://localhost:11434"
TELEGRAM_WAIT_S = 120


@dataclass
class Provider:
    key: str
    title: str
    env: str | None      # the secret holding its API key
    models: list[str]
    where: str = ""      # where to get a key


PROVIDERS = {
    "anthropic": Provider("anthropic", "Anthropic (Claude)", "ANTHROPIC_API_KEY",
                          ["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"],
                          "https://console.anthropic.com/settings/keys"),
    "openai": Provider("openai", "OpenAI", "OPENAI_API_KEY", ["openai/gpt-5", "openai/gpt-5-mini"],
                       "https://platform.openai.com/api-keys"),
    "openrouter": Provider("openrouter", "OpenRouter (many models, one key)", "OPENROUTER_API_KEY",
                           ["openrouter/anthropic/claude-sonnet-5.5", "openrouter/openai/gpt-5",
                            "openrouter/google/gemini-2.5-pro"], "https://openrouter.ai/keys"),
    "gemini": Provider("gemini", "Google Gemini", "GEMINI_API_KEY", ["gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash"],
                       "https://aistudio.google.com/apikey"),
    "ollama": Provider("ollama", "Ollama (runs on this computer, free, no key)", None, ["ollama_chat/qwen3.5:9b"],
                       "https://ollama.com"),
}


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
        return (lines[0] if lines else type(e).__name__)[:300]
    return ""


def ollama_models(base: str = OLLAMA) -> list[str] | None:
    """Local Ollama models as LiteLLM ids, or None if Ollama isn't reachable."""
    try:
        r = httpx.get(f"{base}/api/tags", timeout=3)
        r.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    return [f"ollama_chat/{m['name']}" for m in r.json().get("models", [])]


def use_model(home: Home, model: str) -> None:
    """What `init --model` does: list the model in models.yaml and make it the main worker's."""
    home._use_model(model)
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
    model: str = ""
    telegram: str = ""
    claude: bool = False
    addons: list[str] = field(default_factory=list)
    background: list[str] = field(default_factory=list)
    running: bool = False


class Wizard:
    STEPS = 7

    def __init__(self, ask, console=None, home_flag: str | None = None):
        from rich.console import Console
        self.ask, self.out = ask, console or Console(highlight=False)
        self.home_flag = home_flag
        self.home: Home | None = None
        self.done = Done()
        self.messages_proc: subprocess.Popen | None = None

    # ---- output --------------------------------------------------------------------------------------
    def step(self, n: int, title: str) -> None:
        self.out.print()
        self.out.rule(f"[bold magenta]{n}/{self.STEPS}[/]  [bold]{title}[/]", align="left", style="magenta")

    def say(self, text: str) -> None:
        self.out.print(text)

    def ok(self, text: str) -> None:
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
            self.model()
            self.telegram()
            self.claude()
            self.addons()
            self.background()
            self.finish()
        except KeyboardInterrupt:
            self.out.print("\n[yellow]Setup stopped.[/] What's done so far is saved; run [bold]daimon setup[/] "
                           "again to pick up where you left off.")
            return 1
        return 0

    def welcome(self) -> None:
        self.panel("[bold]Daimon[/] is a personal agent that keeps working in the background. Everything it is "
                   "(its instructions, memory, workers and settings) lives in markdown files in one folder, which "
                   "you and it can both edit.\n\nThis takes a few minutes. Use the arrow keys and Enter; Ctrl+C stops "
                   "at any point, and you can run [bold]daimon setup[/] again later to change anything.",
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

    def model(self) -> None:
        home = self.home
        self.step(2, "Model provider")
        current = home.worker("main").model
        p = PROVIDERS[self.ask.select("Which AI provider should the agent use?",
                                      [(v.title, k) for k, v in PROVIDERS.items()],
                                      default=next((k for k in PROVIDERS if current.startswith(k)), "anthropic"))]
        if p.env:
            self.api_key(p)
        self.step(3, "Main model")
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
                self.api_key(p, force=True)
            if again == "keep":
                break
        use_model(home, model)
        self.done.model = model
        self.ok(f"Main model: {model} (change it any time in workers/main.md, or ask the agent)")

    def api_key(self, p: Provider, force: bool = False) -> None:
        have = os.environ.get(p.env)
        if have and not force and self.ask.confirm(f"{p.env} is already set. Keep it?", True):
            return
        self.say(f"Get a key at [link={p.where}]{p.where}[/link]. It's typed hidden and saved to the secrets file.")
        key = self.ask.secret(f"{p.title} API key:")
        if key:
            self.home.set_secret(p.env, key)
            self.ok(f"Saved {p.env}")
        else:
            self.warn(f"No key given. Add {p.env}=... to {esc(str(self.home.secrets_file))} later.")

    def pick_model(self, p: Provider, current: str) -> str:
        models = list(p.models)
        if p.key == "ollama":
            while (local := ollama_models()) is None:
                self.warn(f"Ollama isn't answering at {OLLAMA}. Install it from https://ollama.com and start it.")
                if self.ask.select("What now?", [("Check again", "retry"), ("Continue anyway", "go")]) == "go":
                    local = []
                    break
            if local:
                models = local
                self.ok(f"Ollama is running with {len(local)} model(s)")
            elif local == []:
                self.say("[dim]No models pulled yet: run `ollama pull qwen3.5:9b` (or any model) before chatting.[/]")
        choices = [(m, m) for m in models]
        if current and current not in models and current.startswith(p.key):
            choices.insert(0, (f"{current} (current)", current))
        choices.append(("Type another model id", ""))
        model = self.ask.select("Main model:", choices, default=current if current in dict(choices).values()
                                else models[0])
        return model or self.ask.text("LiteLLM model id (provider/model):")

    def telegram(self) -> None:
        home = self.home
        self.step(4, "Telegram (optional)")
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
        self.step(5, "Claude Code")
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

    def addons(self) -> None:
        home = self.home
        self.step(6, "Add-ons")
        servers = home.mcp().get("mcpServers", {})
        picked = self.ask.checkbox("Add-ons to set up (Space to tick, Enter to go on):",
                                   [(f"{a.title}: {a.blurb}", a.key, a.key in servers) for a in addons.ADDONS.values()])
        for key in picked:
            a = addons.ADDONS[key]
            self.say(f"\n[bold]{a.title}[/]")
            if not addons.installed(a):
                while problem := addons.install(a):
                    self.warn(f"Installing {a.dist} failed: {esc(problem)}")
                    if self.ask.select("What now?", [("Try again", "retry"), ("Skip it", "skip")]) == "skip":
                        self.say(f"[dim]To retry later: {esc(' '.join(addons.install_command(a)))}[/]")
                        break
                if problem:
                    continue
            self.ok(f"{a.dist} installed")
            if addons.add_server(home, a):
                self.ok(f"Added the \"{a.key}\" server to mcp.json")
            else:
                self.say(f"[dim]mcp.json already has a \"{a.key}\" server; left as it is.[/]")
            if self.setup_addon(a):
                self.done.addons.append(key)

    def setup_addon(self, a: addons.Addon) -> bool:
        py = sys.executable
        if a.key == "canvas":
            argv, retry = [py, "-m", "daimon_canvas", "login"], "daimon-canvas login"
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
        self.step(7, "Keep it running")
        has_tg = bool(os.environ.get("TELEGRAM_TOKEN"))
        messages = "messages" in self.done.addons or "messages" in home.mcp().get("mcpServers", {})
        self.say("Daimon can start at login and keep working in the background: reminders, routines and tasks run "
                 "even with no terminal open.")
        if not has_tg:
            self.warn("Running in the background needs Telegram, so it can reach you and ask before acting. Run "
                      "`daimon setup` again after adding a bot.")
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
        lines = service.install(home, messages=messages, start_now=start, agent=has_tg)
        for line in lines:
            self.ok(esc(line))
        self.done.background = lines
        self.done.running = start
        if service.platform() == "linux":
            self.say("[dim]To keep it running while you're logged out: `loginctl enable-linger`.[/]")

    def close_messages(self) -> None:
        if self.messages_proc:
            self.say(f"[dim]The messaging service keeps running for now (pid {self.messages_proc.pid}). After a "
                     "restart, start it with `daimon-messages run`.[/]")

    def finish(self) -> None:
        home, d = self.home, self.done
        rows = [("Home folder", str(home.root)), ("Secrets file", str(home.secrets_file)),
                ("Main model", d.model or home.worker("main").model),
                ("Telegram", d.telegram or "not set up"),
                ("Claude Code", "ready" if d.claude else "not installed (optional)"),
                ("Add-ons", ", ".join(addons.ADDONS[k].title for k in d.addons) or "none"),
                ("Background", "running, starts at login" if d.running else
                 "starts at login" if d.background else "off")]
        from rich.console import Group
        from rich.table import Table
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(overflow="fold")
        for k, v in rows:
            grid.add_row(k, esc(v))
        if d.running and d.telegram:
            nxt = "Message your bot on Telegram: it's running now."
        elif d.running:
            nxt = "The messaging service is running. Run [bold]daimon chat[/] to talk to your agent."
        else:
            nxt = "Run [bold]daimon chat[/] to talk to your agent."
        self.panel(Group(grid, f"\n{nxt}\nChange anything later with [bold]daimon setup[/]; background control: "
                               "[bold]daimon service status|start|stop[/]."), "Ready", "green")


def run(home_flag: str | None = None) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("`daimon setup` asks questions, so it needs an interactive terminal. For scripts, use `daimon init`.")
        return 1
    if not shutil.which("git"):  # the home folder is a git repo: every change the agent makes is a commit
        print("Daimon needs git, which keeps the history of everything it changes. Install it, open a new terminal and "
              "run `daimon setup` again:\n  Windows: winget install Git.Git\n  macOS:   xcode-select --install\n"
              "  Linux:   your package manager, e.g. sudo apt install git")
        return 1
    try:
        ask = Ask()
        return Wizard(ask, home_flag=home_flag).run()
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "NoConsoleScreenBufferError":
            print("This terminal can't show the setup menus. Run `daimon setup` in Windows Terminal, PowerShell or "
                  "cmd (or prefix it with `winpty` in Git Bash).")
            return 1
        raise
