# daimon-canvas

Your Canvas classes for Daimon (or any MCP client): what's due, each class's modules, pages and files,
grades and announcements. It's an MCP server over stdio that calls the Canvas API with your access token. It only
reads: nothing it does submits or changes anything on Canvas.

Items have short ids the agent passes around: `assignment:9:1`, `quiz:10:2`, `file:9:77`, `syllabus:9`...

```
 the agent ──stdio──> daimon-canvas mcp ──https──> your school's Canvas API
                           │
                           └── files/  what canvas_open downloaded, read back with read_canvas_file
```

## Setup

Tick **Canvas** in `daimon setup`'s add-ons step. It installs this package next to Daimon, connects it to the
agent, and runs the sign-in. You'll need an access token: in Canvas, Account → Settings → **+ New access token**.

Afterwards, `python -m daimon_canvas status` (with Daimon's Python) checks the login, and `... todo` shows what's due
the way the agent sees it. Every tool is read-only, so they run without asking (`auto`).

### By hand, or with another MCP client

```bash
uv pip install -e canvas      # from a checkout, into whichever Python will run it
daimon-canvas login           # your Canvas address + the access token
```

Then add it to the agent's `mcp.json` (setup writes the same thing, with Daimon's Python as the command):

```json
{
  "mcpServers": {
    "canvas": {"command": "/path/to/python", "args": ["-m", "daimon_canvas", "mcp"]}
  }
}
```

It works the same in Claude Code (`claude mcp add canvas -- daimon-canvas mcp`) or any other MCP client.

## Tools

| tool | what it does |
|---|---|
| `canvas_assignments(days_ahead, course)` | overdue work and what's due soon that isn't submitted, each with an id |
| `canvas_course(course, search)` | one class's modules and their items, the syllabus; search reaches its Pages and Files too |
| `canvas_open(item, download_files)` | anything in full by id, link or title: instructions, due date, points, status, rubric, or a file's text; attachments are saved |
| `read_canvas_file(path, start_line)` | read a saved file a page at a time (PDF, Word, slides, spreadsheets as text) |
| `canvas_grades()` | current grade in each class |
| `canvas_announcements(days_back, course)` | recent announcements, newest first |
| `canvas_inbox(unread_only)` | the Canvas inbox: conversations with their latest message |
| `canvas_message(conversation)` | one inbox conversation in full (it stays unread) |
| `canvas_events(days_ahead, course)` | class events that aren't assignments: lectures, exams, trips |
| `canvas_feedback()` | recent comments from teachers and TAs on submitted work |
| `canvas_status()` | which Canvas, who's signed in, active classes |

## Events

`daimon-canvas watch` checks Canvas and tells the agent what's new by dropping files into its inbox, each with
`event: canvas.<name>` and a flat `data:` dict in the frontmatter, so the agent's own rules (reactions) can act on
them without a model. Each fact is reported once.

| event | when | data |
|---|---|---|
| `canvas.assignment_due` | work not done yet comes within `watch_days` of its due date | `course`, `course_id`, `assignment_id`, `kind`, `title`, `due`, `due_date`, `url`, `points` |
| `canvas.assignment_changed` | its due date, title or points changed | the same, plus `changed`, `previous_due`, `previous_title` |
| `canvas.grade_posted` | a grade is released or changed | `course`, `course_id`, `assignment_id`, `title`, `score`, `grade`, `points`, `url`, `graded_at` |
| `canvas.announcement_posted` | a class posts an announcement | `course`, `course_id`, `announcement_id`, `title`, `author`, `posted_at`, `url`, `text` |
| `canvas.inbox_message` | someone writes to you in the Canvas inbox | `conversation_id`, `message_id`, `subject`, `course`, `from`, `sent_at`, `text`, `files`, `url` |
| `canvas.submission_comment` | a teacher or TA comments on your submission | `course`, `course_id`, `assignment_id`, `title`, `comment_id`, `from`, `posted_at`, `text`, `url` |
| `canvas.module_unlocked` | a module opens, or a new one appears | `course`, `course_id`, `module_id`, `module`, `items`, `url`, `new` |
| `canvas.course_event` | a class event (not an assignment) comes within `watch_days` | `course`, `course_id`, `event_id`, `title`, `start`, `end`, `all_day`, `date`, `location`, `url`, `description` |
| `canvas.course_event_changed` | its time, title or place changed | the same, plus `changed`, `previous_start`, `previous_title` |

Times are local ISO 8601 (`2026-10-30T23:59`; all-day events give a date). `assignment_id` is the canvas_open id
(`assignment:9:1`) and `event_id` is `event:<id>`: unique and stable, so they make good keys for anything built from
them. Text is plain and clipped to 1500 characters. Upcoming work and class events are reported from the first
check; grades, announcements, messages, comments and modules only from then on, so starting the watcher doesn't
replay history. Watching never marks a message read.

Not watched: replies in discussions (Canvas only shows them by opening every discussion), and quizzes opening (a quiz
with a due date already comes as `canvas.assignment_due`).

`daimon-canvas watch` checks once; `--every 30` keeps checking every 30 minutes, which is how `daimon setup` runs it
at login. It only reports: what happens next is up to the agent.

## Home folder

`$DAIMON_CANVAS_HOME`, default `~/.daimon-canvas`.

```
config.yaml     settings (below)
token           the access token; CANVAS_TOKEN in the environment wins
files/          downloads, by class: <class>/<assignment>/..., <class>/files/...
watch.json      what `watch` has already reported
```

| setting | default | |
|---|---|---|
| `url` | "" | your Canvas, like `https://school.instructure.com` (CANVAS_URL in the environment wins) |
| `timezone` | "" | for due dates ("" = this computer's) |
| `max_file_mb` | 25 | bigger files aren't downloaded |
| `read_chars` | 6000 | how much one answer shows before it says where to read on |
| `watch_days` | 14 | `watch` reports work this many days before it's due |
| `agent_inbox` | "" | where `watch` drops events ("" = `$DAIMON_HOME/inbox`, else `~/.daimon/inbox`) |

## Tests

```bash
.venv/Scripts/python -m pytest canvas/tests
```

They run against a fake Canvas, through a real MCP client.
