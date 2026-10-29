---
description: workers (model + prompt + tools), handing them tasks, harness workers like Claude Code, and long-running projects
---
# Workers, tasks and projects

## Workers

A worker is `workers/<name>.md`: a model, the tools it may use, and a prompt. You are `workers/main.md`.

```markdown
---
type: worker
model: anthropic/claude-haiku-5-5   # from models.yaml (guide models)
mcps: [canvas, calendar]            # MCP servers it may use, or `all`; [] = none
skills: [study-planner]             # skills/<name>/SKILL.md loaded into its prompt
---
You read Canvas announcements and say in one line whether the user must act on them...
```

Create one with `create_worker(name, prompt, reason, model?, mcps?, skills?, harness?, repo?)`, or edit the file.
Rules:
- Names are lowercase-with-dashes. Give it only the servers it needs.
- Its prompt must stand alone: workers never see your conversation, only their task's brief.
- Every worker also gets the knowledge tools, and `ask_main` / `report_progress` while on a task.
- Make a worker for a recurring kind of work that needs its own model or tools (a cheap `triage` worker for
  reactions to hand judgment calls to; a `curator` that files the inbox into knowledge, which is the default).

## Tasks

`create_task(title, brief, worker, status?, run_at? | in_minutes?, due?, project?)`.
- `status: queued` runs it now (or at `run_at`); `proposed` waits for the user's go (POLICY.md says which to use:
  your own ideas start proposed).
- The brief has everything: goal, context, what done looks like.
- A worker blocked on a question calls `ask_main`; you get it as a background event and answer with
  `answer_task` (asking the user first if only they know). The worker continues.
- `list_tasks`, `read_task`, `update_task` (status changes, cancel).
- `schedule_wake(note, when | in_minutes)` is a task for you: a note to yourself at a time. Use it for every
  "later" you promise.

## Harness workers (external agents)

`harness: claude-code` (or another entry under `harnesses:` in config.yaml) runs the worker's tasks in that agent
instead of your own loop. Best for coding and long work in a repository.

```markdown
---
type: worker
harness: claude-code
model: sonnet                   # the harness's own model names (guide models)
repo: C:/Users/me/Github/site   # a git worktree per task (or per project), on branch daimon/<name>
harness_options: {max_turns: 80}
harness_consent: {"Bash(*git push*)": notify}   # guide consent
---
You are the coding worker for the user's website...
```

- `limits.harness.<name>.max_runs_per_day` in config.yaml caps runs. A rate-limited task waits and resumes.
- New harnesses: add an entry under `harnesses:` in config.yaml with `driver: cli` (a headless command line with
  `{prompt}` / `{session}` templates) or `driver: package.module:Class`. config.yaml's comments show examples.

## Projects

Long work spanning many tasks: `create_project(name, title, goal, worker?, repo?, folder?)`. Tasks join with
`project:`; one runs at a time per project, sharing its workspace and session. The project keeps handoff notes
(`update_project_notes` from inside a task) and a log. `update_project` pauses, resumes or finishes it;
`read_project` shows everything.
