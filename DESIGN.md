# Daimon: design draft

A background agent that works on your behalf. It is driven entirely by markdown that you and the agent both
edit, reaches the world through MCP servers, and hands work to user-defined workers that can run on any
model provider.

## Core principle: you change it by talking to it

With most agents, changing behaviour means editing code or config. Here you ask the agent itself:
"stop scheduling things before 10", "make a research worker on Gemini", "connect my Outlook but only let it
draft", "check my grades every Sunday". It makes the change itself, then tells you what it changed.

- **Every behaviour knob is a file the agent can edit:** SELF, POLICY, MEMORY, workers, skills, MCP
  servers and their consent levels, the nightly time, wakes. If a request can't be met by editing a file,
  that is a design bug: move the logic out of Python and into a file.
- **The Python core stays small and boring:** the loop, the gate, the scheduler, the adapters. It holds no
  opinions about how the agent should behave.
- **Built-in self-management tools:** `edit_agent_file`, `add_mcp_server`, `set_tool_consent`,
  `create_worker`, `schedule_wake`, `reload`. Changes take effect on hot reload, with no restart.
- **Every change is a git commit with a one-line reason**, so "undo what you changed yesterday" and "what
  did you change this week?" are ordinary requests.
- **The consent gate still applies** (see `self_edit` below). By default it changes itself freely and
  asks only before loosening its own rules or connecting new tools.
- **Possible later stage:** the agent edits its own Python core through its coder worker on its own
  repo, tests the change, and restarts. Not in v1.

## Keeping it simple

Agents tend to grow convoluted: too many subsystems, config and state spread around, behaviour hardcoded in
code, and heavy approval machinery. Rules for this one:

- **Five parts, no more** (below). Anything else is a plugin, an MCP server, or a separate process.
- **One home folder holds all state.** Everything in it is markdown/YAML except a rebuildable cache.
- **Behaviour lives in markdown, not Python.** If you'd want to tweak it, it's a file the agent reads.
- **One consent mechanism** covers MCP tools, self-edits and everything else.

## The five parts

```
            channels (Telegram, Discord, web UI, CLI)
                              |
                        [1] Gateway  ── Message / Prompt envelopes
                              |
 external processes ──> [2] Inbox ──> main thread <── worker reports
 (messaging curator,          |            |
  webhooks, MCP events)  [3] Scheduler     | dispatch
  nightly, wakes, due tasks|        [4] Runner (agent loop: LiteLLM + MCP client + consent gate)
                              |            |
                         [5] Knowledge (OKF files + SQLite index)
```

1. **Gateway.** Channel adapters behind a tiny contract (`connect`, `send(text)`, `event`, `prompt(Prompt)`,
   inbound `Message` events), the same pattern OpenClaw and Hermes use. A `Prompt` is text plus options;
   each adapter renders it as buttons or cards, or falls back to "reply 1 or 2". Adapters run in-process or
   as separate processes over a local socket.
2. **Inbox.** One queue of items for the main agent: user messages, curated facts from external
   ingestors, worker reports, webhook and MCP events. External processes such as the messaging
   curator write here through a small local API and never into `knowledge/` directly.
3. **Scheduler.** Fires routines (`routines/*.md`, including the nightly run), wakes the agent scheduled, and
   tasks that come due. A wake is simply a task for `main` with a `run_at`. Routines are behaviour (committed,
   undoable); tasks are state (git-ignored).
4. **Runner.** One agent loop, used by the main agent and by every worker: model → tool calls → MCP →
   repeat. It's multi-provider via LiteLLM. Every tool call passes through the consent gate. A worker can
   opt into an external harness (`harness: claude-code`) as an escape hatch.
5. **Knowledge.** An OKF bundle (`knowledge/`) is the source of truth. A SQLite cache (full-text,
   embeddings, link table) is rebuilt from the files and can be deleted at any time.

## Home folder

```
$DAIMON_HOME/                  # git repo; every agent edit is a commit
  config.yaml              # providers, channels, nightly time, consent mode, memory cap, limits
  models.yaml              # the models the agent may use, with tiers (user-owned)
  mcp.json                 # standard mcpServers block + per-tool consent levels
  SELF.md                  # who the agent is, how it behaves, how proactive to be
  POLICY.md                # soft rules (prompting): "drafts ok, sending to strangers needs ok"
  MEMORY.md                # capped (e.g. 1.5k tokens), loaded everywhere, rewritten nightly
  workers/
    main.md                # the main agent is just a worker with a special role
    coder.md  scheduler.md ...
  skills/<name>/SKILL.md   # Agent Skills format
  WORKER.md                # instructions every worker gets
  routines/<name>.md       # recurring work: schedule + instruction (behaviour, committed)
  tasks/<date>-<slug>.md   # work items and one-off wakes (state, git-ignored)
  knowledge/               # OKF bundle (index.md, log.md, people/, projects/, ...)
  threads/main/            # main conversation log + compaction summaries
  .cache/                  # sqlite index, sessions; gitignored, rebuildable

$DAIMON_HOME/../.daimon-secrets.env   # NOT in git: API keys, tokens, vault key
$DAIMON_HOME/../.daimon-vault         # NOT in git: encrypted saved logins
```

### Worker file

```markdown
---
type: worker
model: anthropic/claude-sonnet-5-5
fallback: [openai/gpt-5]
mcps: [github, filesystem]
skills: [git-workflow]
harness: native            # or claude-code / codex
---
You are the coding worker. ...
```

### Task file

```markdown
---
type: task
status: proposed           # proposed > queued > running > waiting > done | failed | cancelled
worker: scheduler
created_by: agent          # agent | user | worker:<name>
run_at: 2026-10-26T09:00
due: 2026-10-28
parent: null
---
## Brief
Reschedule dentist to next week, afternoons only.
## Log
- 00:04 found 3 slots
- 00:05 asked main: Tue 3pm or Wed 10am?
## Result
```

## Behaviour

- **Main thread.** A single long-running conversation. Every inbox item arrives there as a message. It
  is compacted when it grows past a threshold, and compaction summaries stay in the context.
- **Context for every call:** `SELF.md` + `POLICY.md` + `MEMORY.md` + the worker prompt + skills
  + (for workers) the task brief + notes retrieved for the task (search + 1–2 link hops). Every worker
  also gets `kg_search`, `kg_read` and `kg_write` tools.
- **Workers talk only to main.** They report results or raise questions; main answers from what it knows
  or relays the question to you. You hear one voice.
- **Initiative.** Tasks the agent creates itself start as `proposed`; tasks you assign and routine upkeep
  run directly. A morning digest lists proposals with go/skip. This is set in `SELF.md`/`POLICY.md`, not
  in code.
- **Nightly run.** Compacts the main thread, rewrites `MEMORY.md` within its cap, curates the inbox
  into knowledge, reviews tasks, schedules tomorrow's wakes, and prepares the morning digest.

## Consent: never "no", only "waiting on you"

- Every tool, whether from MCP or built in, has a level: `auto`, `notify` (act, then tell) or `ask`
  (pause the task, send a `Prompt`, resume on approval). Defaults come from MCP tool annotations: read-only
  is `auto`, everything else is `ask`. You override per tool or with globs in `mcp.json`.
- The agent is told each tool's level, so it plans around it ("queued the purchase for your OK") instead
  of failing.
- Self-edits are just the built-in file tool with path rules, so one mechanism covers them. The
  `self_edit` mode in `config.yaml` sets the preset:
  - `policy` (default): edits to workers, skills, memory and knowledge are `auto`; `POLICY.md`,
    `mcp.json`, `models.yaml` and `config.yaml` are `ask`.
  - `free`: everything is `auto`.
  - `all`: every agent file edit is `ask`.
- Finer rules ("replies to Mom: just send") live in `POLICY.md` as prompting.

## Harness workers (Claude Code, Codex, Cursor, Gemini CLI, your own)

A worker with `harness: <name>` runs its task in an external agent instead of the native loop. The harness layer
(`daimon/harness/`) is split so no agent is hardcoded:

- **Drivers** know how to talk to one kind of agent. A driver gets a `Job` (prompt, instructions, workspace,
  session to resume, our tools, a consent callback) and returns an `Outcome` (final message, session id, cost,
  error, rate-limit reset). Built in: `claude-sdk` (Claude Code through the Claude Agent SDK) and `cli` (any
  headless command line, configured with argv templates and output parsing). `package.module:Class` loads your
  own `Driver` subclass.
- **Harnesses** are named entries in config.yaml (`harnesses:`), each a driver plus options. `claude-code` is
  built in. A worker picks one and can override options with `harness_options`.
- **The shared run** (`run.py`) does everything else the same way for every harness:
  1. **Workspace.** A project's persistent workspace, or one per task: an existing `folder`, a git worktree
     of `repo` on branch `daimon/<name>`, or a scratch git folder.
  2. **Session.** The harness gets the brief, the worker prompt, the project's goal/notes/log and the retrieved
     notes. Drivers that can lend tools give it an MCP server of ours: `kg_search`, `kg_read`, `kg_write`,
     `ask_main`, `report_progress`, `update_project_notes`, each still behind its consent level. Drivers that
     can't use `QUESTION:` / `NOTES:` lines at the end of the reply instead.
  3. **Resume.** The session id is kept on the task (for answers and restarts) and on the project (for the
     next task). A session that can't be resumed is replaced by a fresh one given a recap.
  4. **Events** (tool use, progress) go to the surface and the task log. On a question the task goes to
     `waiting` and resumes in the same session with the answer.
  5. **Result.** Summary plus workspace, branch, commits and uncommitted changes go into the task and the
     project log, and main decides what's next.

The harness runs freely inside its workspace; actions that leave it (push, PR, publish, deploy, writes
elsewhere, its own MCP servers) go through consent rules (`Tool(argument)` globs) for drivers that can
intercept them. `auth: subscription | api_key` is set per harness. With a subscription (as of 2026-10), the SDK
counts against your plan limits, shared with your interactive use, so `ANTHROPIC_API_KEY` is hidden from the
CLI, harness workers obey `limits.harness.<name>.max_runs_per_day`, and a rate-limited task is re-queued for
the reset time and resumes in the same session.

Not built yet: ACP (Agent Client Protocol) as a driver, lending our tools to CLI harnesses (over a local HTTP MCP
server), `prefer_hours`, and falling back to another worker on a rate limit.

## Add-ons and reactions

Add-ons (messages, calendar, Canvas, ...) are separate programs that never import, name or configure each other.
Each one does two things:

- **Tools (verbs).** An MCP server with plain argument schemas a rule can fill without a model (ISO times, flat
  strings), accurate `readOnlyHint` (consent defaults come from it), plain-text errors, and an optional
  caller-chosen `source` key on anything that creates things, so a repeat updates instead of duplicating.
- **Events (facts).** What it notices on its own goes to the inbox as a file whose frontmatter has
  `event: <addon>.<name>` and a flat `data:` dict, plus a human-readable body. `for: main` is only a fallback
  hint. Add-ons never decide what happens next.

How events become actions lives in the agent's home, as files it writes itself: `reactions/<name>.md` matches an
event (`on`, `when`) and calls one tool with `args` filled from the event's data (`{field}`, `{field?}`), through
the consent gate as usual. A reaction that claims an event keeps it away from main, so routine plumbing costs no
model calls. Failures (a missing field, a tool error) go to main with the error. Successes emit `reaction.done`,
so reactions chain, and routines emit `routine.<name>` (with `worker: none` they do nothing else). That covers the
whole range: a rule for mechanical mappings, `create_task` to a cheap worker when some judgment is needed, and main
for anything else. Reactions can call MCP tools, task tools and knowledge tools, but never self-edits, because event
data comes from other people. `recent_events` shows events with their fields and what handled them, and
`preview_reaction` dry-runs a draft against the latest matching event.

```markdown
---
on: messages.watch_hit
when: {watcher: plans}
call: calendar__calendar_add_event
args: {title: "{title}", start: "{start}", end: "{end?}", location: "{location?}",
       source: "sms:{chat_id}:{message_ids.0}"}
then: notify
---
Plans made in texts go on the calendar (the add still asks unless the user loosened it).
```

**How the agent learns all this.** Its prompt carries only an index: each connected add-on's overview (its MCP
server `instructions`), the add-ons not connected yet, and one line per guide. `guide(topic)` returns the full text
on demand. The built-in guides (`daimon/guides/*.md`: reactions, consent, models, workers, routines, connections,
config, files) ship with the code, each with rules and worked examples. A test parses every reaction example, so
the guides can't drift from the code. An add-on's guide is its instructions plus its tools and their consent
levels. Add-ons end their instructions with an `Events:` line and the list of events and fields; that part stays
out of the prompt and is served only by its guide.

Extracting fields is the emitter's job (a messages watcher's `fields: {start: "ISO 8601 local datetime"}`, filled by
its own small model), so nothing between the add-ons needs a model to reshape data.

Events the add-ons emit (times are ISO 8601 local, `2026-10-30T23:59`; empty fields are left out):

- `messages.watch_hit`: a text watcher fired. `watcher`, `chat_id`, `chat`, `from`, `summary`, `message_ids`,
  `received`, plus the watcher's own extracted fields.
- `canvas.assignment_due`: work not yet done came within `watch_days` (14) of its due date; once per item.
  `course`, `course_id`, `assignment_id` (the canvas_open id, `assignment:9:1`, `quiz:10:2`: stable, so
  `source: "canvas:{assignment_id}"` makes a calendar add an upsert), `kind`, `title`, `due`, `due_date`, `url`,
  `points`.
- `canvas.assignment_changed`: something already reported got a new due date, title or points. The same fields, plus
  `changed` (a list of due/title/points), `previous_due`, `previous_title`.
- `canvas.grade_posted`: a grade was released or changed. `course`, `course_id`, `assignment_id`, `title`,
  `score`, `grade`, `points`, `url`, `graded_at`.
- `canvas.announcement_posted`: a class posted an announcement. `course`, `course_id`, `announcement_id`, `title`,
  `author`, `posted_at`, `url`, `text` (plain, clipped to 1500 characters).
- `canvas.inbox_message`: someone else wrote to the student in the Canvas inbox. `conversation_id`, `message_id`,
  `subject`, `course`, `from`, `sent_at`, `text`, `files`, `url`. Nothing is marked read.
- `canvas.submission_comment`: a teacher or TA commented on submitted work. `course`, `course_id`,
  `assignment_id`, `title`, `comment_id`, `from`, `posted_at`, `text`, `url`.
- `canvas.module_unlocked`: a module opened (it was locked, or was just published). `course`, `course_id`,
  `module_id`, `module`, `items`, `url`, `new`.
- `canvas.course_event`: a class event that isn't an assignment (lecture, exam, trip) within `watch_days`.
  `course`, `course_id`, `event_id` (`event:50`, stable), `title`, `start`, `end` (dates when `all_day`), `all_day`,
  `date`, `location`, `url`, `description`.
- `canvas.course_event_changed`: one already reported changed. The same fields, plus `changed` (title, start, end,
  location), `previous_start`, `previous_title`.

Canvas events come from `daimon-canvas watch --every 30`, which setup runs at login beside the agent; what it has
already reported is in its own home (`watch.json`), so a restart doesn't repeat anything. Upcoming work and class
events are reported from the first pass; grades, announcements, messages, comments and modules start from the first
pass that covers them, so turning the watcher on doesn't replay history. Each source keeps its own checkpoint: one
that fails (a school that turned the inbox off) is retried next time without holding up the others.

## Projects

Long-running work that spans many tasks and days. `projects/<name>.md` (state, git-ignored) holds the goal,
handoff notes (rewritten by whoever worked on it last), a log of every dispatch, the workspace and the harness
session per worker. Tasks join with `project:`; only one runs per project at a time, since they share a
workspace and session. Paused projects hold their queued tasks. Routines can fire into a project. Native workers
in a project get the same context and `update_project_notes`.

## Models

`models.yaml` is the catalog of models the agent may use. `create_worker` and any model change are
validated against it.

```yaml
default_for_new_workers: anthropic/claude-sonnet-5-5
models:
  - id: anthropic/claude-haiku-5-5
    tier: cheap
    good_for: [curation, triage]
  - id: anthropic/claude-sonnet-5-5
    tier: standard
    good_for: [agentic, scheduling]
  - id: anthropic/claude-fable-5-1
    tier: premium
    approval: ask          # every assignment needs your OK
    max_usd_per_task: 3
```

- Models not in the catalog can't be used.
- `approval: ask` models need your OK each time they're assigned to a worker or task.
- A worker can request a one-off upgrade for a single task, which also goes through `ask`.
- The agent can't edit `models.yaml` without your OK in every `self_edit` mode except `free`.

## Limits

```yaml
limits:
  max_concurrent_workers: 3        # or unlimited
  daily_budget_usd: 5              # or unlimited
  on_budget: ask                   # ask | wait | continue
  harness:
    claude-code: { max_runs_per_day: 6, prefer_hours: "00-07" }
```

Spend is tracked per task and worker from LiteLLM's cost data. Remaining budget is shown to main in
context so it can prioritise. These caps sit on top of the spend limits you set with each provider.

## Secrets and saved logins

- **`secrets.env`** (outside the repo) holds API keys and tokens. Config refers to them by name
  (`${TELEGRAM_TOKEN}`). The agent can use them but never read them.
- **The vault** is an encrypted file (key in `secrets.env`) for logins. The agent can write to it
  (`secret_save`, `secret_generate` for new accounts) but can never read values back.
- **Placeholders.** The agent passes `{{secret:amazon}}` to tools. The runtime substitutes the real
  value at call time and redacts it from tool output and logs.
- **Domain binding.** Each entry lists `domains`, and a browser tool only gets the value on a matching
  origin, so a prompt injection can't send it elsewhere.
- **Consent.** Each entry has its own consent level for use (`auto` / `notify` / `ask`).
- **Adding values yourself.** `daimon secret set amazon` keeps the value out of the model's context entirely.
  Pasting it in chat works, but then the value has passed through the model once.
- **Backends are pluggable**, so Bitwarden or 1Password can be added later.

## Stack

Python 3.11+, portable (Windows now, Linux/Docker later). LiteLLM for providers, the official MCP Python
SDK, SQLite for the cache. Embeddings are your choice (`none` (default), `local:<model>` or a provider
model); search is full-text + links, plus vectors (sqlite-vec) when enabled. Channels in order: CLI,
Telegram/Discord, then web UI. Until the web UI, Obsidian or VS Code on the home folder is
the dashboard.

## Build order

1. Home folder, runner (LiteLLM + MCP + consent gate), CLI chat with main.
2. Tasks, workers, scheduler (nightly run + wakes).
3. Knowledge: OKF read/write, SQLite index, retrieval, inbox + curation.
4. Gateway contract + Telegram adapter with approval prompts.
5. Web UI; external messaging ingestor.

The messaging ingestor is started early, as its own program in `messaging/` (`daimon-messages`): a service that keeps
the Google Messages link and stores every text, and an MCP server the agent connects to like any other. A separate
message graph built from the texts, and a filter for what crosses into `knowledge/` through the inbox, come next.

