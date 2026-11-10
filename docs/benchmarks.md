# Benchmarks

A **benchmark run** = one task suite × N agents × R repeats, optionally with fault injection.
Every agent sees **identical tasks** through the **identical gateway path** (Bifrost), so the
differences you see come from the agent: its model, prompt, tools and step budget.

## Built-in suites

Seeded from [`backend/app/seed/suites/`](../backend/app/seed/suites/) on startup.

| Suite | Tasks | What it tests |
|---|---|---|
| **Factual QA** | 8 | Stable knowledge (geography, science, history), plus one free-text question graded by an LLM judge |
| **Math & Reasoning** | 8 | Exact arithmetic and word problems. A calculator should help, and faults on it test recovery. |
| **Current Events (time-sensitive)** | 6 | Facts that change: UK PM, latest World Cup winner, latest Python, current year/day, UN Secretary-General. Models answering from training data give **stale answers**. |

The time-sensitive expected answers were verified with Tavily on **2026-11-05** (e.g. Andy Burnham became
UK PM in July 2026, and Spain won the 2026 World Cup). `stale_answer` records what an out-of-date model says.
The mock LLM uses it, and Phase 5's stale-answer detector builds on the same idea. When a fact changes,
edit the YAML and restart: tasks are matched by `key` and updated in place.

## Task format

```yaml
name: My Suite
description: What this suite measures.
tasks:
  - key: compound-interest          # unique within the suite: [a-z0-9_-]
    prompt: "$1,000 at 5% compounded yearly for 10 years?"
    expected: "1628.89"
    scorer: numeric                 # contains | exact | numeric | regex | judge | jury
    tolerance: 0.01                 # numeric only (absolute)
    category: word-problem          # free text, used for per-category accuracy
    time_sensitive: false
    stale_answer: null              # optional: the typical outdated answer
```

## Your own suites

Built-in suites are **read-only** (seeded from YAML and re-synced on every start). Build your own in the
dashboard's **🧪 Suites** page, or through the API ([api.md](api.md#suites)):

| Action | How |
|---|---|
| Start from scratch | **Create a suite** → *Blank suite*, then add rows in the task table |
| Start from a built-in | **📄 Duplicate**: an editable copy named "… (copy)" |
| Edit tasks | Edit cells in the table, add rows at the bottom, delete rows with the row selector, then **💾 Save changes**. Every row is validated and errors are shown per row; rows without errors are saved. |
| Check a scorer | **🧪 Test a scorer**: score a sample answer against a task's scorer and expected answer (no model is called; `judge` previews with its `contains` fallback) |
| Import | **Create a suite** → *Import a file*: YAML (the format above) or CSV |
| Export | **⬇️ Export YAML / CSV**: also the easiest template for a new file |

**Protecting results**: a task that a run has already used can't be deleted (so old runs stay readable), and
a suite with runs can't be deleted (delete the runs first). Editing a used task is allowed; the change applies
to future runs. Custom suites are open to everyone, including on the public demo; built-ins can't be changed
there or anywhere.

### CSV format

One row per task. Header names match the YAML fields, and only `key` and `prompt` are required:

```csv
key,prompt,expected,scorer,tolerance,category,time_sensitive,stale_answer
capital-peru,What is the capital of Peru?,Lima,contains,0,geography,false,
compound,"$1,000 at 5% for 10 years, compounded yearly?",1628.89,numeric,0.01,finance,false,
```

Blank cells use the defaults (`contains`, `0`, `general`, `false`). `time_sensitive` accepts
`true/false/yes/no/1/0`. Unknown columns are ignored with a warning. Errors are reported by line.

You can also add a YAML file to `seed/suites/` and restart; it then becomes a built-in suite.

## Scorers

| Scorer | Passes when | Notes |
|---|---|---|
| `contains` | `expected` appears in the answer | Case, markdown and punctuation insensitive. Word boundaries, so `Au` doesn't match "beautiful". Use `A\|B` for alternatives. |
| `exact` | Normalised answer equals `expected` | Strips "The answer is" filler |
| `numeric` | A number in the **final sentence containing a number** is within `tolerance` of `expected` | Handles `1,234`, `1 234`, `$1,628.89`, unicode minus and narrow spaces. Using the final sentence avoids false passes on intermediate numbers. |
| `regex` | `re.search(expected, answer, IGNORECASE)` | |
| `judge` | An LLM judge (`JUDGE_MODEL`, default `openrouter/anthropic/claude-sonnet-5.5`, from a lab the agents don't use) gives a score ≥ 0.5 | Score 0–1 is kept as-is. Falls back to `contains` if the judge errors or replies without JSON, or in mock mode. The fallback is always noted in `score_detail`. |
| `jury` | Several judge models from different labs (`JURY_MODELS`: Claude Opus 5.5, Gemini 3.1 Pro, DeepSeek V4 Pro) vote; the majority decides | Score = the jurors' mean. Failed jurors are left out. Agreement (Fleiss' κ) and disputed tasks are reported per run. Works in mock mode too. See [statistics.md](statistics.md#the-jury-scorer-jury). |

Placeholders in `expected`: `{{current_year}}`, `{{today_iso}}`, `{{today_weekday}}`, resolved at scoring time.
An empty answer always fails. Each execution stores the scorer's explanation in `score_detail`
(e.g. `expected 395 (±1e-06), found 396`).

## How a run executes

```
POST /runs ──▶ validate (suite has tasks, agents exist, ≤ 2000 executions) ──▶ 202 + run id
                    │
     background thread: agents in parallel ─┬─ agent A: repeat 1 [t1 t2 … tn] → repeat 2 [...] …
                                            ├─ agent B: …
                                            └─ agent C: …
     each execution: run agent → score → price → commit (+ progress_done += 1)
```

- **Agents run in parallel, and each agent's tasks run sequentially.** This respects per-model rate limits and gives every agent partial results early, so the leaderboard fills in live.
- **Each execution is committed as soon as it finishes.** Cancelling (`POST /runs/{id}/cancel`) stops after the in-flight tasks and keeps finished work.
- **Faults are seeded** from `(run, agent, task, repeat)`, so the same run configuration reproduces the same faults.
- **An internal error in one execution** is recorded on that execution (`status=error`) and the run continues.
- **Runs left `running` by a backend restart** are marked `failed` ("interrupted") on startup.

### Reliability behaviours measured during runs

| Situation | What the runtime does | Counted as |
|---|---|---|
| Injected tool fault (timeout / 500 / empty / malformed) | Shows the error to the model, which may retry | `faults_injected`, failure |
| Real tool error (e.g. division by zero) | Same | `tool_errors`, failure |
| Provider rejects a malformed tool call (HTTP 400 "tool call validation failed") | Tells the model what was wrong and retries (up to 2×) | `retries` + `tool_errors`, failure |
| Rate limit (429) | Exponential backoff 2 s → 4 s → … up to 30 s, 5 retries | `retries`, failure |
| Timeout / 5xx | Backoff 0.5 s → 1 s, 2 retries | `retries`, failure |

"Failure" here means the execution counts towards the recovery-rate denominator. **Recovered** = a failure happened and the task was still answered correctly.

## Cost

Token usage × the model's list price, taken **from Bifrost's model catalogue** (`GET /v1/models` →
`pricing.prompt` / `pricing.completion`, cached for 1 h). Local Ollama models cost $0, and `:free`
OpenRouter models cost $0. In mock mode, or if the catalogue is unavailable, a dated snapshot is used.
`score_detail` ends with `price: bifrost|snapshot|local|free|unknown`.

## Observed live results (2026-11-05)

These come from real runs through Bifrost and Tavily, and show what the benchmark surfaces:

- **Current Events, 1 repeat, no faults:** GPT-OSS 120B + tools ranked first (83% accuracy). The no-tools baseline couldn't name the UK PM. Both tools agents still said **Argentina** won the latest World Cup without searching: a stale answer even with tools available.
- **Math, 2 repeats, 30% faults:** GPT-OSS 20B recovered from every injected fault (100%). Qwen3.8 27B hit Groq's 7k input-tokens-per-minute limit and produced malformed tool calls. After the corrective-retry fix it reached 100% with 34 retries, all recovered.
- **GPT-OSS models on Groq know today's date without tools.** The provider includes it in the model's system prompt, so the "current year / day" tasks don't separate tools from no-tools on that model.
