# AgentPulse

[![CI](https://github.com/UTSAVS26/AgentPulse/actions/workflows/ci.yml/badge.svg)](https://github.com/UTSAVS26/AgentPulse/actions/workflows/ci.yml)

**AI agent benchmarking & reliability platform** with built-in **stale-answer detection**.

> Global AI Hackathon – Geek Room · Bifrost challenge (agent benchmarking) + Tavily challenge (stale-answer detector)

AgentPulse runs multiple AI agents on identical tasks and compares them on **accuracy, consistency,
failure recovery, tokens, cost, latency and execution steps**. It also checks whether their answers are
still true today, extracting time-sensitive claims and verifying them against live web sources.

| Capability | Powered by |
|---|---|
| Agent execution, multi-provider routing (Groq, OpenRouter, Ollama) | [Bifrost](https://github.com/maximhq/bifrost) AI gateway |
| Live search & claim verification | [Tavily](https://docs.tavily.com/) |
| Scoring, reliability & freshness analytics | AgentPulse evaluation engine |
| API / UI / storage | FastAPI · Streamlit · PostgreSQL |

> **Status:** Phase 3 of 7. Benchmark engine with scoring, repeated runs, cost from Bifrost pricing, reliability metrics and the Run Benchmark page. See [CHANGELOG.md](CHANGELOG.md).

## Live demo

**Hugging Face Space:** auto-deployed from `main` after CI passes (see [docs/deployment.md](docs/deployment.md)).
Everyone gets the full product in **mock mode**. Judges can unlock **live mode** (real models via Bifrost +
live Tavily) with an access code from the team.

## Quickstart

```bash
make setup     # .venv + dev deps + .env (MOCK_MODE=true works with no keys at all)
make up        # build & start everything, waits until all services are healthy
make health    # check backend, Bifrost and dashboard
```

Run `make` with no target to list all commands. Without `make`, the equivalent is
`cp .env.example .env && docker compose up --build -d --wait`.
On Windows, `make` is available via `choco install make`.

| Service | URL |
|---|---|
| Dashboard (Streamlit) | http://localhost:8501 |
| API (FastAPI) + OpenAPI docs | http://localhost:8000/docs |
| Bifrost gateway | http://localhost:8080 |
| PostgreSQL | localhost:5432 |

Full instructions, API keys, Ollama setup and troubleshooting: **[docs/setup.md](docs/setup.md)**.

## Documentation

- [Setup & configuration](docs/setup.md): keys, Ollama, mock vs live mode, troubleshooting
- [Architecture](docs/architecture.md): components, clients, data model
- [Bifrost integration](docs/bifrost.md): gateway config, model ids, error handling, live check
- [Agents](docs/agents.md): agent definition, default agents, tools, runtime loop, fault injection
- [Benchmarks](docs/benchmarks.md): suites, task format, scorers, how runs execute, cost
- [Metrics](docs/metrics.md): every formula and the reliability score
- [Deployment](docs/deployment.md): Hugging Face Space auto-deploy, access code, secrets
- [Continuous integration](docs/ci.md): what runs on every push and how to run it locally
- [API reference](docs/api.md): endpoints with curl examples (full spec at http://localhost:8000/docs)

## Repository layout

```
backend/     FastAPI service (benchmark engine, agent runtime, freshness verifier)
frontend/    Streamlit dashboard
bifrost/     Bifrost gateway config (providers)
docs/        Project documentation
```

## Development

| Command | What it does |
|---|---|
| `make setup` | Create `.venv`, install dev dependencies, create `.env` |
| `make up` / `make up-live` | Start the stack in the `.env` mode / forced live mode (`MOCK_MODE=false`) |
| `make down` / `make reset` | Stop (keep data) / stop and wipe volumes |
| `make restart` | Recreate backend + frontend (after `.env` changes) |
| `make logs` / `make ps` / `make health` | Observe the stack |
| `make ollama-test` | Prompt Ollama through Bifrost (`make ollama-test OLLAMA_MODEL=qwen3.5:9b` to change model) |
| `make llm-test MODEL=<id>` / `make models` | Prompt any model through Bifrost / list reachable models |
| `make live-check` | Test real Bifrost providers, tool calling and Tavily end to end |
| `make db-tables` | List PostgreSQL tables |
| `make test` / `make lint` / `make format` | Tests / lint check / auto-fix |
| `make ui-test` | Render dashboard pages against the running stack (needs `make up`) |
| `make validate-config` | Validate `bifrost/config.json` against Bifrost's JSON schema |
| `make space-smoke` | Build the Hugging Face Space image, run it like HF does, and smoke-test it |
| `make check` | Lint + config validation + tests (what CI runs). **Run before every commit.** |
