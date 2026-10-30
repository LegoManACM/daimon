---
description: slow tool calls that run in the background (jobs), start_job, job_wait, job_cancel, and slow or local models
---
# Background jobs

A tool call that's still running after `tools.background_after_s` (config.yaml, default 30s) stops holding you
up. Its result says it became a job (`job3`), and the call keeps going. When it finishes, the result comes back:

- in this turn, if you're still working: a message `[Job job3 (tool args) finished after 2m 10s]` before your
  next step
- as a new message, if you've already replied: it starts a turn like any background event (reply with what the
  user should hear, or `[quiet]`)
- a worker never finishes its task while its jobs run: it waits for them first

Calls waiting on the user's approval count too: an `ask` that isn't answered in time becomes a job, so you can
keep going and the call runs once they answer.

| Tool | Use |
|---|---|
| `start_job(tool, args)` | run a tool in the background on purpose, when you know it's slow (a test run of a watcher on a week of texts, a long search) and there's other work to do meanwhile |
| `job_wait(ids?, timeout_s?)` | wait for jobs and get their results; without ids it returns when any one finishes |
| `job_cancel(id)` | stop one |

Don't start a job for something you need before your very next step; just call the tool. Tell the user when
you're replying while something is still running ("testing the watcher; I'll tell you what it finds"). Jobs live
in memory: a restart drops them, so one that never reported back was cut short.

Settings (config.yaml `tools`): `background_after_s` (0 waits for every call), `job_max_minutes` (a job running
longer is stopped, default 30), `mcp_timeout_s` (the most any add-on tool call may take at all, default 900; an
add-on's own `timeout_s` in mcp.json overrides it).

## Slow models

Model calls wait `model_calls.first_token_timeout_s` (default 300) for the first word, which covers a local model
loading into memory, then `stall_timeout_s` (120) for any silence after that, and retry twice. When one still
fails, the error says why: for Ollama, whether it's not running, the model isn't pulled, or it was still loading.
Pass that on to the user instead of guessing.

Ollama models are kept loaded for `model_calls.ollama.keep_alive` (30m) after their last use, are started
loading when a turn begins, and get a `num_ctx` (16384) context window, because Ollama's own is small and it cuts
long prompts without saying so. The messages add-on's watchers use the same settings, warm their model as soon
as a text is waiting to be read, and have their own `timeout_seconds` in `watch_settings`.
