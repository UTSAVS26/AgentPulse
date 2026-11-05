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

**Get started:** open **Agents** in the sidebar to inspect the pre-configured agents and run
one on a prompt, with optional fault injection to test how it recovers.
"""
)
