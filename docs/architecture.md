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
| **Streamlit frontend** (`frontend/`) | Dashboard pages. Talks only to the backend through `frontend/api.py`, which turns every failure into a friendly message. | Phase 0: home page + status |
| **FastAPI backend** (`backend/app/`) | Orchestration, agent runtime, scoring, metrics, freshness verification, reports. OpenAPI at `/docs`. | Phase 0: `/health` |
| **Bifrost** (`bifrost/config.json`) | Single OpenAI-compatible endpoint for all LLM providers. It holds the provider keys, so the backend never sees them. Models are addressed as `provider/model`. | Running, 3 providers configured |
| **Tavily** | Live web search used by the agents' `web_search` tool and by the stale-answer verifier. | Phase 1+ |
| **PostgreSQL** | Persistent results, history and traces. | Running, schema in Phase 1 |

## Key design decisions

- **The gateway is the only path to LLMs.** Every agent, the LLM judge and the claim extractor call Bifrost. This makes provider comparisons fair (identical request path) and lets Bifrost's logs double as an audit trail.
- **Mock mode** (`MOCK_MODE=true`) swaps the LLM and search clients for deterministic fakes with the same interface, so the full product can be demoed offline and tested without keys.
- **Fail soft.** Every outbound call has a timeout. A provider failure marks a single execution as `error` and never aborts a run or crashes the UI.
- **Pinned everything.** Python dependencies are pinned exactly, and the Bifrost image is pinned by digest.

### Bifrost configuration notes

`bifrost/config.json` is mounted read-only into the container's app dir (`/app/data`). Its runtime state lives in the `bifrost-data` volume.

- `source_of_truth: "config.json"` — the file always wins over state persisted in the volume.
- `client.enforce_auth_on_inference: false` — no virtual key required. This is safe only because the gateway is used on a local or Docker network. Turn it on for any shared deployment.
- `providers.ollama.network_config.allow_private_network: true` — needed because `host.docker.internal` resolves to a private IP, which Bifrost's SSRF guard blocks by default.

## Data model

Defined in Phase 1. See the plan: agents, task suites/tasks, benchmark runs, task executions, trace steps, freshness checks/claims.
