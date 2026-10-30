---
description: what you're shown at the start of each turn (CONTEXT.md and its {tags}), message times, and keeping the prompt cacheable
---
# Turn context (CONTEXT.md)

Your prompt has two parts:

- **The standing prompt** (system): SELF.md, POLICY.md, MEMORY.md, your worker file, skills, guides and the
  Runtime list. It changes only when a file changes, so the provider reuses it from step to step (prompt
  caching) and a local model keeps it loaded. Don't put anything there that changes by itself.
- **The turn context**: `CONTEXT.md`, rendered once when a message arrives and put in front of that message in a
  `<turn-context>` block. The time, open tasks, today's calendar, recent texts, relevant knowledge: anything
  that's different each turn goes here.

Every message from the user or an event also starts with when it arrived, like `[Fri Oct 30 14:02]`, so you can
tell how much time passed between messages ("you asked this three hours ago").

## Tags

CONTEXT.md is markdown with `{tags}`; each is replaced by what it names. A line that's only a tag disappears when
the tag comes out empty, and so does a heading with nothing left under it. `<!-- comments -->` are dropped (the
file explains itself in one at the top).

| Tag | Gives |
|---|---|
| `{now}` | the date and time |
| `{tasks}` / `{projects}` | open tasks / active projects |
| `{knowledge}` | notes relevant to this message (`knowledge.retrieve_for_main` in config.yaml turns it off) |
| `{spend}` | model spend today |
| `{jobs}` | your background jobs still running (guide `jobs`) |
| `{events:messages.*}` | recent add-on events (a name or glob), newest last |
| `{file:notes/today.md}` | one of your files, or a note: `{file:knowledge/people/sam.md}` |
| `{tool:NAME key=value ...}` | a tool's result, for any tool whose consent level is `auto` |

Options go after a `|`: `every=15m` reuses the result for that long instead of fetching it each turn, `max=1500`
cuts it (default 4000 characters), `n=10` is how many events. Quote values with spaces:
`{tool:messages__search_messages query="dinner plans" since=2d}`.

A tag that fails says so in its place (`({tool:...} failed: no tool named ...)`) and never stops the turn; check
the result after editing. Only `auto` tools run as tags: nothing that asks or changes things.

## Changing it

CONTEXT.md is your file (no approval needed under `self_edit: policy`). Add something when you keep having to
look it up at the start of turns ("what's on today?" → a calendar tag with `every=10m`); remove what you never
use, since every line costs tokens on every turn. Prefer `every=` for tools that hit the network.

```markdown
{now}

## Today's calendar
{tool:calendar__calendar_events start=today days=1 | every=10m max=2000}

## Due this week
{tool:canvas__canvas_assignments days_ahead=7 | every=30m max=1500}
```

Workers get their own: `context: contexts/<name>.md` in a worker's frontmatter names a file written the same way.
Without it a worker sees the time and knowledge relevant to its task.
