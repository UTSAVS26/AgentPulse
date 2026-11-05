# Changelog

All notable changes per build phase.

## [v2.2] Bring your own model: OpenAI-compatible connections

### Added
- **🔌 Models** page: connect any OpenAI-compatible endpoint with a base URL, model name(s) and an optional API key.
  - Presets for Ollama, LM Studio, vLLM, llama.cpp, Jan, OpenAI, Anthropic, Gemini, Mistral, DeepSeek, xAI, Cerebras, Together, Fireworks, Groq and OpenRouter, plus a custom option.
  - **Fetch models**, **test** (a one-word chat, latency, a tool-calling check), edit, delete, optional prices per 1M tokens.
  - **🤖 New agent** opens the Agents page with the model pre-selected.
- Agents use connection models as `custom/<slug>/<model>`. They are marked 🔌 in the Agents model picker, which warns when you give tools to a model that failed the tool-calling test. They work in try-an-agent, benchmarks, Compare, Traces and Reports.
- API: `GET /connections/presets`, `GET/POST /connections`, `GET/PUT/DELETE /connections/{id}`, `POST /connections/test`, `POST /connections/{id}/test`. `POST/PUT /agents` validate `custom/` ids.
- `ModelRouter`: `custom/…` ids go to the connection's client and everything else to Bifrost. The engine, judge and freshness checker work unchanged. `BifrostClient` is now a subclass of a generic `OpenAICompatibleClient` (Bearer key; keys scrubbed from errors; 401/403 → `auth`).
- **Security:**
  - keys are encrypted at rest (`app/crypto.py`, Fernet, `APP_SECRET`) and never returned (only `key_hint`);
  - URL guard (`app/net_guard.py`): http(s) only, no credentials in the URL;
  - on the public demo, private, loopback, link-local, metadata and `*.internal` addresses are rejected, and creating or testing a connection needs the access code.
- **Docker:** `localhost` in a connection URL is rewritten to `host.docker.internal` (`LOCALHOST_ALIAS`, `extra_hosts`), and the UI explains it. The generated secret persists in the `appdata` volume. Render generates `APP_SECRET`.
- Pricing: connection models use the connection's prices ($0 if none set), labelled `connection`.
- New dependency `cryptography==44.0.0`. New table `model_connections`, created on startup.
- Tests: 35 backend connection tests (crypto, URL guard, API without key leaks, probing against a fake server, routing and cache rebuild, a live benchmark priced from the connection) and 4 page tests. The deploy-image smoke test checks connection security (16 checks).
- Docs: `docs/models.md`, plus updates to the API, agents, setup, dashboard, architecture, deployment and README docs.

### Verified live
- A local **Ollama** entered as `http://localhost:11434/v1`: rewritten to reach the host, 6 models discovered, chat in 8.5 s, tool calling ✓.
- An agent on `custom/my-ollama/granite4:3b` with the calculator answered 395 (correct).
- A live Math & Reasoning benchmark ran it next to `groq/openai/gpt-oss-20b` via Bifrost: both 100%, local p50 7.6 s at $0 vs 1.0 s and $0.0005.
- In a real browser: preset → Fetch models reached the host's Ollama and offered all 6 models.

## [v2.1] Build your own benchmarks: suites & task editor

### Added
- **🧪 Suites** page:
  - create a blank suite, **duplicate** any suite (built-ins too), and **import** YAML or CSV;
  - **edit tasks in a table** (add, edit and delete rows, then save), with per-row validation errors;
  - **export** YAML or CSV, rename or delete, and **test a scorer** against a sample answer;
  - a warning for tasks without an expected answer.
  - Run Benchmark preselects the suite you were editing and links back ("Edit tasks").
- API:
  - `PUT /suites/{id}`, `POST /suites/{id}/duplicate`;
  - task CRUD (`POST/PUT/DELETE /suites/{id}/tasks[/{task_id}]`);
  - `GET /suites/{id}/export?format=yaml|csv`, `POST /suites/import?format=yaml|csv` (raw body, 1 MB limit, per-line errors);
  - `POST /scoring/preview`.
  - Suites now report `builtin` and `runs`, and tasks report `executions`.
- `task_suites.builtin` (added to existing databases on startup) marks the seeded suites. They are **read-only** through the API (`403 "duplicate it to edit"`).
- Protection: a task used by an execution, or a suite with runs, can't be deleted.
- Suites can now be created empty. Names are trimmed and unique regardless of case.
- `frontend/suite_edit.py`: the table → API diff as pure, unit-tested functions.
- `ui.nav_link()`: page links that also work when a page runs on its own (AppTest).
- Tests: 21 backend suite tests, 4 table-diff unit tests (part of `make test`), 5 page tests. The deploy-image smoke test gains a custom-suite check (15 checks).

### Changed
- **Public demo:** custom suites and tasks need no access code (deleting a custom suite used to). Built-ins stay protected.
- Pages are renumbered (Suites is 3; slot 2 is kept for the Models page in v2.2). Page URLs (`/Run_Benchmark`, `/Compare`…) are unchanged.

### Verified
- In a real browser (headless Chromium): typed a new row into the task grid, saved it, and the task was created with default scorer and category. That is the one path AppTest can't drive.
- Postgres migration on existing data: `task_suites.builtin` added, and the seeded suites were flagged on startup.

## [Phase 7] Demo hardening & final docs

### Added
- **Demo data on an empty database**: two mock benchmark runs (Math & Reasoning and Current Events, 4 agents × 2 repeats, 20% faults) and two example stale-answer checks.
  - They are seeded on startup on their own thread and finish in about a minute.
  - They are never re-created once any run exists. `SEED_DEMO_DATA=false` turns them off.
  - On Render, where the disk resets on every boot, visitors therefore never land on empty pages.
- **Home** shows the latest finished run (top agents) with links to Compare, Traces, Detector and Reports.
- **README** rewritten: what it answers, a feature table, how the Bifrost and Tavily challenges are used, live-validated results, the architecture, quickstart, live mode, a docs index and the repo layout. It includes real screenshots (`docs/images/`, taken with headless Chromium against the running stack).
- **`docs/DEMO_SCRIPT.md`**: a 4–5 minute click path with talking points, a pre-flight checklist (including quota planning) and a fallback plan.
- The deploy-image smoke test checks the demo data too (14 checks).

### Changed
- `frontend/app.py` → `frontend/Home.py`, so the sidebar's first entry reads **Home** instead of "app".
- Stat tiles show the value large with a plain caption underneath. `st.metric` deltas always draw a ↑ arrow that read as a trend ("↑ 0 stale of 12 checked"), and agent names were cut off ("GPT-OSS 20…").
- Freshness model chain: the default fallbacks now end on a free **OpenRouter** model, so checks keep working when every Groq model's daily quota is used up. Verified live.

### Fixed (found in the final end-to-end pass)
- The stale-answer judge again called "Argentina won the latest World Cup" *contradicted* ("Argentina was not the latest champion"). The judge now decides `was_true_before` first, with an explicit definition for "current/latest" claims.
- Waits for per-minute rate limits now cover a full one-minute window (2→30 s). Before, they gave up after 14 s while a benchmarked agent shared the same quota.
- Reasoning models (e.g. Nemotron) spent the whole 1,500-token budget thinking and returned empty content, so their verdicts were "unparseable". The budget is now 4,096 tokens, an empty truncated reply moves on to the next model, and the JSON parser skips `<think>` blocks and picks the object that has the expected key.
- **Groq organisation IDs** in provider error messages (which end up in the database, the UI and reports) are now redacted when the error is created.
- Rule-based extraction treated "The answer is I'm not certain…" as a claim: hedges are now detected after the lead-in is stripped. The mock model no longer wraps hedges in "The answer is…".

### Final gate
- Clean state: `make reset && make up` → all services healthy, and both demo runs finished in under a minute.
- `make check`: lint, both Bifrost configs, 234 backend tests and 18 chart tests, also in a clean Linux container.
- `make ui-test`: 39 page tests against the fresh stack. Container logs had 0 tracebacks.
- `make image-smoke`: 14/14 checks.
- Live:
  - `make live-check`: 7/7 (Groq, OpenRouter, Ollama, tool calling, Tavily).
  - A live benchmark with a live report, and live detector verdicts. These were also confirmed with every Groq quota exhausted, through the OpenRouter fallback.

## [Phase 6] Benchmark and reliability reports

### Added
- `GET /reports/{run_id}` in three formats. Every number comes from the same code as `/runs/{id}/metrics`, so reports always match the dashboard.
  - `format=md`: highlights, leaderboard, reliability detail, accuracy by category, pass rate per task, freshness with each stale answer's correction and newest source, and failed executions.
  - `format=csv`: `table=leaderboard` or `table=executions`.
  - `format=json`: everything, including every execution.
  - Downloads are named `agentpulse-run-<id>…`. `download=false` shows the report inline. A report for an in-progress run is marked partial.
- **📄 Reports** page: four download buttons and a rendered preview of the Markdown report.
- `docs/examples/`: a report and leaderboard CSV exported from a live run (Groq via Bifrost + Tavily).
- Refactor: the runs router exposes `metrics_payload()` and `execution_rows()`, which the reports reuse.
- Tests: 7 report tests (formats, equality with the metrics endpoint, freshness section, escaping, partial runs) and 2 page tests.

## [Phase 5] Stale-answer detection with live Tavily verification

### Added
- **Stale-answer detector** (`backend/app/freshness/`):
  - An LLM extracts atomic claims, flags the time-sensitive ones and writes a neutral search query for each.
  - Each time-sensitive claim is searched on Tavily (dated sources).
  - An LLM judge returns `current`, `outdated`, `contradicted` or `unverifiable`, with confidence, an explanation and what is true today.
  - Claims about today's date and year are checked against the clock. Timeless claims are not searched.
  - Freshness score = current ÷ decisive claims. There is also an overall verdict per text: fresh, stale, wrong, unverified, timeless or no claims.
- **API**: `POST /freshness/check` (a text with an optional question, or an `execution_id`), `GET /freshness/checks[/{id}]`, `DELETE /freshness/checks/{id}`. `GET /executions/{id}` gains `freshness_verdict` and `freshness_check_id`.
- **Benchmarks**: answers to ⏰ time-sensitive tasks are checked automatically (`check_freshness`, on by default).
  - One cached checker per run, so identical answers and claims are verified once.
  - Per-agent metrics: `freshness`, `freshness_checked`, `stale_answers`, `stale_rate`, `freshness_verdicts`.
  - Reported next to the reliability score, not inside it.
- **🕰️ Stale-Answer Detector** page:
  - Paste text (with examples), check an agent's answer, or browse history.
  - Results: verdict banner, stat tiles, and per-claim cards with "true today", the explanation, the search query, and dated sources (newest first), plus a data table.
  - Shareable `?check=` and `?execution=` links.
- **Compare → 🕰️ Freshness** tab: per-agent freshness tiles, a 100%-stacked verdict chart (status colors with icon labels), and a table of every stale or wrong answer with *why ↗* and trace links.
  - The leaderboard gains Freshness and Stale columns.
  - Run Benchmark gains a staleness-check toggle and a Freshness column.
  - Traces links to the check.
- **Mock mode runs the same pipeline**: the mock LLM answers the extraction and judge prompts from a mock corpus that lists the current and stale answers per topic (UK PM, World Cup, latest Python, UN Secretary-General…).
- **Robustness**: `check()` never raises.
  - If extraction fails, rule-based extraction takes over.
  - Tavily errors, missing sources and judge errors give `unverifiable` with the reason, and the sources are kept.
  - **Model fallback** (`FRESHNESS_FALLBACK_MODELS`) with rate-limit cooldowns: a daily quota benches a model for an hour and per-minute limits are waited out.
- Settings `FRESHNESS_MODEL`, `FRESHNESS_FALLBACK_MODELS` and `FRESHNESS_SEARCH_DEPTH` (default advanced) are forwarded by compose. `JUDGE_MODEL` is now forwarded too (it was documented but never reached the container).
- Schema: new nullable columns, added to existing databases on startup. Verified on the local Postgres volume: 12 columns added, no data lost.
- Tests: 49 backend freshness tests, 1 chart test and 6 page tests. The deploy-image smoke test gains a detector check (13 checks).
- Docs: `docs/stale-answer-detection.md`, plus updates to the API, metrics, dashboard, architecture and setup docs.

### Found in live testing (and fixed)
- **Judge called a stale answer "contradicted"** ("Argentina won the latest World Cup"). The prompt now spells out that former holders are *outdated*, and the judge returns `was_true_before`. A contradicted claim that was true before becomes outdated.
- **Groq free-tier quotas**: the benchmarked agents and the checker share per-model quotas (200k tokens per day, and per-minute input limits). Verdicts came back `unverifiable` until model fallback, cooldowns and per-minute waits were added. Quota planning for demos is in the docs.
- **"What day is it?" answers** were rewritten by the extractor as timeless facts ("Nov 5, 2026 is a Thursday"). Date questions now go straight to the clock. Historical "which year…" questions are excluded.
- **Basic search depth returned undated pages** (and a wrong "Python 3.10.22" summary), so automatic checks now default to advanced.

### Verified live (Groq via Bifrost + Tavily, 2026-11-05)
- Rishi Sunak as PM → outdated (NPR, Georgetown, The Hill, July–Nov 2026: Andy Burnham).
- Argentina → outdated (Spain, July 2026). Python 3.13 → outdated (Python Insider, Oct 2026: 3.15). Elon Musk as PM → contradicted.
- "Water boils at 100 °C" → timeless, not searched. "It is 2024" → outdated (clock).
- Live benchmark on Current Events (2 agents): every answer got a verdict and the stale answers were flagged. Both agents answered Python 3.14 (out of date: 3.15 shipped in October 2026).

## [Phase 4] Comparison dashboard & trace viewer

### Added
- **Compare** page: headline tiles (most reliable, most accurate, fastest, cheapest per correct answer), leaderboard, and tabs for overview (reliability bars, radar of up to 3 agents), speed & cost (p50/p95 latency, accuracy-vs-cost scatter, tokens, total cost, data table), per task (pass-rate heatmap with ⏰ time-sensitive markers, accuracy by category), and failures & recovery (per-agent recovery and every failed execution with its reason and a trace link).
- **Traces** page: open an execution by link (`?execution=<id>`), by run → agent → task → repeat, from ad-hoc runs, or by ID. Shows prompt, answer, scoring explanation, cost, metrics, a step **timeline** chart and the full step list.
- `frontend/charts.py`: Plotly builders following a validated data-viz palette (color by agent in a fixed order, radar capped at 3 agents, direct labels, one axis per chart, reserved status colors). Dark theme pinned in `frontend/.streamlit/config.toml`.
- Trace links in the Run Benchmark executions table.
- Tests: 17 chart unit tests (part of `make test`) and 5 page tests (30 frontend tests in total).

### Visual review
- Every chart was rendered from a real run and inspected. That surfaced and fixed: radar tick labels colliding with axis labels, overlapping and clipped scatter labels, an unreadable `$50µ` cost axis (now cost per 1,000 tasks), low-contrast heatmap text (per-cell ink), and an undefined recovery value drawn as a misleading 0% (hover now says "n/a"). Each fix has a regression test.
- Docs: `docs/dashboard.md`. Deployment troubleshooting explains the harmless Bifrost log lines on Render.

### Fixed
- Radar cost efficiency: when a free model was in the run, every paid agent showed 100%. Paid agents are now scored relative to the cheapest *paid* agent.

## [Deploy] Switch hosting to Render

Hugging Face now requires a paid plan for Docker Spaces, so the deployment moved to a **free Render Docker web service**.

### Changed
- `render.yaml` Blueprint: one free Docker web service. It deploys `main` **only after GitHub CI checks pass** (`autoDeployTrigger: checksPass`), and secrets are prompted at setup (`sync: false`), never committed.
- The single-container image moved to `deploy/Dockerfile` + `deploy/start.sh`, built straight from the repo root (root `.dockerignore` keeps `.env`, `.git`, `.venv` and tests out). The dashboard listens on the platform's `$PORT`.
- `bifrost/config.space.json` → `bifrost/config.cloud.json`. Make targets `space-*` → `image-build`, `image-smoke`, `image-live`, `image-stop`. CI job → **Deploy image (build + smoke test)**.

### Removed
- Hugging Face deploy workflow and scripts (`deploy.yml`, `build_space.py`, `deploy_space.py`), the Space card, and the `huggingface_hub` dev dependency. The `HF_*` GitHub secrets are no longer used.

### Fixed
- **Render deploy crash-looped** ("No open ports detected", then `Bifrost fatal: database is locked`). On 0.1 CPU, Bifrost's SQLite stores hit lock timeouts, and `start.sh` started the dashboard last and stopped the whole container when Bifrost died. Now the dashboard binds `$PORT` first, Bifrost runs in a non-fatal restart loop (skipped without provider keys), and its SQLite stores are disabled in the cloud config. Reproduced and verified locally with `--cpus=0.1 --memory=512m`: no lock errors, port open in ~70 s, live mode and all 12 smoke checks pass. Regression tests cover the startup order and the store settings.

### Verified
- The image runs on Render's default port (`PORT=10000`) at about 290 MB RAM (512 MB free-plan limit) and passes all 12 smoke checks.

## [Deploy] Hugging Face Space auto-deploy + public demo mode

### Added
- **Per-request mock/live mode** (`app/mode.py`): with `MOCK_MODE=true` + `LIVE_ACCESS_CODE`, visitors get mock mode and the `X-Access-Code` header unlocks live mode. Wrong code → 401. Runs record their mode and the engine uses the matching clients per run.
- **Admin guard** on public deployments: editing or deleting agents, deleting suites or runs, and cancelling runs need the code (403 otherwise).
- Dashboard: sidebar **"Unlock live mode"** (verifies the code, recovers gracefully from a stale code, lock button).
- **Single-container Space image** (`deploy/space/`): static Bifrost binary + FastAPI + Streamlit (port 7860), uid 1000, SQLite in WAL mode or Postgres via `DATABASE_URL`, and a cloud Bifrost config without Ollama.
- `scripts/build_space.py`, `scripts/deploy_space.py` (create Space, sync secrets, upload, wait for RUNNING, check URL), `scripts/smoke_space.py`, `scripts/wait_healthy.py`.
- **Deploy workflow** (`.github/workflows/deploy.yml`): runs after CI succeeds on `main`, or manually.
- CI job **"Hugging Face Space image"** (`make space-smoke`). `make validate-config` now validates both Bifrost configs.
- Postgres URLs from hosted providers (`postgres://`, `postgresql://`) are normalised to the psycopg driver.
- Tests: 26 new backend tests (171 total) for access-code rules, per-run mode, the deploy bundle and DB URLs, plus a UI test for the unlock flow.
- Docs: `docs/deployment.md`, plus updates to the README, setup, architecture and CI docs.

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
