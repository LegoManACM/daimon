---
description: recurring work on a schedule (routines/), one-off wakes, and the schedule syntax
---
# Routines and wakes

## Routines

A routine is `routines/<name>.md`: a schedule and an instruction. It's behaviour, so it's committed and undoable.

```markdown
---
schedule: weekly sun 18:00
worker: main          # main (you get the body as a message) | a worker name (a task is made) | none
project: thesis       # optional, with a worker: the task joins that project
enabled: true
---
Check my grades in every class and tell me about anything below a B.
```

Schedules:
- `daily HH:MM`, `weekdays HH:MM`
- `weekly mon,wed,fri HH:MM` (days: mon tue wed thu fri sat sun)
- `monthly D HH:MM` (day 1-31)
- `every 30m`, `every 2h`, `every 1d` (from the last run)

Rules:
- A routine missed while the agent was off runs once when it's back.
- Changing the schedule starts it fresh at the next matching time.
- Use `worker: main` for things needing your context and judgment; a worker for self-contained jobs (cheaper,
  runs in parallel); `worker: none` when a reaction does the whole job with no model (guide `reactions`).
- Every routine also sends the event `routine.<name>` when it fires, whatever its worker.
- Turn off with `enabled: false`. `nightly` is the built-in upkeep routine: edit it, don't delete it.

## Wakes

For one-off "later": `schedule_wake(note, when="2026-11-02T09:00")` or `in_minutes=90`. The note comes back to
you at that time as a background event. Never promise a follow-up without scheduling one.

## Which one?

| The user says | Do |
|---|---|
| "Every Sunday, check my grades" | a routine, `worker: main` or a worker |
| "Remind me Friday to call the dentist" | `schedule_wake` |
| "Every morning at 7, post the weather" | routine `worker: none` + a reaction on `routine.<name>` |
| "When a text mentions plans, add them" | not a routine: a reaction on the event (guide reactions) |
