<!--
CONTEXT.md: what the agent is shown at the start of every turn, besides its standing prompt (SELF.md, POLICY.md,
MEMORY.md...). It's rendered once when a message arrives and put in front of that message, so it can hold things
that change on their own (the time, today's calendar, recent texts, what's due) without changing the standing
prompt, which providers then reuse from step to step (prompt caching) instead of reading it again.

You and the agent can both edit this file. Comments like this one are dropped before the agent sees it.

Tags are replaced by what they name. A line that's only a tag disappears when the tag comes out empty, and so
does a heading with nothing left under it.

  {now}                     the date and time
  {tasks}                   open tasks
  {projects}                active projects
  {knowledge}               notes relevant to this message, from the knowledge folder
  {spend}                   model spend today
  {jobs}                    background jobs still running
  {events:messages.*}       recent add-on events (a name or glob, e.g. canvas.*), newest last
  {file:notes/today.md}     a file in the home folder (or a knowledge note: {file:knowledge/people/sam.md})
  {tool:NAME key=value ...} a tool's result. Any tool the agent has whose consent level is auto (lookups, never
                            something that asks or changes things). Values with spaces go in quotes:
                            {tool:calendar__calendar_events start=today days=2}
                            {tool:messages__search_messages query="dinner plans" since=2d}

Options go after a |, space-separated:
  every=15m                 reuse the result for this long instead of fetching it every turn (s, m, h, d)
  max=1500                  cut the result to this many characters (default 4000)
  n=10                      how many events, for {events}

A tag that fails says so in its place and never stops the turn. Workers have their own: put
`context: contexts/<name>.md` in a worker's frontmatter (otherwise they get the time and relevant knowledge).

Ideas, for the add-ons that ship with Daimon (copy one below this comment to use it):

## Today's calendar
{tool:calendar__calendar_events start=today days=1 | every=10m max=2000}

## Due on Canvas this week
{tool:canvas__canvas_assignments days_ahead=7 | every=30m max=1500}

## Texts from the last 6 hours
{tool:messages__recent_messages since=6h incoming_only=true limit=15 | every=5m max=2500}

## What the watchers caught
{events:messages.watch_hit | n=5}
-->
{now}

## Open tasks
{tasks}

## Active projects
{projects}

## Running in the background
{jobs}

## Relevant knowledge
{knowledge}
