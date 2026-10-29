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
| `canvas_status()` | which Canvas, who's signed in, active classes |

## Home folder

`$DAIMON_CANVAS_HOME`, default `~/.daimon-canvas`.

```
config.yaml     settings (below)
token           the access token; CANVAS_TOKEN in the environment wins
files/          downloads, by class: <class>/<assignment>/..., <class>/files/...
```

| setting | default | |
|---|---|---|
| `url` | "" | your Canvas, like `https://school.instructure.com` (CANVAS_URL in the environment wins) |
| `timezone` | "" | for due dates ("" = this computer's) |
| `max_file_mb` | 25 | bigger files aren't downloaded |
| `read_chars` | 6000 | how much one answer shows before it says where to read on |

## Tests

```bash
.venv/Scripts/python -m pytest canvas/tests
```

They run against a fake Canvas, through a real MCP client.
