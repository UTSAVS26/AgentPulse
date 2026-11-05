# Changelog

All notable changes per build phase.

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
