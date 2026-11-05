# Changelog

All notable changes per build phase.

## [Phase 0] Project skeleton & infrastructure

### Added
- Docker Compose stack: PostgreSQL 16, Bifrost gateway (pinned by digest), FastAPI backend, Streamlit frontend, all with healthchecks and ordered startup.
- Bifrost config with Groq, OpenRouter and Ollama providers (keys via `env.` references), validated against the official Bifrost JSON schema.
- Backend: `/health` endpoint (version + mock mode), settings via environment variables, OpenAPI docs at `/docs`.
- Frontend: home page with a sidebar showing backend status and mock/live mode, plus an API wrapper with friendly errors.
- Tooling: pytest, ruff (lint + format), pinned dependencies, `.env.example`.
- `Makefile` with setup, stack, health, Ollama test and quality targets. It works from PowerShell/cmd, Git Bash, macOS and Linux.
- Docs: README, `docs/setup.md`, `docs/architecture.md`.
