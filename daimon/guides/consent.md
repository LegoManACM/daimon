---
description: who approves what. Tool consent levels in mcp.json, self_edit for your own files, harness rules, and POLICY.md
---
# Consent and permissions

Consent never means "no". It decides whether the user approves first. There are four layers; know which one a
request is about.

## 1. Tool levels (mcp.json)

Every tool has a level: `auto` (just run), `notify` (run, then the user gets a one-line notice) or `ask` (the user
approves each call; they can also answer "always", which sets that tool to `auto`).

Where the level comes from, first match wins:
1. The server's `consent` rules in `mcp.json`: an exact tool name, then globs in order.
2. The top-level `defaults` in `mcp.json`: `{"read_only": "auto", "other": "ask"}` unless changed. A tool counts
   as read-only when the server marks it so.

Change it with `set_tool_consent(server, tool, level, reason)`. `tool` may be a glob (`send_*`, `*`). Exact names
are placed first so they beat globs. Editing `mcp.json` asks the user under `self_edit: policy`, so this tool asks
too. That's right: loosening is the user's call.

```json
{
  "defaults": {"read_only": "auto", "other": "ask"},
  "mcpServers": {
    "calendar": {
      "command": "...", "args": ["mcp"],
      "consent": {"calendar_add_event": "notify", "calendar_delete_event": "ask", "calendar_*": "ask"}
    },
    "messages": {
      "command": "...",
      "consent": {"send_message": "ask", "set_watcher": "notify", "remove_watcher": "notify"}
    }
  }
}
```

Rules:
- Loosen only when the user asks ("just add things to my calendar, don't ask") and only as far as they said:
  one tool, not the whole server. Tightening ("always ask before ...") you may do on your own.
- Prefer `notify` over `auto` for anything that acts on the world, so the user still sees it happen.
- Reactions and workers use the same levels. A reaction calling an `ask` tool asks every time it fires; if
  that's the point of the reaction ("put plans on my calendar automatically"), suggest `notify` for that one
  tool and let the user decide.
- `mcp_status` shows every tool with its current level.

## 2. Your own files (config.yaml `self_edit`)

- `policy` (default): POLICY.md, config.yaml, mcp.json and models.yaml ask; everything else (SELF.md, MEMORY.md,
  workers, skills, routines, reactions, knowledge) is auto.
- `free`: every edit is auto. `all`: every edit asks.
- Assigning a model marked `approval: ask` asks in every mode (guide `models`).
- Every edit is a git commit with your reason. `change_history` lists them; `undo_change` reverts one.

Changing `self_edit` is an edit to config.yaml, so it asks (unless already `free`).

## 3. Harness workers (external agents such as Claude Code)

Inside its workspace a harness works freely. Actions that reach outside it get a level from rules shaped
`Tool(argument)` with globs, first match wins: the worker's `harness_consent`, then the harness entry's `consent`
in config.yaml, then the built-in defaults (`git push`, `gh pr create/merge`, `gh release`, publish, deploy, and
the harness's own MCP servers ask; writes outside the workspace ask).

```yaml
# workers/coder.md frontmatter
harness: claude-code
harness_consent: {"Bash(*git push*)": notify, "mcp__github__get_*": auto}
```

## 4. POLICY.md (soft rules)

Plain-language rules you follow on top of the levels: "replies to Mom: just send", "never book anything over
$50 without asking", "don't schedule before 10". Use it for rules that depend on content, which a level can't
express. A level is enforced by the runtime; a policy rule is enforced by you. Editing it asks under `policy`.

## Which layer?

| The user says | Change |
|---|---|
| "Stop asking me before adding calendar events" | `set_tool_consent(calendar, calendar_add_event, notify)` |
| "Always check with me before texting anyone" | `set_tool_consent(messages, send_message, ask)` |
| "You can text Mom without asking" | POLICY.md rule (a level can't tell who the text is to) |
| "Change your own files without asking" | config.yaml `self_edit: free` |
| "Let the coder push to its branches" | the worker's `harness_consent` |
| "Don't use the expensive model without asking" | models.yaml `approval: ask` |
