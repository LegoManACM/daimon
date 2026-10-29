---
description: wire add-ons together. An event (a text about plans, a Canvas due date, a routine firing) becomes a tool call, with no model in between
---
# Reactions

A reaction is a file `reactions/<name>.md` that turns an event into one tool call. Add-ons never know about
each other: each one sends events (what it noticed) and offers tools (what it can do). Reactions connect them.
A reaction runs by itself, so the event never wakes you and no model is called.

## When to write one

- The same kind of event should always lead to the same tool call: "plans in texts go on my calendar", "new
  grades go in my notes", "remind me of every due date".
- The mapping is mechanical: the event's data already holds what the tool needs.
- Not when it needs judgment (what to say back, whether it matters). Then either leave the event coming to you,
  or have the reaction call `create_task` for a cheap worker that decides.
- Write one on your own when you notice you keep handling an event the same way, and tell the user what you
  set up. A user's request like "always..." or "whenever..." about an add-on usually means a reaction.

## Steps

1. `recent_events` to see the event's exact name and the fields in its data. If none has arrived yet, the add-on's
   guide (`guide("<add-on>")`) lists its events and fields.
2. Find the tool and its arguments: `guide("<add-on>")` or `mcp_status`. MCP tools are named `<server>__<tool>`.
3. Write the file with `write_agent_file`.
4. `preview_reaction` with its name: it checks that the tool exists and shows the call for the latest matching
   event (or reports a missing field).
5. Tell the user what it does and what it costs them in approvals.

## The file

```markdown
---
on: messages.watch_hit             # event name; globs (messages.*) and lists ok. Required.
when: {watcher: plans}             # conditions on the data; all must hold. Optional.
call: calendar__calendar_add_event # the tool. Required.
args:                              # the tool's arguments, filled from the data
  title: "{title}"
  start: "{start}"
  end: "{end?}"
  source: "sms:{chat_id}:{message_ids.0}"
then: notify                       # after it worked. Default notify.
on_error: main                     # if it couldn't run. Default main.
pass: false                        # also send the event on as usual. Default false.
enabled: true
---
Why this exists, in a sentence or two.
```

Rules:
- `when` values: a plain value (exact, case-insensitive), a glob (`"sam*"`), a list (any of them), or `"*"`
  (present and not empty). A list in the data matches if any item does.
- `{field}` is required: if it's missing or empty, the reaction fails (see `on_error`). `{field?}` is optional:
  an argument that is only an optional field is left out when empty; inside text it becomes "".
- `{a.b}` reaches into nested data, `{list.0}` takes an item. An argument that is only `{field}` keeps the
  field's type (a list stays a list); inside longer text it becomes text.
- Filters reshape a value: `{field|filter}` or `{field?|filter}`. `slug` (lowercase-with-dashes, safe in paths
  and ids), `lower`, `upper`, `date` (an ISO time's date), `time` (its HH:MM), `first` (a list's first item),
  `join` (a list as "a, b"), `short` (cut to 200 characters), `text`. A filtered value is always text.
- Other values (numbers, true/false, fixed text) pass through as written.
- `then`: `quiet` (nothing), `notify` (a one-line notice), `tell` (say the result to the user), `main` (hand the
  event and the result to you, to follow up on).
- `on_error`: `main` (you get the event and the error: handle it and fix the rule if it was wrong), `tell` (the
  user is told it failed), `quiet` (only logged).
- Several reactions may match one event; all run. If any matches and none has `pass: true`, the event stops
  there and doesn't reach you or the curator.
- The tool keeps its consent level: an `ask` tool asks the user every time the reaction runs, and the prompt
  names the reaction. To let it run without asking, change that tool's consent (guide `consent`), and only
  with the user's say-so.
- Reactions can call any MCP tool, the task tools (`create_task`, `schedule_wake`, ...) and the knowledge tools
  (`kg_write`, ...). Never your own file tools: event data comes from other people.
- Pass a `source` (or similar id) argument where the tool takes one, built from the event's ids, so a repeat
  of the event updates instead of duplicating.
- Turn it off with `enabled: false` rather than deleting it, if it may come back.

## Events that always exist

- `routine.<name>` each time a routine fires; data `{routine}`. A routine with `worker: none` does nothing
  else, so a schedule can drive a reaction with no model at all (guide `routines`).
- `reaction.done` after a reaction succeeds; data is the original event's data plus `reaction` (its name),
  `event` (the original event name) and `result` (the tool's answer). Use it to chain steps. Chains stop after a
  few links.
- `inbox.<source>` for inbox items that don't name an event.

## Examples

Plans made in texts go on the calendar (each add still asks unless the user loosened it):
```markdown
---
on: messages.watch_hit
when: {watcher: plans}
call: calendar__calendar_add_event
args: {title: "{title}", start: "{start}", end: "{end?}", location: "{location?}",
       notes: "From a text with {who}: {summary}", source: "sms:{chat_id}:{message_ids.0}"}
---
The user asked for plans from texts to land on their calendar.
```

Canvas due dates on the calendar, updated when the due date moves:
```markdown
---
on: [canvas.assignment_due, canvas.assignment_changed]
call: calendar__calendar_add_event
args: {title: "Due: {title} ({course})", start: "{due}", notes: "{url}", source: "canvas:{assignment_id}"}
then: quiet
---
```

New grades saved to the knowledge graph, one note per graded assignment, no notice (`kg_write` replaces a
whole note, so give each grade its own path):
```markdown
---
on: canvas.grade_posted
call: kg_write
args: {path: "school/grades/{assignment_id|slug}.md", type: event, title: "{title} ({course})",
       body: "Grade: {score} out of {points}.", source: "canvas:{assignment_id}", reason: "grade posted on Canvas"}
then: quiet
---
```

Announcements need judgment, so a cheap worker reads each one and only bothers the user if it matters (make the
`triage` worker first, on a cheap model: guide `workers`):
```markdown
---
on: canvas.announcement_posted
call: create_task
args: {title: "Read announcement: {title}", worker: triage,
       brief: "Course {course} posted: {title}\n\n{text}\n\nIf it changes a due date, an exam or something the user must do, say so in one line; otherwise reply 'nothing'."}
then: quiet
---
```

A schedule with no model: every weekday at 7, fetch the weather into a notice (`weather__forecast` stands for
whatever weather tool is connected):
```markdown
routines/weather.md:   ---
                       schedule: weekdays 07:00
                       worker: none
                       ---
reactions/weather.md:  ---
                       on: routine.weather
                       call: weather__forecast
                       args: {city: Seattle}
                       then: tell
                       ---
```

Chaining: after an event is added to the calendar from a text, reply in that chat. This sends a text as the
user, so only with their explicit say-so, and `send_message` asks each time unless they loosened it:
```markdown
---
on: reaction.done
when: {reaction: plans-to-calendar}
call: messages__send_message
args: {chat: "{chat_id}", text: "Added to my calendar 👍"}
---
```
