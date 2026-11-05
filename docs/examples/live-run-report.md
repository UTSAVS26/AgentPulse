# AgentPulse benchmark report: run #9

**Suite:** Current Events (time-sensitive) · **Mode:** live (Bifrost + Tavily) · **Status:** done  
**Agents:** 2 · **Repeats:** 1 · **Fault injection:** 0% of tool calls · **Executions:** 12/12  
**Started:** 2026-11-05 09:49:21 UTC · **Duration:** 156 s · **Generated:** 2026-11-05 10:18:15 UTC by AgentPulse v0.1.0

## Highlights

- **🏆 Most reliable:** Qwen3.8 27B · tools (86%)
- **🎯 Most accurate:** Qwen3.8 27B · tools (83%)
- **⚡ Fastest (p50):** GPT-OSS 20B · tools (1.17 s)
- **💰 Cheapest per correct answer:** GPT-OSS 20B · tools ($0.0002)
- **🕰️ Freshest answers:** Qwen3.8 27B · tools (83%)

## Leaderboard

| # | Agent | Model | Reliability | Accuracy | Consistency | Recovery | Errors | p50 | p95 | Tokens/task | Total cost | Cost/correct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Qwen3.8 27B · tools | `groq/qwen/qwen3.8-27b` | 86% | 83% | – | 80% | 0% | 19.07 s | 42.19 s | 2,832 | $0.0151 | $0.0030 |
| 2 | GPT-OSS 20B · tools | `groq/openai/gpt-oss-20b` | 56% | 67% | – | 0% | 0% | 1.17 s | 6.65 s | 1,179 | $0.0007 | $0.0002 |

Reliability = accuracy 40% · consistency 25% · recovery rate 20% · stability 15%. Undefined components (consistency with 1 repeat, recovery when nothing failed) are dropped and the weights renormalised. Formulas: docs/metrics.md.

## Reliability detail

| Agent | pass@k | pass-all-k | Passed | Executions | Faults injected | Recovered | LLM retries | Model calls/task | Tool calls/task |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3.8 27B · tools | 83% | 83% | 5 | 6 | 0 | 4/5 | 26 | 2.67 | 1.67 |
| GPT-OSS 20B · tools | 67% | 67% | 4 | 6 | 0 | 0/1 | 2 | 2.00 | 1.00 |

## Accuracy by category

| Agent | date | politics | sports | technology |
|---|---|---|---|---|
| Qwen3.8 27B · tools | 100% | 100% | 100% | 0% |
| GPT-OSS 20B · tools | 100% | 100% | 0% | 0% |

## Pass rate per task

⏰ = time-sensitive task. Cells: share of repeats passed.

| Task | Qwen3.8 27B · tools | GPT-OSS 20B · tools |
|---|---|---|
| uk-prime-minister ⏰ | 100% | 100% |
| world-cup-winner ⏰ | 100% | 0% |
| latest-python ⏰ | 0% | 0% |
| current-year ⏰ | 100% | 100% |
| day-of-week ⏰ | 100% | 100% |
| un-secretary-general ⏰ | 100% | 100% |

## Freshness (stale-answer detection)

Answers to time-sensitive tasks were checked by the stale-answer detector: claims extracted, searched live on Tavily and judged current, outdated or contradicted. See docs/stale-answer-detection.md.

| Agent | Freshness | Checked | ✅ fresh | ⚠️ stale | ❌ wrong | ❔ unverified |
|---|---|---|---|---|---|---|
| Qwen3.8 27B · tools | 83% | 6 | 5 | 1 | 0 | 0 |
| GPT-OSS 20B · tools | 75% | 6 | 3 | 1 | 0 | 2 |

### Stale or wrong answers

- ⚠️ **GPT-OSS 20B · tools** · `world-cup-winner` #1: “Argentina won the most recent FIFA World Cup.”
  - outdated: Argentina won the most recent FIFA World Cup.. **True today:** Spain won the most recent FIFA World Cup (2026). ([FIFA World Cup winners list by year as Spain claims 2026 title – NBC New York](https://www.nbcnewyork.com/world-cup/fifa-world-cup-winners-list-year-by-year/6526459), 2026-07-20)
- ⚠️ **Qwen3.8 27B · tools** · `latest-python` #1: “The latest stable minor version of Python is **3.14** (released October 7, 2025).”
  - outdated: Python 3.14 is the latest stable minor version of Python. **True today:** Python 3.15 is the latest stable minor version of Python as of 2026‑11‑05 ([Which Python Version Should I Install?](https://www.youtube.com/watch?v=ZQ6F_M4HAhE&vl=en))

## Failed executions (3 of 12)

| Agent | Task | Repeat | Status | Why | Answer |
|---|---|---|---|---|---|
| GPT-OSS 20B · tools | world-cup-winner | 1 | ok | none of ['Spain'] found | Argentina won the most recent FIFA World Cup. |
| GPT-OSS 20B · tools | latest-python | 1 | ok | none of ['3.15'] found | The most recent stable minor release of Python is **Python 3.14.x**. |
| Qwen3.8 27B · tools | latest-python | 1 | ok | none of ['3.15'] found | The latest stable minor version of Python is **3.14** (released October 7, 2025). |

---
Costs use each model's list price from Bifrost's model catalogue (local models are $0). The stale-answer detector's own LLM and search usage is not included in agent costs.
