# Demo script (4–5 minutes)

A click path through the whole product, with what to say at each step. It works in **mock mode** with no keys
or network. Steps 4b and 6b show the **live** extras when keys and quota are available.

## Before you start (5 minutes, the day before and again 30 minutes before)

| Check | Command / action | Expected |
|---|---|---|
| Stack up from a clean state | `make reset && make up` | 4 containers healthy; the 2 demo runs finish within ~1 min |
| Everything green | `make health` | backend `ok`, Bifrost `ok`, dashboard `ok` |
| Live keys and quota (if presenting live) | `make live-check` | `7/7 checks passed` |
| Detector live | Detector → paste "The current UK PM is Rishi Sunak." | ⚠️ Outdated with dated news sources |
| Public URL (if presenting the deployment) | open https://agentpulse-9551.onrender.com about 2 minutes early | the free instance spins down when idle; the first load takes ~1 min |

Groq's free tier allows ~200k tokens/day **per model**. Don't benchmark heavily on demo day. If presenting
live, set `FRESHNESS_MODEL` to a model no agent in your live run uses (see
[stale-answer-detection.md](stale-answer-detection.md#quota-planning-for-a-live-demo)).

Browser zoom 90%, dark theme, one tab per page you'll show.

## The path

### 1. Home (20 s)
> "AgentPulse answers two questions about AI agents: *which one can I rely on?*, by benchmarking agents on
> identical tasks through the Bifrost gateway, and *is its answer still true today?*, with stale-answer
> detection verified live with Tavily."

Point at **How to use it** and the **What each page does** cards: every page is explained in plain words.
Point at the sidebar: **mock / live mode**, component health and *your models*.

### 1b. Models: bring your own (30 s, live)
**Models** → **➕ Add a model connection** → **Ollama** preset → **🔍 Fetch models**, pick one, then
**🧪 Test first model** and **💾 Save**.
> "Any OpenAI-compatible model plugs in: a laptop model, or OpenAI, Anthropic or Gemini with your own key. Hosted
> providers are registered *inside Bifrost* at runtime, so they take the same gateway path as everything else.
> Keys are encrypted and never shown again."

Point at the card: 🛰️ via Bifrost or ↗️ direct, **and why**, plus tool support ✓/✗. Then **🤖 New agent**.

### 2. Agents (30 s)
> "An agent is a model plus a system prompt, tools and a step budget. Six are pre-configured across Groq,
> OpenRouter and a local Ollama model, all called through one OpenAI-compatible Bifrost endpoint."

**Try an agent**: pick *GPT-OSS 120B · tools*, the *Arithmetic (calculator)* example, fault rate **50%**,
then **Run agent**. Show the trace: the tool call, then the 💥 injected fault, then the retry, then the
correct answer.
> "We inject faults on purpose: timeouts, HTTP 500s, empty and malformed results. Reliability means
> recovering from them."

### 2b. Suites: build your own benchmark (20 s)
**Suites** → *Math & Reasoning* → **📄 Duplicate**, edit a cell, add a row, **💾 Save changes**. Open
**🧪 Test a scorer** and grade a sample answer.
> "Built-in test sets are read-only. Duplicate one, or import a CSV, and benchmark on *your* questions."

### 3. Run Benchmark (40 s)
Choose *Current Events (time-sensitive)* and 3 agents, including the **no-tools baseline**. Set repeats to
2 and faults to 20%. Leave **Check answers for staleness** ticked, then **Start**.
> "Every agent gets the same tasks, repeated to measure consistency. The leaderboard fills in live: reliability
> weights accuracy, consistency, recovery and stability."

While it runs (about 20 s in mock mode), point at the partial leaderboard.

### 4. Compare (60 s)
- **Tiles**: most reliable, most accurate, fastest, cheapest per *correct* answer.
- **Overview**: reliability bars and the radar.
  > "Colour follows the agent on every chart."
- **Speed & cost**: the accuracy-vs-cost scatter (top-left is best).
- **Per task**: the heatmap.
  > "⏰ rows are time-sensitive. The no-tools baseline fails exactly those: it answers from training data."
- **🕰️ Freshness**:
  > "Every answer to a time-sensitive task was checked automatically. The baseline is stale most of the
  > time."

  Click **why ↗** on a stale row.

**4b (live)**: in a live run, the stale rows cite real news articles with dates.

### 5. Stale-Answer Detector (60 s)
You land on the answer from **why ↗**: *"Keir Starmer"* is ⚠️ outdated, and **True today** says Andy
Burnham, with sources newest first.

Switch to **Paste text** and load the example **A stale answer**, then **Check freshness**:
- Sunak → ⚠️ outdated, Argentina → ⚠️ outdated, "Water boils at 100 °C" → ➖ timeless (not searched).
  > "We only spend searches on claims that can go stale. Claims about today's date are checked against the
  > clock."
- Explain outdated vs contradicted:
  > "Outdated means it *was* true; contradicted means it never was. The freshness score is current ÷
  > decisive claims."

**6b (live)**: paste anything topical from today's news. Results come back in 5–15 s with real dated
sources.

### 6. Traces (30 s)
Open a trace from Compare → *Failures & recovery* → **open ↗**.
> "Every model call, tool call, fault and retry, with tokens and latency. The timeline shows where the time
> went: those red bars are injected 5-second timeouts."

### 7. Reports (20 s)
Download the **Markdown report** and show the preview.
> "Everything exports as Markdown, CSV and JSON, with the same numbers as the dashboard. That includes the
> stale answers with their corrections and sources."

### Close (10 s)
> "Bring any model, build any benchmark. One gateway for every model, Bifrost, so the comparison is fair. Live verification, Tavily, so answers are
> checked against today's world, not training data. It works offline in mock mode and is deployed publicly."

## Fallback plan

| Problem | Action | Time |
|---|---|---|
| Venue network down | `MOCK_MODE=true` in `.env`, then `make restart`. Everything works offline. | < 30 s |
| Groq rate-limited (`429 … per day`) | Switch to mock (above), or show the live sample report in [examples/live-run-report.md](examples/live-run-report.md) | < 30 s |
| A live check returns *unverifiable* | Expand the claim: the explanation says why (search or judge quota). Mention that it degrades safely by design. | — |
| Dashboard shows "Cannot reach the backend" | `make restart`, then `make health` | ~30 s |
| Docker itself broken | Use the public deployment (mock by default, live with the access code) | — |
| Empty dashboard | `make reset && make up`; demo data is re-seeded within a minute | ~2 min |
