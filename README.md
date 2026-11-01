# Daimon

A personal agent that keeps working in the background. Everything it is (its instructions, memory, workers,
routines and settings) lives in markdown files in one folder that you and the agent both edit, so you change it by
talking to it. It reminds you of things, runs errands with workers, remembers what you tell it, and reaches you on
Telegram. Every change it makes to itself is a git commit, so "what did you change today?" and "undo that" just work.

In Greek myth a daimon is a guiding spirit that stays with you. In Unix it's a program that runs in the background.

## Install

**Windows** (PowerShell):

```powershell
irm https://github.com/LegoManACM/daimon/releases/latest/download/install.ps1 | iex
```

**macOS / Linux:**

```bash
curl -fsSL https://github.com/LegoManACM/daimon/releases/latest/download/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/), which brings its own Python, installs Daimon, then starts
`daimon setup`. To update later, run `daimon update` (or the same line again); your add-ons are kept.

You need:

- **git**, which keeps the history of everything the agent changes (`winget install Git.Git`,
  `xcode-select --install`, or your package manager). Setup tells you if it's missing.
- **A model.** An API key from Anthropic, OpenAI, OpenRouter or Google Gemini, or [Ollama](https://ollama.com)
  running locally for free.

`DAIMON_VERSION=X.Y.Z` installs a particular version, and `DAIMON_NO_SETUP=1` skips the wizard.

## Setup

`daimon setup` walks you through it a step at a time:

1. **Home folder**, where the agent lives (default `~/.daimon`).
2. **Knowledge**, where its notes live: its own folder (the default), or one you already have.
3. **Model.** Pick a provider (Anthropic, OpenAI, OpenRouter, Google Gemini or Ollama) and paste its key. The key
   shows as dots while you paste, then setup shows its first and last few characters and checks it with the
   provider. Then pick the main model from the provider's own current list: type to filter what's shown, or search
   all of them (OpenRouter has hundreds; try `sonnet`, `gpt` or `free`).
4. **Telegram** (optional). Make a bot with [@BotFather](https://t.me/BotFather), paste its token, then message
   the bot once; setup picks up your chat id itself.
5. **Claude Code** (optional). If `claude` is installed and logged in, workers can hand coding work to it.
6. **Add-ons** (optional): [Canvas](canvas/README.md) for your classes, [text messages](messaging/README.md)
   through Google Messages, and your [calendars](calendars/README.md) (Google, Outlook, iCloud, .ics). Setup installs them, connects them to the agent and runs their sign-in.
7. **Background.** Start Daimon at login so it keeps running after you close the terminal, and choose whether it
   installs new releases by itself.

Keys and tokens go in `~/.daimon-secrets.env`, outside the agent's folder.

### Changing things later

`daimon config` changes one part without going through setup again. With no argument it shows a menu; or name
the part:

```bash
daimon config model        # provider and main model
daimon config key          # replace an API key
daimon config knowledge    # where the notes live
daimon config telegram
daimon config addons       # Canvas, text messages
daimon config background   # start at login
daimon config updates      # whether it tells you about new releases or installs them
daimon config show         # what's set up now
```

On Windows, run setup in Windows Terminal, PowerShell or cmd. Git Bash's terminal can't draw the menus.

## Talking to it

```bash
daimon chat
```

`daimon chat` runs the agent with you at the terminal; background work (reminders, routines, workers) happens
while it's open. `daimon run` runs it headless and you reach it through the [web page](#the-web-page) or Telegram
instead. Only one of them can run per home, so while the background service is on, talk to it there:
`daimon web` opens the page.

When a tool needs approval you get `[y]es / [n]o / [a]lways / or type a reply`. A typed reply declines the action
and passes your words back to the agent. With Telegram connected, approvals show up there too as buttons;
whichever you answer first wins.

Chat commands: `/tasks`, `/kg QUERY`, `/tools`, `/history`, `/cost`, `/compact`, `/clear`, `/exit`.

### Things to try

- "Stop using bullet points." The agent edits its SELF.md and commits the change.
- "Remind me in 20 minutes to stretch." It schedules a wake; the reminder arrives while the agent runs.
- "Every Sunday at 6pm, review my week." It writes a routine in `routines/`.
- "My sister is vegetarian. Remember that." It saves a note in `knowledge/`. Relevant notes are retrieved into
  every prompt, so it knows later, even in a fresh conversation.
- "Make a researcher worker and have it look into X." It creates the worker, queues a task, and reports back when
  the worker finishes.
- "Connect the filesystem MCP server for my Documents folder." It edits mcp.json (asking first, since mcp.json is
  protected by default), and the tools appear on its next step.
- "Make sending messages ask me first" or "...not ask me anymore." It sets the tool's consent level.
- "What did you change today?" / "Undo that."

## Running in the background

```bash
daimon service install --start   # start at login, and now
daimon service status
daimon service stop | start | restart
daimon service uninstall
```

`daimon service install` needs the web page or Telegram, since that's how you reach a headless agent. It also runs the text
messages service when that add-on is connected. Logs go to `~/.daimon/.cache/daimon.log`.

| | How it starts |
|---|---|
| Windows | Task Scheduler tasks "Daimon" and "Daimon Messages" at logon, in a hidden window (or the Startup folder if Task Scheduler refuses) |
| macOS | LaunchAgents `com.daimon.agent` and `com.daimon.messages` in `~/Library/LaunchAgents` |
| Linux | systemd user units `daimon.service` and `daimon-messages.service`. Run `loginctl enable-linger` to keep them running while you're logged out |

## The web page

The running agent serves a page for chatting with it and watching it work, from any browser, phone or computer.
Next to the conversation you see what each worker is doing right now (thinking, writing, which tool it's running,
tokens per second, the end of what it's writing), background jobs and what's coming up. Every reply shows the tool
calls behind it; tap one for its result. Other views: Activity (every tool call, notice and error as it happens),
Tasks (go / skip proposals, answer a worker's question, start or cancel tasks), Schedule, Changes (the agent's own
commits, each with its diff and an Undo), Knowledge and the agent's files. The pills at the top open too: status and
pause, the main model (pick another from models.yaml), today's spend by model and by worker, everything waiting on
you, and how the page is reached. Phone and computer get the same features in their own layout; on a phone, add it
to the home screen.

Approvals show up on the page as cards and are asked on Telegram at the same time; whichever you answer first wins.

A new agent has it on at <http://localhost:8770>, this computer only, served while the agent runs. `daimon web`
opens it in your browser, already signed in; `daimon web --link` prints a link that signs in another device, like
your phone. `daimon config web` changes where it's reachable and who gets in. Two settings in config.yaml's `web:` section:

| `listen` | Reachable from |
|---|---|
| `localhost` | this computer only (default), and anything you run here that forwards to it: Caddy, nginx, Cloudflare Tunnel |
| `tailscale` | your tailnet: it listens on this computer's Tailscale address |
| `all` | every network this computer is on: your home Wi-Fi, a VPN |
| an IP address | just that address of this computer |

| `auth` | Who gets in |
|---|---|
| `token` | each browser signs in once with `WEB_TOKEN` from the secrets file (made for you on first start). The default, except on Tailscale |
| `tailscale` | only devices logged in to your own Tailscale account, checked with `tailscale whois`. No password |
| `none` | anyone who reaches it. Only with `listen: localhost`, or with `allow_open: true` when something in front of it already checks |
| `package.module:Class` | your own check, a subclass of `daimon.web.access.Auth` |

```yaml
web:
  enabled: true
  listen: tailscale     # or localhost, all, 192.168.1.20
  port: 8770
  auth: tailscale       # or token, none
  behind_proxy: false   # true behind a reverse proxy, so it sees https and the real addresses
```

Behind a reverse proxy or tunnel, keep `listen: localhost` with `auth: token` (or your proxy's own login and
`auth: none`), and set `behind_proxy: true`. Anything that changes something needs a header only the page itself
sends, so another site can't act through your signed-in browser.

## What it sees each turn: CONTEXT.md

The agent's standing prompt (SELF.md, POLICY.md, MEMORY.md, its worker file) stays the same from step to step, so
providers can cache it and a local model keeps it loaded. Anything that changes by itself goes in `CONTEXT.md`
instead: it's rendered once when a message arrives and put in front of that message. Each message is also stamped
with when it arrived, so the agent knows the time and how long ago things were said.

`CONTEXT.md` is markdown with `{tags}`, and you and the agent can both edit it. The file explains every tag in a
comment at the top. In short:

```markdown
{now}

## Open tasks
{tasks}

## Today's calendar
{tool:calendar__calendar_events start=today days=1 | every=10m max=2000}

## Texts from the last 6 hours
{tool:messages__recent_messages since=6h incoming_only=true | every=5m}

## Relevant knowledge
{knowledge}
```

`{tool:...}` runs any tool the agent has whose consent level is `auto` (read-only lookups). `every=` reuses a result
for a while instead of fetching it every turn, and `max=` caps its size. A tag that fails says so in place and never
stops the turn. Other tags: `{projects}`, `{spend}`, `{jobs}`, `{events:messages.*}`, `{file:notes/today.md}`. A
worker can have its own file: add `context: contexts/<name>.md` to its frontmatter.

## Slow models and long tool calls

Local models are slow to start: Ollama loads the model into memory before its first answer, which can take
minutes, and by default it unloads the model again after 5 idle minutes. Daimon handles this:

- **Generous, two-part timeouts.** A call waits up to 300s for the first word (that covers loading), then 120s for
  any silence after that, and retries twice. If it still fails, the error says why: Ollama isn't running, the model
  isn't pulled, or it was still loading.
- **Ollama stays loaded** for 30 minutes after each use, and gets a 16k context window. Ollama's default window is
  small, and it silently cuts any prompt that's longer.
- **Warm-up.** The main agent's local model starts loading when Daimon starts and at the start of each turn. A
  text watcher's model starts loading as soon as a text is waiting to be read, so it's ready by the time the chat
  goes quiet.
- **Background jobs.** A tool call still running after 30s becomes a job: the agent hears its job id right away,
  keeps working, and gets the result when it's done (in the same turn, or as a new message). A call waiting for
  your approval counts too. The agent can also start a job itself (`start_job`), and wait for or cancel one
  (`job_wait`, `job_cancel`).
- **Prompt caching.** For Claude, the standing prompt and the conversation are marked for caching. Other providers
  cache a repeated prefix on their own.

All of it is in config.yaml (`model_calls:` and `tools:`, explained in comments there). For example, on a slow
computer:

```yaml
model_calls:
  first_token_timeout_s: 600
  ollama:
    keep_alive: -1      # never unload
    num_ctx: 8192       # less memory
```

The watchers have their own `timeout_seconds` (default 300) in the messages add-on's watch settings.

## Knowledge

What the agent learns (people, projects, preferences, plans) goes into markdown notes with YAML frontmatter, linked
to each other with ordinary markdown links. Notes relevant to each message are retrieved into the prompt, and the
agent searches, reads and writes them with its `kg_*` tools.

By default they live in `knowledge/` inside the agent's folder. Point `knowledge.folder` in config.yaml (or
`daimon config knowledge`) at any other folder to use notes you already have, like an Obsidian vault, or to
share one knowledge base between several agents so none of them starts from scratch. Each agent keeps its own
search index, built from the files. Inside the agent's folder its edits are commits you can undo; elsewhere they're
committed if that folder is a git repo of its own.

## Feeding it

Other programs (an email script, a webhook) drop markdown files into `~/.daimon/inbox/`, or:

```bash
daimon inbox add "lands 6:40pm on the 14th" --source sms --from Sam
daimon inbox add "package at the door" --for main   # straight to the main agent
```

The curator worker files what's worth keeping into the knowledge graph, with the source recorded on each note.
Set `knowledge.embeddings` in config.yaml (e.g. `ollama/qwen3-embedding:0.6b`) to add meaning-based search.

## Harnesses: Claude Code and other agents

A worker can run its tasks in an external agent instead of the agent's own loop. Claude Code is built in and runs
on your Claude subscription (it needs `claude` installed and logged in):

```markdown
---
type: worker
model: sonnet
harness: claude-code
repo: ~/code/my-app             # optional: a git worktree of it (branch daimon/<project or task>); a plain
                                #   folder without git is worked in directly
harness_options: { max_turns: 60 }
---
You are the coding worker. Commit your work on the branch and report what changed.
```

A harness worker's `model` is the harness's own model name (`opus`, `sonnet`, `haiku` for Claude Code), not a
models.yaml entry. The harness is the provider, so Claude Code models always run on your subscription and never
through OpenRouter or an API key. Restrict or gate them per harness in config.yaml, e.g.
`models: [sonnet, {id: opus, approval: ask}]`.

Or just ask: "Make a coder worker that uses Claude Code on ~/code/my-app." Claude Code gets the brief, the retrieved
notes and a `daimon` MCP server with `kg_search`, `ask_main`, `report_progress` and `update_project_notes`. It works
freely inside its workspace. Pushing, opening PRs, publishing, writing outside the workspace and its other MCP
servers ask you first (change this with `consent:` rules). With `auth: subscription` the API key is hidden from it,
so runs count against your plan; `limits.harness` caps runs per day, and a rate-limited task resumes in the same
session when the limit resets.

Other agents (Codex, Gemini CLI, anything with a headless mode) are added under `harnesses:` in config.yaml with
`driver: cli` and command templates, or `driver: package.module:Class` for your own `daimon.harness.Driver`.

### Projects

For work that spans days and many dispatches: "Start a project for the my-app redesign, using the coder." A project
(`projects/<name>.md`) keeps one workspace, the harness session to resume, handoff notes and a log. Every task with
`project: <name>` continues from where the last one stopped, one at a time, whether you queue it, schedule it for
tomorrow, or a routine fires it (`project:` in the routine's frontmatter). If a session can't be resumed, a fresh
one starts from the notes and log. Pause or finish a project and its queued tasks wait.

## Telegram by hand

`daimon setup` does this for you. To do it yourself:

1. Make a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and put its token in the secrets file:
   `TELEGRAM_TOKEN=...`
2. Run `daimon run` (or `daimon chat`) and send the bot any message. It replies with your chat id.
3. Add `TELEGRAM_CHAT_ID=...` to the secrets file and restart. The bot answers only that chat.

Replies, background results, approvals (Yes / No / Always buttons, or reply with text to decline and say why) and
`notify`/`error` notices go to Telegram. Change which notices with `channels.telegram.events` in config.yaml, or
turn it off with `enabled: false`.

## Files

| Where | What |
|---|---|
| `~/.daimon/` | the agent: SELF.md, POLICY.md, MEMORY.md, CONTEXT.md, workers/, routines/, knowledge/ (unless it lives elsewhere), mcp.json, models.yaml, config.yaml (a git repo) |
| `~/.daimon-secrets.env` | API keys and tokens, never committed |
| `~/.daimon-canvas/`, `~/.daimon-messages/`, `~/.daimon-calendar/` | the add-ons' own folders |

`--home` or `DAIMON_HOME` points at a different agent folder; `DAIMON_SECRETS` at a different secrets file.
`daimon init [--model ID]` makes the files without the wizard, for scripts.

## Updating

```bash
daimon update           # asks first, then installs the newest release
daimon update --check   # just says whether there's one
daimon --version
```

The add-ons you have are updated with it, and the background service is stopped for the update and started again
after. While it runs, Daimon also looks for a new release once a day and tells you (on Telegram and in
`daimon chat`). Turn on `updates.auto` (`daimon config updates`) and the background agent installs new releases by
itself when it's idle; it's off by default.

## Uninstall

```bash
daimon uninstall
```

It lists what it's about to remove and asks first: starting at login, the agent's folder (`~/.daimon`), the
secrets file, the add-ons' folders and the program itself. A knowledge folder that lives outside the agent's
folder is kept. `--yes` skips the question. If you paired text messages, also remove the computer in Google
Messages > Device pairing.

## Development

```bash
git clone https://github.com/LegoManACM/daimon && cd daimon
uv venv --python 3.12
uv pip install -e . -e canvas -e messaging -e calendars
uv run pytest tests canvas/tests messaging/tests calendars/tests
```

From a checkout, `daimon setup` installs add-ons editable from `canvas/`, `messaging/` and `calendars/` instead of downloading
them. [DESIGN.md](DESIGN.md) has the design, and [RELEASING.md](RELEASING.md) how releases are made.

```
daimon/cli.py        daimon setup / config / init / chat / run / service / inbox, the terminal channel
daimon/setup/        the setup wizard and config, providers and their model lists, add-on installs,
                     start-at-login per OS
daimon/home.py       home folder, frontmatter, secrets, git
daimon/gate.py       consent levels (auto / notify / ask)
daimon/mcp_hub.py    MCP connections + per-tool consent
daimon/selftools.py  tools the agent uses to change itself
daimon/runner.py     the agent loop (LiteLLM), main turns and worker tasks
daimon/agent.py      inbox, scheduler, worker pool
daimon/tasks.py      task files; daimon/tasktools.py: task tools for main and workers
daimon/schedule.py   routine schedules
daimon/knowledge.py  notes, SQLite index, search, retrieval, kg_* tools
daimon/inbox.py      inbox drop folder
daimon/thread.py     main conversation + compaction
daimon/gateway.py    channel contract, approvals across channels, `daimon run` log
daimon/telegram.py   Telegram channel (Bot API long polling, inline-button approvals)
daimon/web/          the web page: channel.py (live updates), server.py (routes), access.py (where it listens,
                     who gets in), data.py (what the page reads), static/ (the page itself, no build step)
daimon/projects.py   projects: workspace, session, handoff notes and log across tasks
daimon/harness/      external agents: driver interface + registry, sessions, tools, consent, limits, workspaces
daimon/templates/    starter files for a new home
canvas/              daimon-canvas, the Canvas add-on
messaging/           daimon-messages, the text messages add-on (Python service + Go link to Google Messages)
calendars/           daimon-calendar, the calendar add-on (Google, Microsoft, CalDAV, .ics)
```

## License

[MIT](LICENSE)
