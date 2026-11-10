# Statistics: is the ranking real?

A leaderboard sorts agents, but with a handful of tasks most gaps are noise. AgentPulse attaches uncertainty
to every score and only separates agents when the data supports it. Code:
[`backend/app/bench/stats.py`](../backend/app/bench/stats.py) (pure functions, seeded). The numbers appear on
Run Benchmark and Compare (the **95% CI** and **Tier** columns, error bars, the "statistically tied" note), in
`GET /runs/{id}/metrics` and in reports ("Is the ranking real?").

## What is computed

| Quantity | Method | Notes |
|---|---|---|
| **Accuracy 95% CI** | Wilson score interval on passes ÷ executions | Better than the naive ±1.96·√(p(1−p)/n) at small n and near 0% or 100%. Repeats of one task aren't independent, so read it as a lower bound on the uncertainty. |
| **Reliability 95% CI** | Paired **task-level bootstrap**: tasks are resampled with replacement and every agent's reliability score is recomputed with the leaderboard's own definitions | 1,000 resamples (fewer for very large runs), seeded by the run id, so a run always shows the same numbers. A task drawn twice counts as two tasks. Zero-width intervals (everything passed or everything failed) are hidden rather than shown as false certainty. |
| **Is A better than B?** | Two-sided bootstrap p-value on the paired differences (the same task draws for every agent) | p = 2 × the smaller tail of (A − B). |
| **Rank tiers** | Walking down the leaderboard, an agent joins the current tier when it is not significantly worse (p ≥ 0.05) than the tier's leader | Agents in one tier are *statistically tied*. The Compare tile says so ("⚖️ statistically tied with …"). |
| **Power note** | The typical interval half-width across agents, floored by the Wilson interval | For example: "With 10 tasks × 3 repeats, reliability scores are uncertain by about ±14 points…". Under 10 tasks or 3 repeats it also suggests more. |

### A real example

A mock run of 10 tasks × 3 repeats ranked four agents 96%, 86%, 82% and 81% reliable. All four landed in
**tier 1**. With so few tasks, a 15-point gap is within noise. Add tasks or repeats to separate them.

## The jury (`scorer: jury`)

One LLM judge has its own biases. In particular, it shouldn't be from the same family as the agents it
grades. The jury asks several **strong judge models from different labs**, in parallel, through Bifrost:

| Juror (default `JURY_MODELS`) | Lab |
|---|---|
| `openrouter/anthropic/claude-opus-5.5` | Anthropic |
| `openrouter/google/gemini-3.1-pro-preview` | Google |
| `openrouter/deepseek/deepseek-v4-pro` | DeepSeek |

None of these labs is behind the default agents (OpenAI gpt-oss, Qwen, NVIDIA Nemotron, IBM Granite). Before
becoming defaults, each juror graded a known-wrong answer (Sydney as the capital of Australia) and a
paraphrased right answer ("written by Austen") correctly. The single `judge` scorer and the stale-answer
detector's judge default to `openrouter/anthropic/claude-sonnet-5.5`, for the same reason. These judges are paid
OpenRouter models (each grading is a few hundred tokens). Their cost is platform overhead, not added to agents.

- **Verdict:** majority vote. A tie goes to the mean score (≥ 0.5 passes). The score is the jurors' mean.
- **Failures:** a juror that errors or replies without JSON is left out, and the detail says so. If every
  juror fails, grading falls back to `contains`, labelled.
- **Agreement:** per run, **Fleiss' κ** over pass/fail votes (Landis & Koch labels: slight … almost
  perfect), the share of unanimous verdicts, and the **most disputed tasks**, which are usually ambiguous
  questions or reference answers worth fixing.
- When every vote is the same, κ is reported as *not measurable*. Agreement beyond chance can't be computed
  without any disagreement, so a perfect score would be misleading.
- Mock mode: simulated jurors judge against the reference with a small, model-specific error rate (2–8%),
  so the statistics are meaningful offline.

Validated live (2026-11-09): a 5-question jury suite on 2 agents went through Bifrost to all three frontier
jurors. All 10 answers passed unanimously, and each juror's vote was recorded.

### OpenRouter credit

The default judges are paid OpenRouter models, so keep a few dollars of credit on the key. OpenRouter reserves
credit for a reply's whole `max_tokens` up front. With a low balance it answers `402 … can only afford N`, and
AgentPulse retries once within that budget (a verdict needs about 200 tokens). At zero balance, nothing breaks:

- the stale-answer detector moves on to `FRESHNESS_FALLBACK_MODELS` (Groq, then a free model);
- a failed juror is left out of the vote, and a failed `judge` grades with `contains`; both are labelled in the
  score detail.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_MODEL` | `openrouter/anthropic/claude-sonnet-5.5` | The `judge` scorer |
| `JURY_MODELS` | the three jurors above | The `jury` scorer; add `custom/<slug>/<model>` connections for more diversity |
