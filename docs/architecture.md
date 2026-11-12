# Architecture

## System overview

```
┌──────────────────────┐   HTTP    ┌──────────────────────┐  OpenAI-compatible  ┌──────────────────────┐
│ Streamlit dashboard  │ ────────▶ │  FastAPI backend     │ ──────────────────▶ │  Bifrost AI gateway  │
│ :8501                │           │  :8000               │  /v1/chat/...       │  :8080               │
└──────────────────────┘           │                      │                     └──────────┬───────────┘
                                   │  • agent runtime     │                                │
                                   │  • benchmark engine  │             ┌──────────────────┼───────────────────┐
                                   │  • scoring/metrics   │             ▼                  ▼                   ▼
                                   │  • freshness checker │       groq/<model>    openrouter/<model>   ollama/<model>
                                   └──────┬────────┬──────┘                                          (host :11434)
                                          │        │ HTTPS
                                          │        ▼
                                          │  ┌──────────────┐
                                          │  │  Tavily API  │  live search, claim verification
                                          │  └──────────────┘
                                          ▼
                                   ┌──────────────┐
                                   │ PostgreSQL   │  agents, runs, executions, traces, freshness checks
                                   │ :5432        │
                                   └──────────────┘
```

## Components

| Component | Responsibility | Status |
|---|---|---|
| **Streamlit frontend** (`frontend/`) | Dashboard pages. Talks only to the backend through `frontend/api.py`, which turns every failure into a friendly message. | Home, Agents, Run Benchmark, Compare, Traces, Stale-Answer Detector, Reports (`ui.py` helpers, `charts.py` Plotly builders) |
| **FastAPI backend** (`backend/app/`) | Orchestration, agent runtime, scoring, metrics, freshness verification, reports. OpenAPI at `/docs`. | Agents, suites, runs, freshness, reports APIs; agent runtime; benchmark engine (scoring, pricing, metrics); stale-answer detector; demo-data seeding |
| **Model connections** (`routers/connections.py`, `clients/router.py`, `connection_routes.py`, `clients/bifrost_admin.py`) | User-added models: hosted providers registered in Bifrost at runtime (self-healing after Bifrost restarts), everything else called directly. Keys are encrypted at rest (`crypto.py`) and URLs checked by `net_guard.py`. See [models.md](models.md). | Verified live with local Ollama |
| **Bifrost** (`bifrost/config.json`) | Single OpenAI-compatible endpoint for all LLM providers. It holds the provider keys, so the backend never sees them. Models are addressed as `provider/model`. | Groq, OpenRouter, Ollama, all verified live |
| **Tavily** | Live web search used by the agents' `web_search` tool and by the stale-answer detector. | Verified live (agents and detector) |
| **PostgreSQL** | Persistent results, history and traces. | 8 tables, created on startup |

## Mock and live mode per request

`app/mode.py` decides **per request**: `MOCK_MODE=false` means always live. Otherwise a request is live only if it
carries a valid `X-Access-Code`. Both client sets (mock and Bifrost/Tavily) exist side by side, every run stores
the mode it ran in, and the engine picks the matching clients per run. This is what lets one public deployment
serve a free mock demo and gated live runs at the same time.

## Key design decisions

- **The gateway is the only path to LLMs.** Every agent, the LLM judge and the claim extractor call Bifrost. This makes provider comparisons fair (identical request path) and lets Bifrost's logs double as an audit trail.
- **Mock mode** (`MOCK_MODE=true`) swaps the LLM and search clients for deterministic fakes with the same interface, so the full product can be demoed offline and tested without keys.
- **Fail soft.** Every outbound call has a timeout. A provider failure marks a single execution as `error` and never aborts a run or crashes the UI.
- **Pinned everything.** Python dependencies are pinned exactly, and the Bifrost image is pinned by digest.

### Clients and mock mode

```
backend/app/clients/
  llm.py          LLMClient protocol, LLMResponse/ToolCall/LLMError, BifrostClient (httpx)
  mock_llm.py     MockLLMClient: per-model profiles, tool use, recovery, answer key, responders
  search.py       SearchClient protocol, SearchResponse/SearchResult/SearchError, TavilySearchClient
  mock_search.py  MockSearchClient: dated demo corpus (current + stale answers per topic) + generic fallback
  mock_freshness.py  mock replies to the stale-answer detector's extraction/judge prompts
  router.py       ModelRouter: custom/<slug>/<model> -> the connection's OpenAI-compatible client, else Bifrost
  __init__.py     clients_for(live) -> (LLM, search) pair; live LLM = ModelRouter(Bifrost, connections)
```

The rest of the code depends only on the `LLMClient` / `SearchClient` protocols, so mock and
real mode run identical code paths above the client layer. FastAPI routes receive the pair for the
request's mode through `Depends(get_mode)` (`app/mode.py`). Gateway details are in [bifrost.md](bifrost.md).

### Stale-answer detector

```
backend/app/freshness/
  prompts.py     extraction and judge prompts (JSON in, JSON out)
  heuristics.py  rule-based claim extraction and time-sensitivity signals (fallback + override)
  checker.py     FreshnessChecker: extract -> clock check or Tavily search -> LLM verdict -> score;
                 model fallback with rate-limit cooldowns; per-run cache; never raises
  store.py       persist checks/claims, build API responses
```

Used by `POST /freshness/check` (`routers/freshness.py`) and by the benchmark engine on answers to time-sensitive
tasks. Details: [stale-answer-detection.md](stale-answer-detection.md).

### Reports and demo data

- `app/reports.py` builds a run report from `metrics_payload()` and `execution_rows()` in `routers/runs.py`
  (the same functions behind `/runs/{id}/metrics` and `/runs/{id}/executions`) and renders it as Markdown, CSV
  or JSON (`GET /reports/{id}`).
- `app/seed/demo.py`: on a database with no runs, startup seeds two example freshness checks, then, on its own
  thread (so startup stays fast and the engine's run workers stay free), a mock Freshness Bench run and two
  mock benchmark runs. See [setup.md](setup.md#demo-data).
- `app/guard/` + `app/routers/guard.py`: the Freshness Guard, an OpenAI-compatible proxy that forwards a chat
  request through the model router, runs the stale-answer detector on the answer and annotates, corrects or
  blocks it; `app/guard/auth.py` authenticates callers (API keys, OIDC ID tokens, scopes, rate limits). See [guard.md](guard.md).
- `app/freshbench/`: the Live Freshness Bench: question generator (Tavily news by age window → writer →
  validator), background jobs, the knowledge-cutoff curve and the daily pulse. See [freshbench.md](freshbench.md).

## Data model

Defined in `backend/app/models.py` (SQLAlchemy 2.0). Generic column types run on PostgreSQL in
Docker and on SQLite in tests. Tables are created at startup by `init_db()`, which retries while
Postgres is still booting.

```
agents ─────────────┐
                    │ agent_id
task_suites ─< tasks│
     │              ▼
     └─< benchmark_runs ─< task_executions ─< trace_steps
                               │
                               └─< freshness_checks ─< claims
```

| Table | Purpose | Key columns |
|---|---|---|
| `agents` | An agent = model + prompt + tools + step budget | `name` (unique), `model` (Bifrost id), `system_prompt`, `tools` (JSON list), `temperature`, `max_steps` |
| `task_suites` | Named benchmark suite | `name` (unique), `description` |
| `tasks` | One benchmark question | `key`, `prompt`, `expected`, `scorer` (`exact\|contains\|numeric\|regex\|judge`), `tolerance`, `category`, `time_sensitive` |
| `benchmark_runs` | One run of a suite × agents × repeats | `agent_ids`, `repeats`, `fault_rate`, `mock_mode`, `check_freshness`, `status`, `progress_done/total`, timestamps |
| `task_executions` | One agent attempting one task once | `output`, `status` (`ok\|error\|max_steps`), `score`, `passed`, token counts, `cost_usd`, `latency_ms`, `steps`, `tool_calls`, `faults_injected`, `retries`, `recovered`, `freshness_score`, `freshness_verdict`. `run_id` is NULL for ad-hoc "try agent" runs. |
| `trace_steps` | Ordered steps of an execution | `idx`, `type` (`llm_call\|tool_call\|tool_result\|error\|retry\|final`), `payload` (JSON), tokens, `latency_ms` |
| `freshness_checks` | One stale-answer analysis | `input_text`, `question`, optional `execution_id`, `freshness_score`, `verdict` (`fresh\|stale\|wrong\|unverified\|timeless\|no_claims`), `extraction`, `mock_mode`, `source` (`text\|execution\|benchmark`), `latency_ms` |
| `claims` | One extracted claim | `text`, `time_sensitive`, `query`, `sources` (JSON: url, title, published_date), `verdict` (`current\|outdated\|contradicted\|unverifiable\|not_time_sensitive`), `confidence`, `explanation`, `correction`, `topic`, `signals`, `method` |

Schema changes are additive. `add_missing_columns()` adds new nullable columns to existing databases on startup, so upgrading never requires wiping data.

Deletes cascade from run → executions → trace steps, and from check → claims. Agents and suites are never deleted by a run.
