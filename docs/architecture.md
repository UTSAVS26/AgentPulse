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
| **Streamlit frontend** (`frontend/`) | Dashboard pages. Talks only to the backend through `frontend/api.py`, which turns every failure into a friendly message. | Home, Agents, Run Benchmark pages (`ui.py` shared helpers) |
| **FastAPI backend** (`backend/app/`) | Orchestration, agent runtime, scoring, metrics, freshness verification, reports. OpenAPI at `/docs`. | Agents, suites, runs APIs; agent runtime; benchmark engine (scoring, pricing, metrics) |
| **Bifrost** (`bifrost/config.json`) | Single OpenAI-compatible endpoint for all LLM providers. It holds the provider keys, so the backend never sees them. Models are addressed as `provider/model`. | Groq, OpenRouter, Ollama, all verified live |
| **Tavily** | Live web search used by the agents' `web_search` tool and by the stale-answer verifier. | Client ready, verified live |
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
  mock_search.py  MockSearchClient: dated demo corpus + generic fallback
  __init__.py     get_llm_client() / get_search_client(): chosen once per process by MOCK_MODE
```

The rest of the code depends only on the `LLMClient` / `SearchClient` protocols, so mock and
real mode run identical code paths above the client layer. FastAPI routes receive clients through
`Depends(get_llm_client)`, which tests override. Gateway details are in [bifrost.md](bifrost.md).

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
| `benchmark_runs` | One run of a suite × agents × repeats | `agent_ids`, `repeats`, `fault_rate`, `mock_mode`, `status`, `progress_done/total`, timestamps |
| `task_executions` | One agent attempting one task once | `output`, `status` (`ok\|error\|max_steps`), `score`, `passed`, token counts, `cost_usd`, `latency_ms`, `steps`, `tool_calls`, `faults_injected`, `retries`, `recovered`, `freshness_score`. `run_id` is NULL for ad-hoc "try agent" runs. |
| `trace_steps` | Ordered steps of an execution | `idx`, `type` (`llm_call\|tool_call\|tool_result\|error\|retry\|final`), `payload` (JSON), tokens, `latency_ms` |
| `freshness_checks` | One stale-answer analysis | `input_text`, optional `execution_id`, `freshness_score` |
| `claims` | One extracted claim | `text`, `time_sensitive`, `query`, `sources` (JSON: url, title, published_date), `verdict` (`current\|outdated\|contradicted\|unverifiable\|not_time_sensitive`), `confidence`, `explanation` |

Schema changes are additive. `add_missing_columns()` adds new nullable columns to existing databases on startup, so upgrading never requires wiping data.

Deletes cascade from run → executions → trace steps, and from check → claims. Agents and suites are never deleted by a run.
