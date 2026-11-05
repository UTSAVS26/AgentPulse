# Dashboard

Streamlit app in [`frontend/`](../frontend/). Pages appear in the sidebar in this order:

| Page | What it's for |
|---|---|
| **Home** | Overview, plus system status and mock/live mode in the sidebar |
| **🤖 Agents** | List, create, edit and delete agents; **try** an agent on one prompt with fault injection and see its trace |
| **🏁 Run Benchmark** | Configure suite × agents × repeats × fault rate, watch live progress, partial leaderboard, all executions |
| **📊 Compare** | Multi-agent comparison for one run: headline tiles, leaderboard, charts |
| **🔍 Traces** | Step-by-step view of a single execution |

## Compare page

| Section | Shows |
|---|---|
| Headline tiles | 🏆 most reliable · 🎯 most accurate · ⚡ fastest p50 · 💰 cheapest per *correct* answer |
| Leaderboard | Reliability, accuracy, consistency, recovery, error rate, p50/p95, tokens, cost |
| **Overview** tab | Reliability score bars, and a **radar** of up to 3 agents (accuracy, consistency, recovery, stability, speed, cost efficiency) |
| **Speed & cost** tab | p50 vs p95 latency, accuracy-vs-cost scatter (top-left is best), tokens per task, total cost, and a data table |
| **Per task** tab | Task × agent **pass-rate heatmap** (⏰ marks time-sensitive tasks) and accuracy by category |
| **Failures & recovery** tab | Recovered / failed counts per agent, plus every failed execution with *why* it failed and a link to its trace |

Radar normalisation (all 0–1, higher is better):

- **Speed** = fastest agent's p50 ÷ this agent's p50.
- **Cost efficiency**: free or local agents score 1. Paid agents score the cheapest *paid* agent's cost per task ÷ this agent's, so paid agents stay comparable when a free model is in the run.
- **Stability** = 1 − error rate.
- **Accuracy, consistency, recovery**: as measured (undefined counts as 0).

## Traces page

Open an execution in any of these ways:

- the **open ↗** links in Run Benchmark → *All executions*, or in Compare → *Failures*;
- browse **run → agent → task → repeat** (✅/❌ markers, with an optional "failed only" filter);
- **ad-hoc** runs from *Agents → Try an agent*;
- **by ID**, or the shareable URL `…/Traces?execution=<id>`.

It shows the prompt, the answer, the scoring explanation, cost, the metrics row, a **timeline** (each step as a bar,
in order) and the full step list with raw payloads.

## Chart design rules

Charts are built in [`frontend/charts.py`](../frontend/charts.py) (pure functions, unit-tested in
`frontend/tests/test_charts.py`) and follow a data-visualisation method with a **validated** palette:

- **Color follows the agent, not its rank.** Slots are assigned in the run's agent order, from 8 categorical colors
  validated for color-vision deficiency and contrast against the dark background (`#0e1117`). A 9th agent or
  beyond gets neutral gray, never a generated color.
- **Identity is never color-alone.** Agent names are on the axis or labelled next to the point.
- **Forms where every series overlaps every other** are capped at the palette's safe limit. The radar shows at
  most 3 agents (the 4th color pair fails the overlap checks). The scatter uses one color with name labels.
- **One y-axis per chart.** Latency, tokens and cost are separate charts.
- **Magnitude uses a single hue** (the blue sequential scale in the heatmap), with every cell labelled.
- **Status colors are reserved for outcomes in traces** (✅ final, ❌ error, 💥 fault, ⚠️ tool error, 🔁 retry) and
  always come with an icon and a label.
- Every chart has hover tooltips, and key charts have a data-table view.

The theme is fixed to dark (`frontend/.streamlit/config.toml`) because the palette was validated against that surface.
