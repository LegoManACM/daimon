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
`daimon setup`. Run the same line again to upgrade; your add-ons are kept.

You need:

- **git**, which keeps the history of everything the agent changes (`winget install Git.Git`,
  `xcode-select --install`, or your package manager). Setup tells you if it's missing.
- **A model.** An API key from Anthropic, OpenAI, OpenRouter or Google Gemini, or [Ollama](https://ollama.com)
  running locally for free.

`DAIMON_VERSION=X.Y.Z` installs a particular version, and `DAIMON_NO_SETUP=1` skips the wizard.

## Setup

`daimon setup` walks you through it a step at a time:

1. **Home folder**, where the agent lives (default `~/.daimon`).
2. **Model.** Pick a provider, paste the key, pick the main model. It checks the key with a tiny request.
3. **Telegram** (optional). Make a bot with [@BotFather](https://t.me/BotFather), paste its token, then message
   the bot once; setup picks up your chat id itself.
4. **Claude Code** (optional). If `claude` is installed and logged in, workers can hand coding work to it.
5. **Add-ons** (optional): [Canvas](canvas/README.md) for your classes and [text messages](messaging/README.md)
   through Google Messages. Setup installs them, connects them to the agent and runs their sign-in.
6. **Background.** Start Daimon at login so it keeps running after you close the terminal.

Run `daimon setup` again any time to change something; what's there is kept. Keys and tokens go in
`~/.daimon-secrets.env`, outside the agent's folder.

On Windows, run setup in Windows Terminal, PowerShell or cmd. Git Bash's terminal can't draw the menus.

## Talking to it

```bash
daimon chat
```

`daimon chat` runs the agent with you at the terminal; background work (reminders, routines, workers) happens
while it's open. `daimon run` runs it headless and you reach it through Telegram instead. Only one of them can run
per home, so while the background service is on, talk to it on Telegram.

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
daimon service stop | start
daimon service uninstall
```

`daimon service install` needs Telegram set up, since that's how you reach a headless agent. It also runs the text
messages service when that add-on is connected. Logs go to `~/.daimon/.cache/daimon.log`.

| | How it starts |
|---|---|
| Windows | Task Scheduler tasks "Daimon" and "Daimon Messages" at logon, in a hidden window (or the Startup folder if Task Scheduler refuses) |
| macOS | LaunchAgents `com.daimon.agent` and `com.daimon.messages` in `~/Library/LaunchAgents` |
| Linux | systemd user units `daimon.service` and `daimon-messages.service`. Run `loginctl enable-linger` to keep them running while you're logged out |

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
| `~/.daimon/` | the agent: SELF.md, POLICY.md, MEMORY.md, workers/, routines/, knowledge/, mcp.json, models.yaml, config.yaml (a git repo) |
| `~/.daimon-secrets.env` | API keys and tokens, never committed |
| `~/.daimon-canvas/`, `~/.daimon-messages/` | the add-ons' own folders |

`--home` or `DAIMON_HOME` points at a different agent folder; `DAIMON_SECRETS` at a different secrets file.
`daimon init [--model ID]` makes the files without the wizard, for scripts.

## Uninstall

```bash
daimon service uninstall
uv tool uninstall daimon
```

Then delete `~/.daimon`, `~/.daimon-secrets.env`, and `~/.daimon-canvas` / `~/.daimon-messages` if you used the
add-ons.

## Development

```bash
git clone https://github.com/LegoManACM/daimon && cd daimon
uv venv --python 3.12
uv pip install -e . -e canvas -e messaging
uv run pytest tests canvas/tests messaging/tests
```

From a checkout, `daimon setup` installs add-ons editable from `canvas/` and `messaging/` instead of downloading
them. [DESIGN.md](DESIGN.md) has the design, and [RELEASING.md](RELEASING.md) how releases are made.

```
daimon/cli.py        daimon setup / init / chat / run / service / inbox, the terminal channel
daimon/setup/        the setup wizard, add-on installs, start-at-login per OS
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
daimon/projects.py   projects: workspace, session, handoff notes and log across tasks
daimon/harness/      external agents: driver interface + registry, sessions, tools, consent, limits, workspaces
daimon/templates/    starter files for a new home
canvas/              daimon-canvas, the Canvas add-on
messaging/           daimon-messages, the text messages add-on (Python service + Go link to Google Messages)
```

## License

[MIT](LICENSE)
