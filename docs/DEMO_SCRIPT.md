# Demo script (4–5 minutes, plus the v3 add-ons)

A click path through the whole product, with what to say at each step. It works in **mock mode** with no keys
or network. Steps 4b and 6b show the **live** extras when keys and quota are available. **Steps 4c, 5b, 5c and 6c
are the v3 additions**: pick two or three for a short slot, or add them all for a 9-minute walk-through.

## Before you start (5 minutes, the day before and again 30 minutes before)

| Check | Command / action | Expected |
|---|---|---|
| Stack up from a clean state | `make reset && make up` | 4 containers healthy; the 2 demo runs finish within ~1 min |
| Everything green | `make health` | backend `ok`, Bifrost `ok`, dashboard `ok` |
| Live keys and quota (if presenting live) | `make live-check` | `7/7 checks passed` |
| Detector live | Detector → paste "The current UK PM is Rishi Sunak." | ⚠️ Outdated with dated news sources |
| Public URL (if presenting the deployment) | open https://agentpulse-9551.onrender.com about 2 minutes early | the free instance spins down when idle; the first load takes ~1 min |

Groq's free tier allows ~200k tokens/day **per model**. Don't benchmark heavily on demo day. The detector and
judges default to OpenRouter models no agent uses, so they don't share the agents' quotas (see
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

### 4c. Is the ranking real? (30 s, v3.1)
Back on **Compare** → the leaderboard.
> "Most gaps between agents are noise at this sample size, so every score has a 95% interval and a *tier*:
> agents whose difference could be luck share a tier. Here the top three are statistically tied, and AgentPulse
> says so instead of crowning one."

Point at the **95% CI** and **Tier** columns, the error bars, and the *"statistically tied"* note on the
*Most reliable* tile. For a free-text suite use the **`jury`** scorer: three judge models from different labs vote
(Fleiss κ and the most disputed tasks are shown), because no single judge should be ground truth.

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

### 5b. Live Freshness Bench (90 s, v3.2, the flagship)
**📡 Freshness Bench** → **Generate today's bench** (mock: instant; live: about a minute for 12 cells), then
**Show all questions with their sources**.
> "A benchmark that can't be memorised, because it didn't exist yesterday. Tavily finds dated news from this week
> back to two years ago, a model writes one question per article, and a second check throws out questions the
> article doesn't support."

**Run this bench** (the default pair is GPT-OSS 120B with and without tools), then read **Knowledge cutoff**:
> "Accuracy by event age. The agent without tools falls off a cliff at its *effective knowledge cutoff*; the
> same model with web search stays flat. This measures what a model actually knows, not what its model card says."

### 5c. Freshness Guard (90 s, v3.3 and v3.4)
**🛡️ Freshness Guard** → pick *"Who is the current prime minister of the United Kingdom?"* and send it in
**Annotate**, then **Correct**, then **Block**.
> "Any app that uses an OpenAI SDK changes one line, its base URL, and every answer is fact-checked on the way
> through: annotated, rewritten from dated sources with citations, or blocked."

Open **🔐 Access**: issue a key with only `guard:annotate`, paste it into *Call as*, switch to **Block** and send.
> "That key may annotate but not block: a 403. Callers use API keys, OIDC ID tokens from your identity provider,
> or the access code, each with scopes and a rate limit."

From a terminal: `python scripts/guard_demo.py` (plain OpenAI SDK, three modes).

### 6. Traces (30 s)
Open a trace from Compare → *Failures & recovery* → **open ↗**.
> "Every model call, tool call, fault and retry, with tokens and latency. The timeline shows where the time
> went: those red bars are injected 5-second timeouts."

### 6c. Bifrost, deeper (60 s, v3.4)
**Run Benchmark** → *Factual QA*, two agents → **🌩️ Provider outage drill**: 50% outage, **Bifrost fallbacks on**,
start. Then **Compare** → **🛰️ Gateway**.
> "Half of every agent's model calls now hit a simulated provider outage. Bifrost's own fallback machinery
> retries them on a backup model: 'rescued by Bifrost' counts the answers that would otherwise have been lost.
> Run it again with fallbacks off to see the difference. Bifrost's own overhead is about 5 milliseconds per call."

In **Traces**, point at *"🛟 rescued by a Bifrost fallback (chaos → groq)"* and *"via 🛰️ Bifrost MCP (Tavily)"*:
> "Even web search goes through the gateway, as a tool call over MCP, so one gateway carries every call."

Optional: **🔑 Create or change the drill token** (the secret only Bifrost may use to trigger the chaos
provider; stored encrypted and pushed into Bifrost at runtime).

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
| Live bench generation is slow or out of credit | Use a smaller bench (2 categories × 6 ages), or the demo bench that is already in mock mode | < 1 min |
| Guard says "Mock mode" | Expected without keys: the simulated model answers demo topics from old training data, so there is something to catch | — |
| Outage drill shows no rescues | Make sure *Bifrost fallbacks on* is ticked and the outage is at least 30%; in live mode, Bifrost must be running | — |
