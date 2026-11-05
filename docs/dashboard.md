# Dashboard

Streamlit app in [`frontend/`](../frontend/). Pages appear in the sidebar in this order:

| Page | What it's for |
|---|---|
| **Home** | Overview, a snapshot of the latest finished run (top agents, links to Compare, Traces, Detector and Reports), and system status plus mock/live mode in the sidebar |
| **🤖 Agents** | List, create, edit and delete agents; **try** an agent on one prompt with fault injection and see its trace |
| **🏁 Run Benchmark** | Configure suite × agents × repeats × fault rate, watch live progress, partial leaderboard, all executions |
| **📊 Compare** | Multi-agent comparison for one run: headline tiles, leaderboard, charts |
| **🔍 Traces** | Step-by-step view of a single execution |
| **🕰️ Stale-Answer Detector** | Check any text, or an agent's answer, for claims that are no longer true (live Tavily sources with dates) |
| **📄 Reports** | Download a run as Markdown, leaderboard CSV, executions CSV or JSON, with a rendered preview of the Markdown report |

## Compare page

| Section | Shows |
|---|---|
| Headline tiles | 🏆 most reliable · 🎯 most accurate · ⚡ fastest p50 · 💰 cheapest per *correct* answer |
| Leaderboard | Reliability, accuracy, consistency, recovery, error rate, p50/p95, tokens, cost |
| **Overview** tab | Reliability score bars, and a **radar** of up to 3 agents (accuracy, consistency, recovery, stability, speed, cost efficiency) |
| **Speed & cost** tab | p50 vs p95 latency, accuracy-vs-cost scatter (top-left is best), tokens per task, total cost, and a data table |
| **Per task** tab | Task × agent **pass-rate heatmap** (⏰ marks time-sensitive tasks) and accuracy by category |
| **Failures & recovery** tab | Recovered / failed counts per agent, plus every failed execution with *why* it failed and a link to its trace |
| **🕰️ Freshness** tab | Only for runs with time-sensitive tasks. Per-agent freshness score and stale count, a 100%-stacked bar of answer verdicts (fresh / stale / wrong / unverified / no claim), and every stale or wrong answer with links to its sources (*why ↗*) and trace. The leaderboard also gains *Freshness* and *Stale answers* columns. |

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

## Stale-Answer Detector page

Three ways in:

- **Paste text**: any text, an optional *question it answers* (short answers such as "2024" need it), and the
  search depth. Four built-in examples: a stale answer, an up-to-date one, a short answer with its question,
  and today's date.
- **An agent's answer**: an execution ID, or the links *why ↗* (Compare → Freshness), *Check this answer* and
  *claims & sources ↗* (Traces). Shows the question and answer, and the existing check if there is one.
- **History**: earlier checks (`?check=<id>` links are shareable). Automatic checks from benchmark runs are
  hidden unless you tick the box.

The result shows a verdict banner (stale / wrong / up to date / unverified / nothing to verify), a stat-tile
row (freshness score and the count per claim verdict), and one card per claim, worst first. Each card has the
verdict with icon and confidence, the claim, **what is true today**, the judge's explanation, the search query,
and the sources with their publication dates, newest first. A data-table view lists every claim.

In mock mode a banner explains that sources come from a small built-in corpus. See
[stale-answer-detection.md](stale-answer-detection.md).

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
- **Status colors are reserved for outcomes**: trace steps (✅ final, ❌ error, 💥 fault, ⚠️ tool error, 🔁 retry) and
  freshness verdicts (✅ fresh, ⚠️ stale, ❌ wrong; gray for unverified / no claim). They always come with an icon and
  a label, and stacked segments are separated by a 2px surface gap.
- Every chart has hover tooltips, and key charts have a data-table view.

The theme is fixed to dark (`frontend/.streamlit/config.toml`) because the palette was validated against that surface.
