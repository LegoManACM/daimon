---
description: which models you and your workers may use (models.yaml), approvals, prices, local models, and the keys they need
---
# Models

`models.yaml` is the list of models you may use. Workers (you included) can only run on a model listed there.
Ids are LiteLLM names, `provider/model`.

```yaml
default_for_new_workers: anthropic/claude-sonnet-5-5

models:
  - id: anthropic/claude-haiku-5-5
    tier: cheap                       # free text: cheap | standard | premium | free ...
    good_for: [triage, curation, summaries]
  - id: anthropic/claude-sonnet-5-5
    tier: standard
    good_for: [agentic, scheduling, general]
  - id: anthropic/claude-fable-5-1
    tier: premium
    approval: ask                     # every assignment to a worker or task needs the user's OK
    max_usd_per_task: 3
  - id: ollama_chat/qwen3.5:9b        # a local model: free, private, weaker
    tier: free
    params: {api_base: "http://localhost:11434"}
  - id: openrouter/some/model
    params: {api_key: "${OPENROUTER_API_KEY}"}
    price: {in: 0.5, out: 1.5}        # USD per million tokens, when LiteLLM doesn't know the price
```

Rules:
- Adding, removing or changing a model is an edit to models.yaml: it asks the user (unless `self_edit: free`).
  Suggest one with a reason (cost, privacy, ability); don't add one quietly.
- A model needs its provider key in the user's secrets file (ANTHROPIC_API_KEY, OPENAI_API_KEY,
  OPENROUTER_API_KEY, GEMINI_API_KEY, ...). The prompt's Runtime lists models that are "listed but not usable
  yet" with the missing key. You can't read or set secrets: ask the user to add the key to the secrets file
  (shown as `${NAME}` in config; they can also run `daimon setup`).
- `${NAME}` in `params` is read from the secrets file when the call is made; the value never passes through you.
- Change a worker's model by editing `model:` in `workers/<name>.md`. It's checked against models.yaml, and asks
  if that model has `approval: ask`.
- Pick by the job: cheap for triage, filing and summaries; standard for most work; premium only when it matters
  and the user agrees. Local models keep data on the computer (good for reading texts) but are weaker.
- Spend is tracked per call (Runtime shows today's). `limits` in config.yaml caps concurrency (guide `config`).

## Harness models are separate

A harness worker (guide `workers`) runs on the external agent's own account, so its `model` is that agent's name
for it (`opus`, `sonnet`, `haiku` for claude-code) and models.yaml doesn't apply. Its allowed names are the
harness entry's `models` in config.yaml; an entry there can be `{id: opus, approval: ask}`. Never add a harness's
model to models.yaml.

## Models outside this file

Some add-ons choose their own model for their own work. The messages add-on's watchers read texts with the model
in its `watch_settings` (a local one keeps texts on the computer). Changing that is that add-on's tool, and its
own consent level.
