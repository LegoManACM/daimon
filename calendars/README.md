# daimon-calendar

Your calendars for Daimon (or any MCP client): Google Calendar, Outlook / Microsoft 365, Apple iCloud, Fastmail,
Yahoo, any other CalDAV server (Nextcloud, ...), and read-only `.ics` / `webcal://` feeds, merged into one. It's an
MCP server over stdio that talks to each provider's API directly.

```
 the agent ──stdio──> daimon-calendar mcp ─┬─> Google Calendar API        (OAuth, your own client)
 (or a reaction,                           ├─> Microsoft Graph            (OAuth device code, your own app)
  or any MCP client)                       ├─> CalDAV: iCloud, Fastmail…  (username + app password)
                                           └─> .ics feeds                 (read-only)
```

Each provider is a small class with the same six methods (list calendars, list/get/create/update/delete events,
see `model.py`). Everything else is shared on top of them: short event ids, merged listings, free time, and
`source` keys. Adding another provider means writing one more of those classes.

## It doesn't know about other add-ons

Nothing here knows about texts, Canvas or anything else. Other things put events on the calendar by calling
`calendar_add_event`. That can be the agent itself, or a Daimon reaction that calls the tool with no model involved
(for example, a text that sets up plans becomes an event). Two features make that safe:

- **`source`**: any key the caller picks (`sms:<chat>:<message>`, `canvas:assignment:9:1`, ...). Adding with a
  source that's already on the calendar updates that event (or answers `Unchanged`) instead of adding a second
  one. `calendar_update_event` and `calendar_delete_event` can find events by it. The key is written as the last
  line of the event's notes (`daimon-source: ...`), so it survives a lost `ids.json`.
- **Plain inputs and clear errors**: ISO 8601 times (`2026-10-30T15:00` local, an offset or `Z`, or
  `2026-10-30` for all day; `today`, `tomorrow`, `friday` and `+2h` work too). `end` is optional. A bad input
  fails with a message that names the argument (`start: Can't read the time 'soonish'...`).

## Setup

Tick **Calendar** in `daimon setup`'s add-ons step. It installs this package next to Daimon, connects it to the
agent and signs in to one calendar. Add more any time:

```bash
daimon-calendar add google        # or microsoft, apple, fastmail, yahoo, caldav, ics
daimon-calendar status            # the accounts and their calendars
daimon-calendar agenda            # the next 7 days, as the agent sees them
daimon-calendar remove NAME
```

`--name` keeps two accounts of the same kind apart (`add google --name work`).

**Google** needs an OAuth client of your own, because Google doesn't let apps share one for calendar access.
In [console.cloud.google.com](https://console.cloud.google.com): make a project, enable the *Google Calendar
API*, set up the OAuth consent screen (External, with yourself as a test user), then Credentials → OAuth client
ID → *Desktop app*, and download the JSON. `add google` asks for that file and opens the browser to sign in.
While the app is in "Testing", Google expires the sign-in after 7 days. Publishing it (no review is needed for
your own use) stops that.

**Microsoft** needs an app registration of your own. In [entra.microsoft.com](https://entra.microsoft.com):
App registrations → New, for *any organizational directory and personal Microsoft accounts*, with no redirect
URI. Then Authentication → *Allow public client flows: Yes*. `add microsoft` asks for its client id and
shows a code to enter at microsoft.com/devicelogin.

**Apple iCloud** uses CalDAV with an app-specific password (account.apple.com → Sign-In and Security →
App-Specific Passwords). Fastmail and Yahoo work the same way with their app passwords. `add caldav --url ...`
connects any other CalDAV server (Nextcloud: `https://host/remote.php/dav`).

**`.ics` feeds** (`add ics --url ...`) are read-only: a school or Canvas calendar feed, holidays, or a shared
calendar's secret address.

### By hand, or with another MCP client

```bash
uv pip install -e calendars
daimon-calendar add google
```

```json
{
  "mcpServers": {
    "calendar": {
      "command": "/path/to/python", "args": ["-m", "daimon_calendar", "mcp"],
      "consent": {"calendar_add_event": "ask", "calendar_update_event": "ask", "calendar_delete_event": "ask"}
    }
  }
}
```

Reading runs without asking (the tools are marked read-only). Adding, changing and deleting ask by default. Tell
the agent to change that (for example "just add events from my texts, tell me after") and it sets
`calendar_add_event` to `notify`.

## Tools

| tool | what it does |
|---|---|
| `calendar_events(start, end, days, query, calendar)` | what's on, every calendar merged, each event with a short id |
| `calendar_free_time(start, end, minutes, calendar)` | open slots within your usual hours |
| `calendar_add_event(title, start, end, all_day, location, notes, calendar, attendees, repeat, reminder_minutes, source)` | add an event, or update the one with that `source` |
| `calendar_update_event(event \| source, ...)` | change only what's given; moving the start keeps the length |
| `calendar_delete_event(event \| source)` | delete it |
| `calendar_list()` | the calendars: default, read-only, hidden |
| `calendar_status()` | each account and who it's signed in as |

`repeat` takes an RRULE (`FREQ=WEEKLY;BYDAY=TU,TH;COUNT=10`), converted to Outlook's format for Microsoft.
Changing or deleting one occurrence of a repeating event changes only that occurrence on Google and Outlook, but
the whole series on CalDAV.

## Home folder

`$DAIMON_CALENDAR_HOME`, default `~/.daimon-calendar`.

```
config.yaml          settings (below)
accounts/<name>.json one per account: kind, settings, and its credentials (tokens or app password)
ids.json             short event ids → each provider's ids, and source keys → events
```

| setting | default | |
|---|---|---|
| `timezone` | "" | for times given and shown ("" = this computer's). Set it (`America/Los_Angeles`) so repeating events follow daylight saving |
| `default_calendar` | "" | where new events go: a name or `account/name` ("" = the first account's main calendar) |
| `hidden` | [] | calendars left out of listings and free time |
| `day_start`, `day_end` | 08:00, 21:00 | the hours `calendar_free_time` looks in |
| `default_minutes` | 60 | an event's length when no end is given |

## Tests

```bash
.venv/Scripts/python -m pytest calendars/tests
```

They run against fake Google, Graph, CalDAV and feed servers, through a real MCP client.
