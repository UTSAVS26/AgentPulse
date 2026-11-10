# Setup & configuration

## Prerequisites

| Tool | Version tested | Notes |
|---|---|---|
| Docker Desktop | 29.x | Must be running before `docker compose up` |
| Python | 3.12+ (3.13 works locally) | Only needed to run tests and lint outside Docker |
| Ollama | optional | For local open-source models |
| GNU Make | 4.x | Optional shortcut runner. Windows: `choco install make`. Every `make` target below shows its raw command too. |

## 1. Configure environment

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|---|---|---|
| `MOCK_MODE` | `true` | `true`: simulated LLM and search (no keys, works offline). `false`: real Bifrost and Tavily calls. |
| `GROQ_API_KEY` | – | Groq models via Bifrost (`groq/<model>`). Get one at https://console.groq.com |
| `OPENROUTER_API_KEY` | – | OpenRouter models via Bifrost (`openrouter/<model>`). Get one at https://openrouter.ai |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Where Bifrost reaches Ollama on your host |
| `TAVILY_API_KEY` | – | Live web search and claim verification. Get one at https://tavily.com |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `agentpulse` | Database credentials |
| `LLM_TIMEOUT_S` / `SEARCH_TIMEOUT_S` | `120` / `20` | Outbound request timeouts |
| `LIVE_ACCESS_CODE` | – | Public-demo mode: with `MOCK_MODE=true`, requests are mock unless they send this code (dashboard sidebar → *Unlock live mode*). Admin actions also need it. See [deployment.md](deployment.md). |
| `JUDGE_MODEL` | `openrouter/anthropic/claude-sonnet-5.5` | Model used by the `judge` scorer (live mode); from a lab the agents don't use |
| `JURY_MODELS` | `openrouter/anthropic/claude-opus-5.5,openrouter/google/gemini-3.1-pro-preview,openrouter/deepseek/deepseek-v4-pro` | Judges for the `jury` scorer. See [statistics.md](statistics.md). |
| `BIFROST_SETUP_TOKEN` | *(empty)* | Unlocks Bifrost's management API so hosted providers you add on the Models page are registered in the gateway at runtime. Empty = they are called directly. See [models.md](models.md#routes-through-bifrost-or-direct). |
| `APP_SECRET` | *(generated)* | Encrypts API keys stored for your own model connections. Set it for anything long-lived. See [models.md](models.md#security). |
| `ALLOW_PRIVATE_ENDPOINTS` | allowed locally, blocked on the public demo | Whether model connections may point at private or loopback addresses |
| `FRESHNESS_MODEL` / `FRESHNESS_FALLBACK_MODELS` | `openrouter/anthropic/claude-sonnet-5.5` / `groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b,openrouter/nvidia/nemotron-3.5-lightning:free` | Stale-answer detector's extraction + judge model, then fallbacks on rate limits. See [stale-answer-detection.md](stale-answer-detection.md#configuration). |
| `FRESHNESS_SEARCH_DEPTH` | `advanced` | Tavily depth for automatic checks during benchmark runs |

Provider keys are read **only by Bifrost** (see `bifrost/config.json`, which uses `env.` references). The backend never sees them.

## 2. Start the stack

```bash
make up                  # = docker compose up --build -d --wait && docker compose ps
                         # all 4 services should show (healthy)
```

`--wait` blocks until every healthcheck passes. Startup order is enforced:
`postgres` + `bifrost` → `backend` → `frontend`.

## 3. Verify

```bash
make health
# curl http://localhost:8000/health   -> {"status":"ok","mock_mode":true,"components":{"database":{"ok":true,...},"llm":{...},"search":{...}}}
# curl http://localhost:8080/health   -> {"components":{"db_pings":"ok"},"status":"ok"}
# curl http://localhost:8501/_stcore/health -> ok
```

Open http://localhost:8501. The sidebar shows **Backend online** and the current mode.

## Using local models with Ollama

1. Install Ollama and start it (`ollama serve`, or launch the desktop app).
2. Pull a model, e.g. `ollama pull granite4:3b` (small and fast) or `ollama pull qwen3.5:9b` (better at tool calling).
3. Test through Bifrost with `make ollama-test` (or `make ollama-test OLLAMA_MODEL=qwen3.5:9b`), or with raw curl:
   ```bash
   curl localhost:8080/v1/chat/completions -H 'content-type: application/json' \
     -d '{"model":"ollama/granite4:3b","messages":[{"role":"user","content":"Reply with: pong"}]}'
   ```
   The first call loads the model into memory and can take 5–30 s.

## Testing cloud providers (Groq / OpenRouter)

After putting keys in `.env`, reload Bifrost so it picks them up:

```bash
docker compose up -d --force-recreate --wait bifrost
```

List every model your keys can reach (Bifrost aggregates all providers):

```bash
curl -s localhost:8080/v1/models
```

Easiest (works in every shell):

```bash
make models                                              # list reachable models
make llm-test MODEL=groq/openai/gpt-oss-20b
make llm-test MODEL=openrouter/nvidia/nemotron-3.5-lightning:free
```

Raw curl also works, but **the quoting differs by shell:**

```bash
# Git Bash / macOS / Linux
curl -s localhost:8080/v1/chat/completions -H "content-type: application/json" \
  -d '{"model":"groq/openai/gpt-oss-20b","messages":[{"role":"user","content":"Reply with: pong"}]}'
```

```powershell
# PowerShell
curl.exe -s localhost:8080/v1/chat/completions -H "content-type: application/json" -d '{\"model\":\"groq/openai/gpt-oss-20b\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with: pong\"}]}'
```

Models verified on 2026-11-05 (provider catalogs change; check `/v1/models` if one disappears):

| Model id | Notes |
|---|---|
| `groq/openai/gpt-oss-20b` | Fast, ~500 ms |
| `groq/openai/gpt-oss-120b` | Larger, ~550 ms |
| `groq/qwen/qwen3.8-27b` | Fastest in testing, ~150 ms |
| `openrouter/nvidia/nemotron-3.5-lightning:free` | Free tier, ~2 s |
| `openrouter/google/gemma-4-31b-it:free` | Free tier, often rate-limited (429) upstream |
| `ollama/granite4:3b`, `ollama/qwen3.5:9b` | Local, cost $0 |

Free OpenRouter models are rate-limited. A `429` means "retry later", not a configuration error.

## Dashboard smoke tests

With the stack running (`make up`, preferably in mock mode):

```bash
make ui-test    # renders Home and Agents with Streamlit AppTest: runs an agent, creates/deletes one, backend-down handling
```

## Verifying all real integrations at once

```bash
make live-check
```

This runs inside the backend container and ignores `MOCK_MODE`. It checks Bifrost health, lists models, sends a prompt to Groq, OpenRouter and Ollama, makes one real tool call, and runs a Tavily news search. Expected result: `7/7 checks passed`. See [bifrost.md](bifrost.md#verifying) for sample output.

## Mock mode

With `MOCK_MODE=true` (the default) **no network calls are made** to LLM providers or Tavily:

| Real | Mock replacement | Behaviour |
|---|---|---|
| Bifrost → LLMs | `MockLLMClient` | Each model has a profile (accuracy, latency, verbosity, failure rate, tool use, recovery). It calls the right tool (calculator, current_date, web_search), retries failed tools, and answers from a task answer key with accuracy-weighted correctness. Seeded, so results are reproducible. |
| Tavily | `MockSearchClient` | Dated "news" corpus for demo topics (e.g. the UK Prime Minister changed in July 2026) plus generic results for anything else. Mock URLs use `mock.agentpulse.local`. |
| Stale-answer judge | `mock_freshness` responder | Answers the detector's extraction and judge prompts from the same corpus (current vs stale answer per topic), so the detector runs the same pipeline offline. |

Settings: `MOCK_SEED` (default `42`) changes the simulated outcomes. `MOCK_LATENCY_SCALE` (default `0.25`) controls how long the mock actually waits: `0` is instant and `1` is realistic. Reported latencies are always the realistic, unscaled values.

The dashboard sidebar and `/health` (`"mock_mode": true`) always show which mode is active, and every benchmark run records the mode it ran in.

## Demo data

On a database with **no runs** (first start, or after `make reset`), the backend seeds:

- two mock benchmark runs in the background, Math & Reasoning and Current Events, each with 4 agents ×
  2 repeats and 20% tool faults; they finish in about a minute;
- two example stale-answer checks (Detector → History).

Every page has content on first open. The demo runs are always **mock** (labelled so everywhere), cost
nothing, and are never re-created once any run exists. Turn this off with `SEED_DEMO_DATA=false`.

## Switching between mock and live mode

Either edit `MOCK_MODE` in `.env` and run `make restart`, or override it for one start:

```bash
make up-live   # MOCK_MODE=false for this start, ignoring .env
make up        # back to whatever .env says
```

## Stopping / resetting

```bash
make down      # = docker compose down     (stop, keep data)
make reset     # = docker compose down -v  (stop and wipe the database + Bifrost state)
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `failed to connect to the docker API ... dockerDesktopLinuxEngine` | Docker Desktop isn't running. Start it and wait until it shows "Engine running". |
| Bifrost: `Invalid request payload` | The JSON body was mangled by shell quoting. Use the variant for your shell from "Testing cloud providers". |
| Provider: `model_not_found` / `model is unavailable` | The model id is outdated. Pick a current one from `curl -s localhost:8080/v1/models`. |
| Bifrost: `no keys found that support model` | The provider key for that model is empty in `.env`. Add it and run `docker compose up -d --force-recreate bifrost`. |
| Bifrost: `virtual key is required` | `enforce_auth_on_inference` was turned back on in `bifrost/config.json`. It is `false` for local use. |
| Bifrost → Ollama: `connection refused` / `network is unreachable` | Ollama isn't running on the host. Start it with `ollama serve`. |
| Bifrost → Ollama: `connection to private IP ... is not allowed` | `network_config.allow_private_network` is missing from the `ollama` provider in `bifrost/config.json`. |
| Port already in use (5432/8000/8080/8501) | Stop the conflicting service, or change the left side of the port mapping in `docker-compose.yml`. |
| Dashboard says "Cannot reach the backend" | `docker compose logs backend` |
| `/health` returns `"status":"degraded"` | Check the component with `"ok": false`. `llm` → Bifrost is down (`make ps`). `database` → Postgres is down. `search` → `TAVILY_API_KEY` is missing in live mode. |
| Live runs or freshness checks: `rate_limited [429] … tokens per day (TPD)` | Groq's free tier has a daily token quota per model (about 200k). Wait for the reset, use other models, or set `FRESHNESS_MODEL` to a model your agents don't use. See [stale-answer-detection.md](stale-answer-detection.md#quota-planning-for-a-live-demo). |
| Freshness verdicts are all *unverifiable* | Read a claim's explanation: `search-error` means Tavily (check `TAVILY_API_KEY` and credits), `llm-error` means the judge models are rate-limited. |
| VS Code shows "Unable to import 'fastapi'" etc. | The editor is using a different Python. Run **Python: Select Interpreter** and pick `.venv`. |
