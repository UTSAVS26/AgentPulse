# Changelog

All notable changes per build phase.

## [CI] Continuous integration

### Added
- GitHub Actions workflow (`.github/workflows/ci.yml`) on every push and pull request:
  lint + Bifrost config schema validation + compose validation · backend tests · full Docker stack in mock mode with health checks, seed checks, dashboard (AppTest) tests and a log scan for tracebacks.
  An optional **live** integration check runs manually (`workflow_dispatch`) using repository secrets.
- `scripts/validate_bifrost_config.py` + committed schema snapshot `bifrost/config.schema.json` (offline fallback); `make validate-config`, now part of `make check`.
- `.gitattributes` enforcing LF line endings (the Makefile and Docker builds need them).
- Docs: `docs/ci.md`, CI badge in README.

### Fixed
- **Pricing was never loaded on a recently booted machine** (found by CI). `PriceBook` treated "never refreshed" as `monotonic() == 0`, and `time.monotonic()` counts from boot on Linux, so within the first hour after boot costs fell back to the snapshot or $0. It now refreshes on first use, re-checks after the TTL, and retries 60 s after a failed fetch (instead of 1 h). Regression tests fake a freshly booted clock.
- The Run Benchmark UI test assumed no other run is started while it waits; it now selects its own run explicitly and starts a concurrent run on purpose to cover that case.

## [Phase 3] Benchmark engine, scoring & metrics

### Added
- 3 seeded task suites (22 tasks): Factual QA, Math & Reasoning, Current Events (time-sensitive). Current answers were verified with Tavily, with `stale_answer` hints.
- Scorers: `contains` (with alternatives), `exact`, `numeric` (tolerance, thousands separators, final-sentence matching), `regex`, `judge` (LLM via Bifrost, falls back to `contains`). Date placeholders are supported in expected answers.
- Benchmark engine: background runs, agents in parallel and tasks sequential per agent, a commit per execution, live progress, cancel, crash isolation, and interrupted-run recovery on startup.
- Cost from **Bifrost's model catalogue pricing** (cached), with a dated snapshot fallback. Local Ollama costs $0.
- Metrics: accuracy, consistency, pass@k, pass-all-k, error rate, recovery rate, latency p50/p95, tokens, cost, cost per correct answer, accuracy by category, task × agent matrix, and the composite **reliability score** (documented weights, renormalised when components are undefined).
- APIs: `/suites` (CRUD), `/runs` (start, list, status, executions, metrics, cancel, delete).
- Dashboard: **Run Benchmark** page (configure, live progress via `st.fragment`, partial leaderboard, cancel, executions table).
- Runtime: rate-limit-aware retries (429 → 2 s…30 s backoff, 5 retries) and **corrective retry for provider-rejected tool calls** (found in live Groq runs).
- Mock LLM: "stale knowledge" for time-sensitive tasks without search.
- Auto-migration: new nullable columns are added to existing databases on startup.
- Tests: 54 new backend tests (145 total) and 2 new UI tests (7 total).
- Docs: `docs/benchmarks.md`, `docs/metrics.md`, plus the suites and runs sections in `docs/api.md`.

## [Phase 2] Agent runtime, tools & tracing

### Added
- Tools: `web_search` (Tavily), `calculator` (safe AST evaluator, no `eval`), `current_date`, in OpenAI function-calling format.
- Agent runtime: tool-calling loop with `max_steps`, LLM retries with exponential backoff (retryable errors only), fault injection (`timeout`, `http_500`, `empty`, `malformed`) with a seeded RNG, and a full trace of every model call, tool call, result, error and retry.
- Agents API: CRUD, `GET /tools`, `POST /agents/{id}/try`, `GET /executions/{id}`, `GET /agents/{id}/executions`.
- 6 default agents (Groq ×3 incl. a no-tools baseline, OpenRouter free, local Ollama), seeded idempotently from YAML. All verified live with tool calling.
- Dashboard: **Agents** page (list, create, edit, delete, try with fault injection, trace timeline). Shared `ui.py` (sidebar status, metrics, trace renderer).
- Tests: 53 new backend tests (91 total) for tools, runtime and the agents API, plus 5 Streamlit AppTest UI tests (`make ui-test`).
- Docs: `docs/agents.md`, `docs/api.md`.

### Changed
- Default `LLM_TIMEOUT_S` raised to 120 s (cold-starting local Ollama models can take over 50 s).
- `pandas` is pinned in the frontend.

## [Phase 1] Database & LLM/search clients

### Added
- PostgreSQL schema (SQLAlchemy 2.0): `agents`, `task_suites`, `tasks`, `benchmark_runs`, `task_executions`, `trace_steps`, `freshness_checks`, `claims`. Tables are created on startup, with retries while the database boots.
- `BifrostClient`: OpenAI-compatible chat (with tool calling) over `httpx`, model listing, health ping, and retryable vs. non-retryable error classification.
- `TavilySearchClient`: REST search with topic and depth options, normalised `published_date`, and error classification.
- `MockLLMClient`: deterministic per-model profiles, tool selection, recovery after tool errors, accuracy-weighted answers from an answer key, and pluggable responders.
- `MockSearchClient`: dated demo corpus and generic fallback results.
- `/health` now reports `database`, `llm` and `search` components with an `ok`/`degraded` status. New `/models` endpoint.
- `make live-check` (`app/scripts/live_check.py`): real end-to-end check of Bifrost (3 providers plus tool calling) and Tavily. `make db-tables`.
- 37 new unit tests (38 total), covering HTTP error mapping via mock transports, mock behaviours and DB cascades.
- Docs: `docs/bifrost.md`, data model and clients sections in `docs/architecture.md`, and mock-mode and live-check sections in `docs/setup.md`.

### Changed
- Settings: added `MOCK_SEED` and `MOCK_LATENCY_SCALE`.

## [Phase 0] Project skeleton & infrastructure

### Added
- Docker Compose stack: PostgreSQL 16, Bifrost gateway (pinned by digest), FastAPI backend, Streamlit frontend, all with healthchecks and ordered startup.
- Bifrost config with Groq, OpenRouter and Ollama providers (keys via `env.` references), validated against the official Bifrost JSON schema.
- Backend: `/health` endpoint (version + mock mode), settings via environment variables, OpenAPI docs at `/docs`.
- Frontend: home page with a sidebar showing backend status and mock/live mode, plus an API wrapper with friendly errors.
- Tooling: pytest, ruff (lint + format), pinned dependencies, `.env.example`.
- `Makefile` with setup, stack, health, Ollama test and quality targets. It works from PowerShell/cmd, Git Bash, macOS and Linux.
- Docs: README, `docs/setup.md`, `docs/architecture.md`.
