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
| GET | `/suites` | List suites (task count, categories, number of time-sensitive tasks) |
| GET | `/suites/{id}` | Suite with all its tasks |
| POST | `/suites` | Create a custom suite → `201` (`409` duplicate name, `422` invalid scorer or duplicate task keys) |
| DELETE | `/suites/{id}` | Delete → `204` (`409` if any run uses it) |

```bash
curl -s localhost:8000/suites -H 'content-type: application/json' -d '{
  "name": "My checks",
  "tasks": [
    {"key": "sq", "prompt": "What is 12 squared?", "expected": "144", "scorer": "numeric"},
    {"key": "pm", "prompt": "Who is the UK Prime Minister?", "expected": "Burnham", "time_sensitive": true}
  ]
}'
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
