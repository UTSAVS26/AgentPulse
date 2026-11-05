---
title: AgentPulse
emoji: 📈
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
short_description: Benchmark AI agents and detect stale answers
---

# AgentPulse: AI agent benchmarking & reliability

Run multiple AI agents on **identical tasks** and compare accuracy, consistency, failure recovery,
tokens, cost and latency. Find out whether their answers are **still true today**.

- **Bifrost** (Maxim AI) routes every agent's LLM calls through one gateway (Groq, OpenRouter).
- **Tavily** provides live web search for the agents and for stale-answer verification.

### Public demo modes

| Mode | Who | What runs |
|---|---|---|
| **Mock** (default) | Everyone | Simulated models and search: the full product with no API calls |
| **Live** | Holders of the access code (sidebar → *Unlock live mode*) | Real models via Bifrost and live Tavily search |

Data is stored in the container and resets when the Space restarts.

Source, docs and local setup: see the GitHub repository linked from the Space header.
This Space is deployed automatically from the `main` branch by GitHub Actions.
