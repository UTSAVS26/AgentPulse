"""AgentPulse Streamlit dashboard: home page."""

import streamlit as st

import api
from ui import page_setup, pct, run_label, secs

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

# ------------------------------------------------------------------------------------ latest results
runs, err = api.get("/runs", params={"limit": 20})
finished = [r for r in runs or [] if r["status"] == "done"]
if finished:
    latest = finished[0]
    data, err = api.get(f"/runs/{latest['id']}/metrics", timeout=30)
    if data and not err:
        st.markdown("#### Latest results")
        st.caption(run_label(latest))
        board = [r for r in data["leaderboard"] if r["executions"]][:4]
        cols = st.columns(len(board))
        for col, r in zip(cols, board, strict=True):
            with col.container(border=True):
                st.markdown(f"**#{r['rank']} {r['agent_name']}**")
                st.caption(f"`{r['model']}`")
                st.markdown(
                    f"Reliability **{pct(r['reliability_score'])}** · accuracy {pct(r['accuracy'])}  \n"
                    f"p50 {secs(r['latency_ms_p50'])}"
                    + (f" · freshness {pct(r['freshness'])}" if r.get("freshness_checked") else "")
                )
        c1, c2, c3, c4 = st.columns(4)
        c1.page_link("pages/5_Compare.py", label="Compare agents", icon="📊")
        c2.page_link("pages/6_Traces.py", label="Inspect traces", icon="🔍")
        c3.page_link("pages/7_Stale_Answer_Detector.py", label="Check an answer", icon="🕰️")
        c4.page_link("pages/8_Reports.py", label="Download a report", icon="📄")
