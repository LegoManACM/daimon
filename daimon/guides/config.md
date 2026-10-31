---
description: runtime settings in config.yaml. Memory cap, steps per turn, compaction, limits, model timeouts, background jobs, knowledge and embeddings
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
| `model_calls.first_token_timeout_s` | 300 | wait this long for a model's first word (a local one may be loading) |
| `model_calls.stall_timeout_s` | 120 | longest silence in the middle of an answer |
| `model_calls.timeout_s` | 600 | a whole answer, when it isn't streamed |
| `model_calls.retries` | 2 | after connection errors, rate limits and timeouts |
| `model_calls.cache` | true | mark prompts cacheable for Claude (others cache on their own) |
| `model_calls.ollama.keep_alive` | `30m` | keep a local model loaded this long after use (`-1` forever) |
| `model_calls.ollama.num_ctx` | 16384 | a local model's context window; a model's own params win |
| `model_calls.ollama.warm_up` | true | start loading local models before they're needed |
| `tools.background_after_s` | 30 | a slower tool call becomes a background job (guide `jobs`); 0 never |
| `tools.job_max_minutes` | 30 | a job running longer is stopped |
| `tools.mcp_timeout_s` | 900 | the most an add-on tool call may take (restart to apply) |
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
| `web` | on, localhost | the web page: `listen`, `port`, `auth` (guide `connections`) |

The user can change any part of their setup themselves with `daimon config <part>` (model, key, knowledge, telegram, web, claude, addons, background, updates, show), and update with `daimon update`. Point them there for what you can't do: API keys, sign-ins, start at login.

Example: the user says "the local model keeps timing out" (on a slow computer it can take minutes to load):
```yaml
model_calls:
  first_token_timeout_s: 600
  ollama: {keep_alive: -1}   # never unload it
```

Example: the user says "you keep stopping halfway on big jobs":
```yaml
max_steps_per_turn: 60
```
Explain the trade-off (longer turns cost more) when you propose it.

Behaviour and preferences don't belong here: style and behaviour go in SELF.md, rules about actions in
POLICY.md, facts about the user in MEMORY.md or knowledge/.
