# Metrics

Computed per agent over all of its executions in a run, by [`backend/app/bench/metrics.py`](../backend/app/bench/metrics.py)
(pure functions, unit-tested against hand-calculated values). Available live while a run is in progress
via `GET /runs/{id}/metrics`.

## Definitions

| Metric | Formula | Notes |
|---|---|---|
| **Accuracy** | passed ÷ executions | Errors and `max_steps` count as failures |
| Mean score | mean of per-execution scores | Fractional only for `judge` tasks |
| **Consistency** | tasks whose pass/fail outcome is the same in every repeat ÷ tasks | Only defined with ≥ 2 repeats |
| pass@k | tasks passed in **at least one** repeat ÷ tasks | Optimistic: "can it ever do this?" |
| pass-all-k | tasks passed in **every** repeat ÷ tasks | Strict: "can I rely on it?" |
| **Error rate** | executions with status `error` or `max_steps` ÷ executions | Infrastructure or agent-loop failures |
| **Recovery rate** | recovered ÷ executions that experienced a failure | A failure is an injected fault, tool error, rejected tool call or LLM retry. Recovered = failure **and** passed. Undefined if nothing failed. |
| Latency p50 / p95 / mean | Linear-interpolated percentiles of per-execution latency | Per-execution latency = model latency + tool latency + simulated fault latency + retry backoff |
| Tokens (mean / total) | prompt + completion tokens, as reported by the provider via Bifrost | |
| **Cost** (total / mean) | Σ tokens × list price | Prices from Bifrost's catalogue, see [benchmarks.md](benchmarks.md#cost) |
| Cost per correct answer | total cost ÷ passed | Undefined if nothing passed |
| Steps / tool calls (mean) | model calls / tool calls per execution | Efficiency |
| Accuracy by category | accuracy within each task `category` | |

## Reliability score

A single 0–1 number for ranking, shown on the leaderboard:

```
reliability = 0.40 · accuracy
            + 0.25 · consistency
            + 0.20 · recovery_rate
            + 0.15 · (1 − error_rate)        # "stability"
```

If a component is **undefined** for a run (consistency with 1 repeat, recovery when nothing failed), it is
dropped and the remaining weights are **renormalised** to sum to 1. The weights actually used are returned
per agent (`reliability_weights`) and shown in the UI.

Worked example (from the unit tests): accuracy 4/6, consistency 1/2, recovery 2/4, error rate 1/6:

```
0.40·0.667 + 0.25·0.5 + 0.20·0.5 + 0.15·0.833 = 0.617
```

With 1 repeat and no failures, only accuracy and stability remain:
`(0.40·acc + 0.15·stability) / 0.55`.

Leaderboard order: reliability score, then accuracy (descending). Agents with no executions yet appear last.

## Freshness

Answers to ⏰ time-sensitive tasks are checked by the [stale-answer detector](stale-answer-detection.md)
(unless the run sets `check_freshness: false`). Per agent:

| Metric | Formula | Notes |
|---|---|---|
| `freshness_checked` | answers the detector checked | Only `ok` answers to time-sensitive tasks |
| **Freshness** | mean of per-answer freshness scores | Per answer: current ÷ (current + outdated + contradicted) over its time-sensitive claims. Answers with no decisive claim are left out. |
| `stale_answers` | answers whose verdict is `stale` (≥ 1 outdated claim) | |
| Stale rate | stale answers ÷ answers checked | |
| `freshness_verdicts` | count per answer verdict: `fresh`, `stale`, `wrong`, `unverified`, `timeless`, `no_claims` | Powers the Freshness chart on Compare |

Freshness is reported **next to** the reliability score, not inside it, so the reliability weights stay
comparable across suites with and without time-sensitive tasks.

## Task matrix

`task_matrix` gives, for every task, each agent's pass rate across repeats (`null` if the agent hasn't
attempted it yet). It powers the per-task comparison on the Compare page.
