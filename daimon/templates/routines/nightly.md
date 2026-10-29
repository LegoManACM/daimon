---
type: routine
schedule: daily 00:00
worker: main
enabled: true
---
It's the nightly run. Work through these, then reply with a short summary for the morning (or start your
reply with [quiet] if there's genuinely nothing worth telling the user).

1. **Memory.** Rewrite MEMORY.md so it reflects who the user is and what matters right now, within its size
   cap. Fold in anything you learned today; drop what's stale.
2. **Tasks.** Review open tasks with list_tasks: cancel ones that no longer make sense, reschedule ones
   that slipped, and note anything stuck.
3. **Tomorrow.** Schedule wakes for anything that needs doing or checking tomorrow (schedule_wake).
4. **Ideas.** If you see something useful to do for the user, create it as a proposed task so they can say
   go or skip.
5. **Knowledge.** Save anything durable from today's conversation that isn't in the graph yet. Skim
   `knowledge/log.md` for today's changes and fix duplicates or contradictions you notice.
6. **Conversation.** Run compact_conversation so the main conversation stays light.
