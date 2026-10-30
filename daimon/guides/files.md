---
description: where each kind of change goes in your home folder, skills, the knowledge graph, and history and undo
---
# Your files

| Change | File | Edit asks (self_edit: policy)? |
|---|---|---|
| How you behave, tone, how proactive | `SELF.md` | no |
| Rules about actions ("ask before X", "never Y") | `POLICY.md` | yes |
| The short picture of the user, always loaded | `MEMORY.md` (capped, see `memory_max_chars`) | no |
| What you're shown each turn (time, calendar, tasks...) | `CONTEXT.md` (guide `context`) | no |
| Details: people, places, plans, preferences | `knowledge/` via `kg_write` | no |
| A worker | `workers/<name>.md` (guide `workers`) | no, unless its model has `approval: ask` |
| Recurring work | `routines/<name>.md` (guide `routines`) | no |
| Event → tool rules | `reactions/<name>.md` (guide `reactions`) | no |
| Know-how a worker loads | `skills/<name>/SKILL.md` | no |
| Tools and their consent | `mcp.json` (guide `connections`, `consent`) | yes |
| Models | `models.yaml` (guide `models`) | yes |
| Runtime settings | `config.yaml` (guide `config`) | yes |

Use `edit_agent_file` for a small change (old text → new text, must match once), `write_agent_file` to create or
replace, `delete_agent_file` to remove. Every change needs a one-line reason and becomes a commit. A file with
broken YAML, an unknown model or a malformed reaction is refused with the reason, so fix and retry.

## Skills

Agent Skills format: `skills/<name>/SKILL.md` with frontmatter `name` and `description`, then the know-how in
markdown. A worker loads it when its frontmatter lists it in `skills:`, so the text only costs context for that
worker.

```markdown
---
name: study-planner
description: turn a list of due dates into a study plan with blocks before each one
---
Work backwards from each due date: ... Never schedule more than 3 hours a day ...
```

Write one when a worker keeps needing the same instructions, or the user teaches you a procedure ("this is how I
like my weekly review done").

## Knowledge

`kg_search`, `kg_read`, `kg_list`, `kg_write`. One note per person, project, place, event, preference or topic,
linked with markdown links. `kg_write` replaces the whole note: read it first and keep what's still true. Pass
`source` so facts keep their origin. The `curator` worker files inbox items into knowledge.

## History

`change_history` lists recent changes (yours and the user's). `undo_change(change, reason)` reverts one; undoing
a change to a protected file asks like editing it would. "What did you change this week?" and "undo that" are
answered from here.
