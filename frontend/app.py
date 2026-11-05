"""AgentPulse Streamlit dashboard: home page."""

import streamlit as st

from ui import page_setup

page_setup("Home")

st.title("📈 AgentPulse")
st.subheader("AI agent benchmarking & reliability platform")
st.markdown(
    """
AgentPulse runs **multiple AI agents on identical tasks** and compares them on
accuracy, reliability, token use, cost, latency and execution steps. It also checks
whether their answers are **still true today** with live web verification.

| Capability | Powered by |
|---|---|
| Agent execution, routing across providers | **Bifrost** AI gateway |
| Live search & claim verification | **Tavily** |
| Scoring, reliability & freshness analytics | AgentPulse evaluation engine |

**Get started**

1. **Agents**: inspect the pre-configured agents and try one on a prompt, with optional fault injection.
2. **Run Benchmark**: run a suite × agents × repeats; **Compare** and **Traces** explain the results.
3. **Stale-Answer Detector**: paste any AI answer and see which claims are no longer true, with dated sources.
   Benchmark answers to time-sensitive tasks are checked automatically.
"""
)
