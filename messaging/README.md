# daimon-messages

Your text messages (SMS, MMS, RCS) for Daimon, read through Google Messages paired as a linked device (the
way messages.google.com works). It's a separate program from the agent: it keeps the link, stores every message
as it arrives, and gives the agent an MCP server to look things up and send texts. The agent never reads the
stream itself.

A message graph built from the texts comes later (see [the end](#for-the-message-graph-next)).

```
 phone (Google Messages)
        │  libgm, linked device
 sidecar/gmessages  (Go, child process, stdin/stdout JSON, no port)
        │
 daimon-messages run  ── filters ──> messages.db (SQLite + FTS5)  <── daimon-messages mcp (stdio) <── the agent
        ▲                                                     <── message graph (later, via feed/ack)
        └── control API on 127.0.0.1 (status, pair, send, ...) <── CLI, MCP send_message
```

## Setup

Tick **Text messages** in `daimon setup`'s add-ons step. It installs this package next to Daimon, connects it to
the agent, starts the service and runs the pairing below. Choose to run Daimon in the background at the end so the
service keeps running (it's what stores messages).

The first start fetches the small Go program that talks to Google Messages (`sidecar`), prebuilt for your system
from the matching release and checked against its SHA256SUMS. From a source checkout with
[Go](https://go.dev/dl) installed, it's built from source instead.

### By hand

```bash
uv pip install -e messaging          # from a checkout, into whichever Python will run it
daimon-messages run                  # keep this running: it's what stores messages
daimon-messages pair                 # in another terminal: pairs through your Google account
```

Google Messages no longer pairs by QR code (Google removed it in 2026), so pairing signs in with your Google
account's cookies and the phone confirms with an emoji:

1. In a private window (Firefox is safest: Chrome can bind cookies to the device), open devtools → Network, go to
   `https://accounts.google.com/AccountChooser?continue=https://messages.google.com/web/config` and sign in with
   just the one Google account. Don't go anywhere else in that window.
2. It lands on `messages.google.com/web/config`: right-click that `config` request in the Network list (reload if it
   isn't there) → Copy → Copy as cURL (bash).
3. Press Enter at `daimon-messages pair`'s prompt: it reads the command from the clipboard (pasting into a terminal
   can cut the long cookie line off). Or save it to a file and pass `--cookies FILE`; a Cookie header or a JSON
   object of the cookies works too.
4. Tap the matching emoji in Google Messages on the phone. Close the private window without signing out.

`daimon-messages pair --qr` still offers the QR code for app versions that have the scanner. The first pairing fetches the
last 7 days (`backfill_days`); Google Messages doesn't give more than about 8. After a restart or disconnect it
catches up from just before the newest stored message.

## Connect it to the agent

Setup does this. By hand, add to the agent's `mcp.json`:

```json
{
  "mcpServers": {
    "messages": {
      "command": "/path/to/python",
      "args": ["-m", "daimon_messages", "mcp"],
      "consent": {"send_message": "ask"}
    }
  }
}
```

Lookups are read-only (so `auto` by default). `send_message` isn't, so it asks by default anyway; the rule just
makes that explicit. The same entry works in Claude Code or any other MCP client.

## Tools

| tool | what it does |
|---|---|
| `search_messages(query, person, chat, since, until, limit)` | full-text search ("a phrase", prefix\*), by person or number, in one chat, in a time range |
| `recent_messages(since, incoming_only, limit)` | what came in lately, grouped by chat |
| `list_chats(query, since, limit)` | conversations, newest first, with their latest message |
| `read_chat(chat, before, after, limit)` | one conversation in order |
| `message_context(message_id, before, after)` | the messages around a search hit |
| `find_contact(query)` | a person's numbers (phone contacts + chats), their chats, when you last talked |
| `messaging_status()` | link state and what's stored |
| `send_message(text, chat \| to)` | send as you, to a chat or a phone number (`allow_send: false` removes it) |

A `chat` can be a chat id, a chat name, or a person ("Sam" means the 1:1 chat with Sam). Times take
`2026-10-20`, `2026-10-20 18:30`, `today`, `yesterday`, `3d`, `12h`, `2w`, in local time. Results are capped
snippets, not dumps.

Lookups read `messages.db` directly, so they work while the service is down. Sending and live status need it running.

## Watchers

Watchers read new texts as they arrive and wake the agent when one matches what it's told to look for. The agent
sets them up through the MCP tools, either because you asked ("tell me when Sam says when they land") or on its
own when it thinks one would help (it tells you when it does). A watcher never replies or acts by itself. When one
fires, it drops a file in the agent's inbox, and the agent decides what to do next, behind its usual consent rules.

| tool | what it does |
|---|---|
| `set_watcher(name, look_for, chats, senders, any_words, all_words, regex, preset, judge, fields, action, then, once, expires, ...)` | create or change one (only the arguments given change) |
| `list_watchers()` / `get_watcher(name)` | what's set up, whether it's active, how often it fired |
| `test_watcher(name, since, chat)` | try it on stored texts without delivering anything |
| `watcher_hits(name, since)` | what fired lately |
| `remove_watcher(name)` | delete one (`set_watcher(name, enabled=False)` pauses it) |
| `watch_settings(model, models, params, quiet_seconds, max_checks_per_hour)` | which model reads texts, and how often |

A watcher is a file in `watchers/` that you can edit too:

```markdown
---
description: Sam's flight landing time
match: {chats: [Sam], any: [land*, flight, arriv*]}   # cheap filter; the model does the judging
fields: {time: "ISO 8601 local datetime"}            # or a list: [time, place]
action: wake          # wake the agent now | inbox (for the curator) | log (just keep the hit)
then: Remind me 30 minutes before Sam lands to leave for the airport.
once: true
expires: 2026-11-15
---
Fire when Sam says when their flight lands, or that it has landed.
```

Texts that pass the match wait until their chat has been quiet for `quiet_seconds`, so a burst is read once. Then
the model reads them, with the earlier texts of the same conversation as context. `judge: false` skips the model
and fires on the match alone, right away. Match presets: `planning`, `changes` (cancel, reschedule, late),
`question`. Two starter watchers come with it: `plans` (new plans and promises) and `changes` (cancellations and
reschedules). Edit or remove them like any other.

**Which model reads your texts** is set in `watch.yaml` (or by asking the agent; `watch_settings` asks you first).
It can be any LiteLLM model. A local one (`ollama_chat/qwen3.5:9b`) keeps texts on this computer, and a cloud one
(`anthropic/claude-haiku-5-5`, `openai/...`, `openrouter/...`) sends them to that provider. Keys come from
`~/.daimon-secrets.env`. A watcher can pick its own model only from `models:` in watch.yaml. With no model set,
only `judge: false` watchers run.

A hit's inbox file carries it twice: as text for the agent to read, and as an event in the frontmatter
(`event: messages.watch_hit`, `data:` with watcher, chat, from, summary, message_ids and the extracted fields
at the top level), so rules on the agent's side can act on it without a model. Hits are also kept in messages.db
(`watch_hits`).

## CLI

```
daimon-messages run [-v]               the service (Ctrl+C stops it)
daimon-messages mcp                    the MCP server, over stdio
daimon-messages pair [--cookies FILE | --qr]
daimon-messages status
daimon-messages search [QUERY] [--person P] [--since 3d]
daimon-messages send TEXT (--chat ID | --to NUMBER)
daimon-messages backfill [--days N]    fetch recent history again
daimon-messages unpair [--wipe]        --wipe also deletes every stored message
daimon-messages build [--download]     build the sidecar from source now (needs Go), or fetch the prebuilt one
```

## Home folder

`$DAIMON_MESSAGES_HOME`, default `~/.daimon-messages`, apart from the agent's home on purpose.

```
config.yaml     settings (below); restart the service after editing
messages.db     chats, messages (+ FTS index), phone contacts, cursors
session.json    the pairing keys; unpairing deletes them
watchers/       one file per watcher (above)
watch.yaml      which model reads texts for watchers, and how often
control.json    port + token of the running service (127.0.0.1 only)
bin/            the sidecar (downloaded or built)
logs/service.log
```

| setting | default | |
|---|---|---|
| `backfill_days` | 7 | history fetched on first pairing |
| `retention_days` | 0 | delete messages older than this (0 keeps everything) |
| `skip_short_codes` | true | never store texts from 5-6 digit senders (group chats exempt) |
| `skip_login_codes` | true | never store login codes |
| `denylist` | [] | names, numbers or chat names never stored |
| `timezone` | "" | for times shown to the agent ("" = this computer's) |
| `control_port` | 8771 | 0 picks a free port |
| `allow_send` | true | offer `send_message` |

MMS attachments are noted (name, type, size), never downloaded.

## For the message graph (next)

The graph builder should read the stream with `Store.feed(consumer)` / `Store.ack(consumer, seq)`: every stored
message has a `seq` in the order it arrived, and each consumer keeps its own cursor in `kv`, so it can stop and
resume without re-reading everything. Crossover into the agent's main graph should go through the agent's inbox
(`daimon inbox add` / `daimon.inbox.drop`, `source: sms`), which hands items to its curator worker, never into
`knowledge/` directly.

## Caveats

- It's an unofficial protocol (libgm, from mautrix-gmessages; version pinned in `sidecar/gmessages/go.mod`), so
  Google can break it. The service restarts the sidecar on a crash and after 10 minutes disconnected; when the phone
  unpairs it, `status` says `logged_out` and you pair again.
- Only one program can use a pairing at a time.
- If Google Messages is opened in a browser somewhere else, the sidecar takes the session back after a short wait.

## Tests

```bash
.venv/Scripts/python -m pytest messaging/tests
```

They need no phone: a fake sidecar stands in for the Go process.
