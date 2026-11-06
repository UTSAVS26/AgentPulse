# API reference

Base URL: `http://localhost:8000`. The interactive OpenAPI docs at **http://localhost:8000/docs** have every
schema and a "Try it out" button. This page is a quick overview with `curl` examples (Git Bash / macOS / Linux syntax).

## System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status of `database`, `llm` (Bifrost or mock), `search` (Tavily or mock). `status` is `ok` or `degraded`. |
| GET | `/models` | Every `provider/model` id Bifrost can route to (the mock's list in mock mode). 502 if Bifrost is unreachable. |

```bash
curl -s localhost:8000/health
curl -s localhost:8000/models
```

## Agents

| Method | Path | Description |
|---|---|---|
| GET | `/tools` | Tool catalogue (name, description, JSON-schema parameters) |
| GET | `/agents` | List agents |
| POST | `/agents` | Create an agent → `201` (`409` if the name exists, `422` on validation error) |
| GET | `/agents/{id}` | Get one agent (`404` if missing) |
| PUT | `/agents/{id}` | Replace an agent's configuration |
| DELETE | `/agents/{id}` | Delete an agent and its executions → `204` |
| POST | `/agents/{id}/try` | Run the agent once on a prompt; returns the execution with its full trace |
| GET | `/agents/{id}/executions?limit=20` | Recent executions of an agent, newest first |
| GET | `/executions/{id}` | One execution with its full trace |

Validation rules: `model` must look like `provider/model`, `tools` must be known tool names, `max_steps` is 1–20, `temperature` is 0–2.

```bash
# create
curl -s localhost:8000/agents -H 'content-type: application/json' -d '{
  "name": "My Qwen agent",
  "model": "groq/qwen/qwen3.8-27b",
  "tools": ["calculator", "web_search"],
  "system_prompt": "Be precise. Use tools when helpful.",
  "temperature": 0,
  "max_steps": 5
}'

# try it, with 50% of tool calls replaced by faults (reproducible with seed)
curl -s localhost:8000/agents/1/try -H 'content-type: application/json' \
  -d '{"prompt": "What is 17 * 23 + 4?", "fault_rate": 0.5, "seed": 7}'
```

Example `/try` response (abridged, shape of a live run of "GPT-OSS 20B · tools" through Bifrost):

```json
{
  "id": 12, "agent_name": "GPT-OSS 20B · tools", "model": "groq/openai/gpt-oss-20b",
  "status": "ok", "output": "17 × 23 + 4 equals 395.",
  "prompt_tokens": 612, "completion_tokens": 131, "latency_ms": 1290.4,
  "steps": 2, "tool_calls": 1, "faults_injected": 0, "retries": 0, "recovered": false,
  "trace": [
    {"idx": 0, "type": "llm_call",    "payload": {"tool_calls": [{"name": "calculator", "arguments": {"expression": "17 * 23 + 4"}}]}},
    {"idx": 1, "type": "tool_call",   "payload": {"name": "calculator", "arguments": {"expression": "17 * 23 + 4"}}},
    {"idx": 2, "type": "tool_result", "payload": {"name": "calculator", "ok": true, "content": "17 * 23 + 4 = 395"}},
    {"idx": 3, "type": "llm_call",    "payload": {"content": "17 × 23 + 4 equals 395."}},
    {"idx": 4, "type": "final",       "payload": {"content": "17 × 23 + 4 equals 395."}}
  ]
}
```

Trace step types: `llm_call`, `tool_call`, `tool_result` (with `fault` when injected), `error`, `retry`, `final`.
See [agents.md](agents.md) for the runtime semantics.

## Suites

| Method | Path | Description |
|---|---|---|
| GET | `/suites` | List suites (`builtin`, `runs`, task count, categories, number of time-sensitive tasks) |
| GET | `/suites/{id}` | Suite with all its tasks (each with `executions`: how many runs used it) |
| POST | `/suites` | Create a custom suite, tasks optional → `201` (`409` duplicate name, case-insensitive; `422` invalid task or duplicate keys) |
| PUT | `/suites/{id}` | Rename / re-describe a custom suite |
| POST | `/suites/{id}/duplicate?name=` | Copy any suite (built-ins too) into a new editable suite → `201` |
| DELETE | `/suites/{id}` | Delete a custom suite → `204` (`409` if any run uses it) |
| POST | `/suites/{id}/tasks` | Add a task → `201` (`409` duplicate key) |
| PUT | `/suites/{id}/tasks/{task_id}` | Edit a task |
| DELETE | `/suites/{id}/tasks/{task_id}` | Delete a task → `204` (`409` if an execution used it) |
| GET | `/suites/{id}/export?format=yaml\|csv` | Download the suite as YAML (the seed format) or CSV |
| POST | `/suites/import?format=yaml\|csv&name=` | Create a suite from a file sent as the raw body (`text/plain`, max 1 MB) → `201` with `warnings`; per-task/line errors → `422` |
| POST | `/scoring/preview` | Score a sample `output` with `scorer`, `expected`, `tolerance` (no LLM) → `score`, `passed`, `detail`, `expected_rendered` |

Built-in suites are read-only: every edit or delete returns `403` ("duplicate it to edit"). Custom suites
need no access code, even on the public demo.

```bash
curl -s localhost:8000/suites -H 'content-type: application/json' -d '{
  "name": "My checks",
  "tasks": [
    {"key": "sq", "prompt": "What is 12 squared?", "expected": "144", "scorer": "numeric"},
    {"key": "pm", "prompt": "Who is the UK Prime Minister?", "expected": "Burnham", "time_sensitive": true}
  ]
}'
# import a CSV file, export as YAML
curl -s "localhost:8000/suites/import?format=csv&name=My%20CSV" -H 'content-type: text/plain' --data-binary @tasks.csv
curl -s "localhost:8000/suites/4/export?format=yaml" -o my-suite.yaml
```

## Runs

| Method | Path | Description |
|---|---|---|
| POST | `/runs` | Start a run → `202` with the run (`422` on invalid suite/agents or > 2000 executions) |
| GET | `/runs?limit=50` | Runs, newest first |
| GET | `/runs/{id}` | Status (`pending`, `running`, `done`, `cancelled`, `failed`), `progress_done/total`, duration |
| GET | `/runs/{id}/metrics` | Leaderboard plus per-agent metrics and the task × agent matrix (partial while running). See [metrics.md](metrics.md). |
| GET | `/runs/{id}/executions` | Every execution (answer, pass/fail, `score_detail`, tokens, cost, faults…) without traces |
| POST | `/runs/{id}/cancel` | Cancel a pending or running run (finished executions are kept) |
| DELETE | `/runs/{id}` | Delete a finished run and its executions (`409` while running) |

```bash
# 3 agents × 8 tasks × 3 repeats with 20% tool faults
curl -s localhost:8000/runs -H 'content-type: application/json' \
  -d '{"suite_id": 3, "agent_ids": [1, 2, 4], "repeats": 3, "fault_rate": 0.2}'
curl -s localhost:8000/runs/1            # poll progress
curl -s localhost:8000/runs/1/metrics    # leaderboard
```

Illustrative `/runs/{id}/metrics` leaderboard row (abridged):

```json
{"rank": 1, "agent_name": "GPT-OSS 120B · tools", "model": "groq/openai/gpt-oss-120b",
 "executions": 6, "passed": 5, "accuracy": 0.83, "consistency": null, "recovery_rate": null,
 "error_rate": 0.0, "latency_ms_p50": 4100, "latency_ms_p95": 9800, "tokens_total": 24044,
 "cost_usd_total": 0.00449, "reliability_score": 0.91,
 "reliability_weights": {"accuracy": 0.727, "stability": 0.273}}
```

`POST /runs` also takes `check_freshness` (default `true`): run the stale-answer detector on answers to
time-sensitive tasks. Executions then carry `freshness_score` and `freshness_verdict`, and the leaderboard rows
carry `freshness`, `freshness_checked`, `stale_answers`, `stale_rate` and `freshness_verdicts`.

## Model connections

Bring your own model: any OpenAI-compatible endpoint. Agents use `custom/<slug>/<model>`. Details and security:
[models.md](models.md). On the public demo, every write and test endpoint needs `X-Access-Code`.

| Method | Path | Description |
|---|---|---|
| GET | `/connections/presets` | Known endpoints (Ollama, LM Studio, vLLM, llama.cpp, Jan, OpenAI, Anthropic, Gemini, Mistral…) with base URL and whether a key is needed |
| GET | `/connections` | Connections: `has_api_key` + `key_hint` (never the key), `models`, `model_ids`, last test result, `agents` using it |
| POST | `/connections` | Add → `201` (`409` name or slug taken; `422` bad or disallowed URL, unknown provider). Body: `name`, `base_url`, `models`, optional `api_key`, `slug`, `preset`, prices per 1M tokens, and `provider` + `via_bifrost` to register a hosted provider in Bifrost. Response: `route` (`bifrost`/`direct`), `route_reason`, `bifrost_key_status`. |
| PUT | `/connections/{id}` | Edit (`api_key` null keeps the stored key; `clear_api_key: true` removes it). The slug never changes. |
| DELETE | `/connections/{id}` | Delete → `204` (`409` while agents use it) |
| POST | `/connections/test` | Check an unsaved endpoint: list models, and with `model` a one-word chat plus a tool-calling check |
| POST | `/connections/{id}/test` | Same with the stored key; records discovered models, tool support and the result |

```bash
curl -s localhost:8000/connections -H 'content-type: application/json' \
  -d '{"name": "My Ollama", "base_url": "http://localhost:11434/v1", "models": ["granite4:3b"]}'
curl -s localhost:8000/connections/1/test -X POST -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/agents -H 'content-type: application/json' \
  -d '{"name": "Granite (mine)", "model": "custom/my-ollama/granite4:3b", "tools": ["calculator"]}'
```

`POST/PUT /agents` reject `custom/<slug>/…` when no connection has that slug (`422`). `GET /models` (live)
lists connection models together with Bifrost's.

## Freshness

Stale-answer detection: claims → Tavily → verdict. How it works: [stale-answer-detection.md](stale-answer-detection.md).

| Method | Path | Description |
|---|---|---|
| POST | `/freshness/check` | Check `text` (with an optional `question`) **or** an `execution_id` → `201` with claims, sources and score. `search_depth`: `advanced` (default) or `basic`. Exactly one of `text`/`execution_id` (`422` otherwise); `404` for an unknown execution. Live mode takes 5–20 s. |
| GET | `/freshness/checks?limit=30&execution_id=&manual_only=` | Recent checks, newest first, without claims. `manual_only=true` hides the automatic checks from benchmark runs. |
| GET | `/freshness/checks/{id}` | One check with every claim and its sources |
| DELETE | `/freshness/checks/{id}` | Delete a check (needs the access code on the public demo) |

```bash
curl -s localhost:8000/freshness/check -H 'content-type: application/json'   -d '{"text": "The current UK PM is Rishi Sunak. Water boils at 100 °C at sea level."}'
# an agent's answer (its prompt becomes the question):
curl -s localhost:8000/freshness/check -H 'content-type: application/json' -d '{"execution_id": 42}'
```

Response (abridged, live):

```json
{"id": 7, "verdict": "stale", "freshness_score": 0.0, "extraction": "llm", "source": "text",
 "counts": {"current": 0, "outdated": 1, "contradicted": 0, "unverifiable": 0, "not_time_sensitive": 1},
 "claims": [
   {"text": "Rishi Sunak is the current Prime Minister of the United Kingdom.", "time_sensitive": true,
    "query": "current UK prime minister", "topic": "news", "verdict": "outdated", "confidence": 0.98,
    "correction": "Andy Burnham is the current Prime Minister of the United Kingdom.",
    "explanation": "Multiple sources from July 2026 report that Andy Burnham succeeded Keir Starmer…",
    "method": "llm",
    "sources": [{"title": "Andy Burnham succeeds Keir Starmer as the U.K.'s 7th prime minister…",
                 "url": "https://www.npr.org/…", "published_date": "2026-07-20", "snippet": "…"}]},
   {"text": "Water boils at 100 °C at sea level.", "time_sensitive": false,
    "verdict": "not_time_sensitive", "method": "skipped", "sources": []}]}
```

`GET /executions/{id}` includes `freshness_verdict` and `freshness_check_id` (the latest check of that answer).

## Reports

| Method | Path | Description |
|---|---|---|
| GET | `/reports/{run_id}?format=md` | Readable report: highlights, leaderboard, reliability detail, accuracy by category, pass rate per task, freshness and stale answers (with what is true today and the newest source), failed executions |
| GET | `/reports/{run_id}?format=csv&table=leaderboard` | One row per agent, every metric (raw numbers: ratios 0–1, latency in ms, cost in USD) |
| GET | `/reports/{run_id}?format=csv&table=executions` | One row per attempt (answer, pass/fail, why, tokens, cost, faults, freshness verdict) |
| GET | `/reports/{run_id}?format=json` | Everything above plus every execution, as one document (`report_version: 1`) |

Responses are file downloads (`Content-Disposition: attachment; filename="agentpulse-run-<id>….<ext>"`). Add
`download=false` to view inline. Numbers come from the same code as `GET /runs/{id}/metrics`, so they always
match the dashboard. A run still in progress gives a report marked *partial*. `404` for an unknown run, `422`
for an unknown format.

```bash
curl -s "localhost:8000/reports/1?format=md" -o run-1.md
curl -s "localhost:8000/reports/1?format=csv&table=executions" -o run-1-executions.csv
```

Example generated from a live run: [examples/live-run-report.md](examples/live-run-report.md)
([leaderboard CSV](examples/live-run-leaderboard.csv)).
