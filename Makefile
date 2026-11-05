# AgentPulse developer commands.
# Works from PowerShell/cmd (Windows, GNU make via `choco install make`), Git Bash, macOS and Linux.
# Recipes avoid shell-specific syntax so they run under both cmd.exe and sh.

ifeq ($(OS),Windows_NT)
  PYTHON ?= python
  PY := ".venv/Scripts/python.exe"
else
  PYTHON ?= python3
  PY := ".venv/bin/python"
endif

COMPOSE := docker compose
OLLAMA_MODEL ?= granite4:3b
MODEL ?= groq/openai/gpt-oss-20b

.DEFAULT_GOAL := help
.PHONY: help setup env up up-live down reset restart build logs ps health ollama-test llm-test models live-check db-tables \
        test lint format check

help:
	$(info AgentPulse - available commands)
	$(info )
	$(info   Setup)
	$(info     make setup        Create .venv, install dev deps, create .env from .env.example)
	$(info     make env          Create .env from .env.example (if missing))
	$(info )
	$(info   Stack (Docker Compose))
	$(info     make up           Build and start all services in default mode from .env, wait until healthy)
	$(info     make up-live      Same, but force MOCK_MODE=false (real Bifrost + Tavily))
	$(info     make down         Stop services, keep data)
	$(info     make reset        Stop services and WIPE volumes (database + Bifrost state))
	$(info     make restart      Recreate backend + frontend (picks up .env changes))
	$(info     make build        Rebuild images)
	$(info     make logs         Follow logs of all services)
	$(info     make ps           Show service status)
	$(info     make health       Check backend + Bifrost + frontend health endpoints)
	$(info     make ollama-test  Send a test prompt through Bifrost to Ollama (OLLAMA_MODEL=$(OLLAMA_MODEL)))
	$(info     make llm-test     Send a test prompt to any model, e.g. make llm-test MODEL=groq/openai/gpt-oss-20b)
	$(info     make models       List every model reachable through Bifrost)
	$(info     make live-check   Test REAL Bifrost providers + tool calling + Tavily (ignores MOCK_MODE))
	$(info     make db-tables    List database tables in PostgreSQL)
	$(info )
	$(info   Quality)
	$(info     make test         Run backend tests)
	$(info     make lint         Ruff lint + format check)
	$(info     make format       Auto-fix lint issues and format code)
	$(info     make check        lint + test (run before every commit))
	@:

# ---------------------------------------------------------------- setup
setup: env
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r backend/requirements-dev.txt -r frontend/requirements.txt

env:
	$(PYTHON) -c "import os, shutil; os.path.exists('.env') or shutil.copyfile('.env.example', '.env'); print('.env ready')"

# ---------------------------------------------------------------- stack
up: env
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) ps

up-live: export MOCK_MODE=false
up-live: env
	$(COMPOSE) up -d --build --wait
	$(COMPOSE) ps

down:
	$(COMPOSE) down

reset:
	$(COMPOSE) down -v

restart:
	$(COMPOSE) up -d --force-recreate --wait backend frontend

build:
	$(COMPOSE) build

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

health:
	curl -s -w "\n" http://localhost:8000/health
	curl -s -w "\n" http://localhost:8080/health
	curl -s -w "\n" http://localhost:8501/_stcore/health

ollama-test:
	curl -s -w "\n" -m 180 http://localhost:8080/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"ollama/$(OLLAMA_MODEL)\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]}"

llm-test:
	curl -s -w "\n" -m 180 http://localhost:8080/v1/chat/completions -H "content-type: application/json" -d "{\"model\":\"$(MODEL)\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]}"

models:
	curl -s -w "\n" -m 60 http://localhost:8080/v1/models

live-check:
	$(COMPOSE) exec backend python -m app.scripts.live_check

db-tables:
	$(COMPOSE) exec postgres psql -U agentpulse -d agentpulse -c "\dt"

# ---------------------------------------------------------------- quality
test:
	$(PY) -m pytest backend -q -W error

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

check: lint test
