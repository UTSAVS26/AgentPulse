# Continuous integration

Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml). Runs on **every push** (any branch),
every **pull request**, and on demand from the Actions tab.

| Job | What it does | Approx. time |
|---|---|---|
| **Lint & config validation** | `make setup` · `make lint` (ruff lint + format check) · validates `bifrost/config.json` against Bifrost's JSON schema · `docker compose config` | ~1 min |
| **Backend tests** | `make test`: every backend test, warnings treated as errors (SQLite, mock mode, no network) | ~1 min |
| **Docker stack + dashboard tests** | `make up` (real images, **mock mode**) · `make health` · checks 6 agents and 3 suites are seeded · `make ui-test` (Streamlit AppTest against the running stack, including a full benchmark run) · fails if container logs contain a traceback · dumps logs on failure · `docker compose down -v` | ~5–8 min |
| **Deploy image** | `make image-smoke`: builds `deploy/Dockerfile` (the exact image Render deploys), runs it as uid 1000 on port 7860 with a test access code, then 17 checks (seeded data and demo runs, a mock benchmark on SQLite, the stale-answer detector, custom suites, model-connection security and route fallback, access-code rules) plus a log scan | ~4 min |
| **Live integration check** (manual only) | Starts the stack in live mode and runs `app.scripts.live_check` against Groq, OpenRouter and Tavily (no Ollama in CI) | ~3 min |

The CI calls the same `make` targets you use locally, so a green CI also proves the Makefile works on Linux.

Render watches these checks and deploys `main` only after they all pass (`autoDeployTrigger: checksPass` in `render.yaml`). See [deployment.md](deployment.md).

## Run the same checks locally

```bash
make check                  # lint + Bifrost config validation + backend tests   (= first two jobs)
make up && make ui-test     # full stack + dashboard tests                         (= third job)
make live-check             # live integrations                                    (= manual job)
```

## Live check in CI (optional)

It spends a few cents of API credits, so it never runs automatically.

1. GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:
   `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`.
2. **Actions → CI → Run workflow** → tick **"Also run the LIVE integration check"**.

Secrets are only passed to that job. Secrets are never available to workflows triggered from forks.

## Bifrost schema snapshot

`scripts/validate_bifrost_config.py` validates against the live schema at `https://www.getbifrost.ai/schema`.
If that is unreachable it uses the committed snapshot `bifrost/config.schema.json`, so validation **always**
runs. To refresh the snapshot after a Bifrost upgrade:

```bash
.venv/Scripts/python scripts/validate_bifrost_config.py --update-snapshot   # Windows
.venv/bin/python scripts/validate_bifrost_config.py --update-snapshot       # macOS/Linux
```
