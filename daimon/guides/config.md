---
description: runtime settings in config.yaml. Memory cap, steps per turn, compaction, limits, knowledge and embeddings
---
# config.yaml

Runtime settings. Editing it asks the user unless `self_edit: free`. It's checked as YAML before saving.
Most keys take effect on your next step; channels and harness drivers on restart.

| Key | Default | What it does |
|---|---|---|
| `self_edit` | `policy` | who approves your own file edits (guide `consent`) |
| `memory_max_chars` | 6000 | MEMORY.md can't grow past this; condense instead |
| `max_steps_per_turn` | 30 | tool-call rounds per message before you stop and report |
| `tool_result_max_chars` | 20000 | longer tool results are cut |
| `thread.compact_at_tokens` | 40000 | the main conversation is summarised past this |
| `thread.keep_recent_messages` | 12 | always kept word for word |
| `limits.max_concurrent_workers` | 3 | or `unlimited` |
| `limits.harness.<name>.max_runs_per_day` | | caps a harness (guide `workers`) |
| `harnesses` | claude-code | external agents (guide `workers`) |
| `harness.workspaces` | next to home | where harness workspaces go |
| `knowledge.folder` | `knowledge` | where the notes live: a folder in the home, or any path (an Obsidian vault) |
| `knowledge.embeddings` | `none` | or a LiteLLM embedding model, for meaning-based search |
| `knowledge.retrieve_for_main` | true | look up notes for every message to you |
| `knowledge.retrieval_max_chars` | 6000 | how much retrieved knowledge goes in a prompt |
| `knowledge.curator` | `curator` | the worker that files inbox items into knowledge |
| `channels` | | guide `connections` |

The user can change any part of their setup themselves with `daimon config <part>` (model, key, knowledge, telegram, claude, addons, background, updates, show), and update with `daimon update`. Point them there for what you can't do: API keys, sign-ins, start at login.

Example: the user says "you keep stopping halfway on big jobs":
```yaml
max_steps_per_turn: 60
```
Explain the trade-off (longer turns cost more) when you propose it.

Behaviour and preferences don't belong here: style and behaviour go in SELF.md, rules about actions in
POLICY.md, facts about the user in MEMORY.md or knowledge/.
