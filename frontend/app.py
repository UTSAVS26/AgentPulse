"""AgentPulse Streamlit dashboard — home page."""

import streamlit as st

import api

st.set_page_config(page_title="AgentPulse", page_icon="📈", layout="wide")


def sidebar_status() -> None:
    """Show backend connectivity and mock/live mode in the sidebar."""
    data, err = api.health()
    with st.sidebar:
        st.markdown("### System status")
        if err:
            st.error(err)
            return
        st.success(f"Backend online · v{data['version']}")
        if data.get("mock_mode"):
            st.warning("MOCK MODE: simulated LLM and search responses")
        else:
            st.info("LIVE MODE: Bifrost + Tavily")


sidebar_status()

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
"""
)
