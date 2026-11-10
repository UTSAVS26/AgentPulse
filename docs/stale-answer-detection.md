# Stale-answer detection

Models answer from training data, so they confidently give answers that **were** true: a former prime
minister, last tournament's winner, an old "latest" version. The stale-answer detector finds those claims and
checks them against **live Tavily search results and their publication dates**.

Code: [`backend/app/freshness/`](../backend/app/freshness/). API: [`POST /freshness/check`](api.md#freshness).
UI: **🕰️ Stale-Answer Detector** page, and the **🕰️ Freshness** tab on Compare. The same detector powers the
[Freshness Guard](guard.md), which checks answers on their way from a model to an app.

## Pipeline

```
text (+ optional question it answers)
  │
  ├─ question is "what year/day is it?" ──────────────► clock check (no LLM, no search)
  │
  ▼
1. extract claims (LLM, JSON)          ── LLM fails / unparseable ──► rule-based extraction
   each: claim · time_sensitive · neutral search query · topic (news|general)
   + heuristic override: "current", "latest", "now", "reigning"… ⇒ always time-sensitive
  │
  ├─ timeless claim ───────────────────────────────────► not_time_sensitive (not searched)
  │
  ▼ (time-sensitive claims, in parallel, max 8)
2. claim about today's date/year/weekday? ─────────────► clock check
   otherwise: Tavily search (advanced, include_answer) ─ fails ─► unverifiable (reason shown)
  │
  ▼
3. LLM judge: claim + today's date + dated sources ──── fails ──► unverifiable (sources kept)
   → current | outdated | contradicted | unverifiable  (+ confidence, explanation, what's true today)
  │
  ▼
4. score + overall verdict
```

| Step | Module |
|---|---|
| Prompts (JSON in, JSON out) | [`prompts.py`](../backend/app/freshness/prompts.py) |
| Pipeline, clock check, scoring, model fallback, caching | [`checker.py`](../backend/app/freshness/checker.py) |
| Rule-based extraction and time-sensitivity signals | [`heuristics.py`](../backend/app/freshness/heuristics.py) |
| Persistence and API shapes | [`store.py`](../backend/app/freshness/store.py) |
| Mock fact-checker (mock mode) | [`clients/mock_freshness.py`](../backend/app/clients/mock_freshness.py) |

### Short answers need their question

"Keir Starmer." means nothing on its own. When a question is given (always the case for benchmark answers), the
extractor writes self-contained claims ("Keir Starmer is the current Prime Minister of the UK") and searches a
*neutral* query that does not contain the claimed answer ("current UK prime minister"). That avoids finding
old articles that merely repeat the claim.

### Time-sensitivity

The LLM flags each claim, and a rule-based check can only *add* time-sensitivity, never remove it:

- **Strong signals** make a claim time-sensitive on their own: *current, latest, newest, most recent, now,
  today, incumbent, reigning, as of, this year…*
- **Weak signals** count only together with a present-tense verb (*is, are, holds, has…*): an office or title
  (*prime minister, CEO, champion*), a superlative or record (*tallest, richest*), a price, a version number,
  a count (*population, users*). "The tallest building is the Burj Khalifa" is time-sensitive. "The Berlin Wall
  fell in 1989" is settled history, and "Water boils at 100 °C" has no signal at all.

Timeless claims are reported but **not searched**, which saves search credits.

## Verdicts

| Claim verdict | Meaning |
|---|---|
| ✅ `current` | The sources show the claim is true today. |
| ⚠️ `outdated` | It **was** true but no longer is (a former office holder, the previous champion, a superseded version). This is a stale answer. |
| ❌ `contradicted` | The sources show it is false and never was true. |
| ❔ `unverifiable` | The evidence is missing or inconclusive, or the search or judge failed (the reason is in `explanation`). |
| ➖ `not_time_sensitive` | A timeless claim, not checked. |

The judge also returns `was_true_before`. A *contradicted* verdict with `was_true_before: true` is turned into
*outdated*, because smaller judge models tend to call "Argentina won the latest World Cup" contradicted
rather than outdated.

**Freshness score** = `current ÷ (current + outdated + contradicted)` over time-sensitive claims. It is `null`
when nothing decisive was found.

**Overall verdict** for a whole text, in priority order:

| Verdict | When |
|---|---|
| ⚠️ `stale` | any claim is outdated |
| ❌ `wrong` | otherwise, any claim is contradicted |
| ✅ `fresh` | otherwise, at least one claim is current |
| ❔ `unverified` | time-sensitive claims, but nothing decisive |
| ➖ `timeless` | only timeless claims |
| ∅ `no_claims` | nothing factual (e.g. "I'm not sure") |

### Clock checks

Claims about *now*, such as "It is 2024", "Today is Monday" or an answer to "What year is it?", are checked
against the server clock (UTC), not searched. An earlier year is `outdated`, a later one or a wrong weekday is
`contradicted`. Historical questions such as "Which year did WW2 end?" are not treated as date questions.

## Robustness

`check()` never raises. Every failure becomes `unverifiable`, with the reason shown:

| Failure | Result |
|---|---|
| Extraction LLM error or unparseable reply | Rule-based extraction; `extraction` says `heuristic (LLM …)` |
| Tavily error (retried once if transient) | `unverifiable`, method `search-error` |
| No search results | `unverifiable`, method `no-sources` |
| A reasoning model spends its whole token budget and returns nothing | Next model in the chain |
| Judge error, unparseable reply or unknown verdict | `unverifiable`, method `llm-error`; the sources and Tavily's summary are kept |

**Model fallback and rate limits.** The checker uses `FRESHNESS_MODEL`, then `FRESHNESS_FALLBACK_MODELS` in
order. The default chain ends on a free OpenRouter model, so a provider whose quota is exhausted for the day doesn't stop checks:

- A 429 benches that model: for an hour if it is a **daily** quota ("tokens per day"), or for 20 s if it is a
  per-minute limit. The next model is tried immediately.
- If every model is rate-limited, the checker waits out the per-minute limits (2, 4, 8, 16 and 30 s, which covers a full one-minute window) and tries
  again, skipping daily-exhausted models.
- Benched models are tried last on later requests.

## In benchmark runs

When a run includes ⏰ time-sensitive tasks (and **Check answers for staleness** is on, the default):

- Every `ok` answer to a time-sensitive task is checked with the task prompt as its question. The result is
  stored as a `freshness_checks` row (`source = benchmark`), and on the execution as `freshness_score` and
  `freshness_verdict`.
- Each run uses **one checker with a cache**, so identical answers and claims are verified once. For example,
  "Keir Starmer" from 9 executions costs one search.
- The detector's own LLM and search usage is platform overhead. It is **not** added to the agent's tokens or
  cost.
- Per agent, the metrics add `freshness` (mean score), `freshness_checked`, `stale_answers`, `stale_rate` and
  `freshness_verdicts` (counts). See [metrics.md](metrics.md#freshness). They are shown next to the reliability
  score, not inside it.
- A failed check never fails the execution.

## Mock mode

The mock LLM answers the extraction and judge prompts, so mock mode runs **the same pipeline**, including JSON
parsing, offline:

- Extraction uses the rule-based extractor.
- Verdicts come from the mock search corpus in
  [`mock_search.py`](../backend/app/clients/mock_search.py). Each topic lists its current answer and the stale
  ones: UK Prime Minister, World Cup, latest Python, UN Secretary-General, tallest building, capital of France,
  boiling point.
- A claim naming the current answer is `current`, an older answer is `outdated`, any other answer on a known
  topic is `contradicted`, and unknown topics are `unverifiable`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FRESHNESS_MODEL` | `openrouter/anthropic/claude-sonnet-5.5` | Extraction and judge model (live), deliberately not one of the benchmarked agents' models |
| `FRESHNESS_FALLBACK_MODELS` | `groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b,openrouter/nvidia/nemotron-3.5-lightning:free` | Tried in order when rate-limited or failing |
| `FRESHNESS_SEARCH_DEPTH` | `advanced` | Tavily depth for automatic checks in runs (advanced = 2 credits, much better dated sources; basic = 1) |
| `TAVILY_API_KEY` | | Required for live verification |

On-demand checks (API and UI) default to `advanced`; the request can ask for `basic`.

### Quota planning for a live demo

Groq's free tier allows about **200k tokens per day per model**. A full live run of 6 agents × 6 tasks × 3
repeats can exhaust a model for the day. The checker's default model (Claude Sonnet 5.5 via OpenRouter) is
deliberately one **no agent uses**, so the checks don't eat the agents' quotas, and it is a stronger judge
(paid, a few hundred tokens per claim; the Groq and free fallbacks take over if it fails). Before a demo:

- if you change `FRESHNESS_MODEL`, keep it on a model no agent in the run uses;
- keep runs small (1–2 repeats);
- remember that each distinct claim costs about 2 Tavily credits (1,000 free per month).

If a quota runs out anyway, verdicts degrade to `unverifiable` with the reason, and the run continues.

## Validated live (2026-11-05)

| Input | Verdict | Evidence |
|---|---|---|
| "The current UK PM is Rishi Sunak." | ⚠️ outdated (0.98) | NPR / Georgetown / The Hill, July–Nov 2026: Andy Burnham |
| "Argentina won the latest FIFA World Cup." | ⚠️ outdated (0.99) | NBC / CNBC / Olympics.com, 19–20 July 2026: Spain |
| "The newest stable Python release is 3.13." | ⚠️ outdated (0.98) | Python Insider, 9 Oct 2026: Python 3.15.0 |
| "Elon Musk is the current UK Prime Minister." | ❌ contradicted (0.99) | Same UK sources |
| "Water boils at 100 °C at sea level." | ➖ timeless | not searched |
| "It is 2024." | ⚠️ outdated | clock |

Later the same day, with **every Groq model's daily quota used up**, the fallback chain reached the free
OpenRouter model, which returned the same verdicts: Argentina → outdated, Elon Musk → contradicted, water → timeless.
