"""Traces: inspect one execution step by step (model calls, tool calls, faults, retries)."""

from __future__ import annotations

import streamlit as st

import api
import charts
from ui import (
    ANSWER_VERDICTS,
    DETECTOR_PAGE,
    execution_summary,
    page_setup,
    render_trace,
    run_label,
    show_error,
)

page_setup("Traces", "🔍")
st.title("🔍 Execution traces")
st.caption("Every model call, tool call, injected fault and retry, in order, with tokens and latency.")


def _set_execution(execution_id: int | None) -> None:
    if execution_id:
        st.query_params["execution"] = str(execution_id)
    elif "execution" in st.query_params:
        del st.query_params["execution"]


# ------------------------------------------------------------------------------------ pick an execution
linked = st.query_params.get("execution")
execution_id: int | None = int(linked) if linked and linked.isdigit() else None

source = st.radio(
    "Find an execution",
    ["From a benchmark run", "Ad-hoc runs (Try an agent)", "By ID"],
    horizontal=True,
    index=2 if execution_id else 0,
)

if source == "By ID":
    typed = st.number_input("Execution ID", min_value=1, step=1, value=execution_id or 1)
    opened = st.button("Open", type="primary")
    execution_id = int(typed) if (opened or execution_id) else None

elif source == "From a benchmark run":
    runs, err = api.get("/runs", params={"limit": 50})
    if show_error(err):
        st.stop()
    runs = [r for r in runs or [] if r["progress_done"] > 0]
    if not runs:
        st.info("No benchmark executions yet. Start a run on **Run Benchmark**.")
        st.stop()
    labels = {r["id"]: run_label(r) for r in runs}
    run_ids = list(labels)
    current = st.session_state.get("run_id")
    run_id = st.selectbox(
        "Run",
        run_ids,
        index=run_ids.index(current) if current in run_ids else 0,
        format_func=lambda i: labels[i],
    )
    execs, err = api.get(f"/runs/{run_id}/executions", timeout=30)
    if show_error(err) or not execs:
        st.stop()
    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    agent = c1.selectbox("Agent", sorted({e["agent_name"] for e in execs}))
    of_agent = [e for e in execs if e["agent_name"] == agent]
    only_failed = c4.checkbox("Failed only", help="Show only tasks this agent failed at least once.")
    task_keys = sorted({e["task_key"] for e in of_agent if (not only_failed or not e["passed"])})
    if not task_keys:
        st.success(f"{agent} passed every task in this run.")
        st.stop()
    task = c2.selectbox(
        "Task",
        task_keys,
        format_func=lambda k: ("✅ " if all(e["passed"] for e in of_agent if e["task_key"] == k) else "❌ ")
        + k,
    )
    repeats = sorted((e for e in of_agent if e["task_key"] == task), key=lambda e: e["repeat_idx"])
    rep = c3.selectbox(
        "Repeat",
        repeats,
        format_func=lambda e: f"#{e['repeat_idx'] + 1} {'✅' if e['passed'] else '❌'}",
    )
    execution_id = rep["id"]

else:  # ad-hoc
    agents, err = api.get("/agents")
    if show_error(err) or not agents:
        st.stop()
    by_name = {a["name"]: a for a in agents}
    agent = by_name[st.selectbox("Agent", list(by_name))]
    history, err = api.get(f"/agents/{agent['id']}/executions", params={"limit": 50})
    if show_error(err):
        st.stop()
    adhoc = [e for e in history or [] if e["run_id"] is None]
    if not adhoc:
        st.info("No ad-hoc executions for this agent yet. Use **Agents → Try an agent**.")
        st.stop()
    picked = st.selectbox(
        "Execution",
        adhoc,
        format_func=lambda e: f"#{e['id']} · {e['created_at'][:19].replace('T', ' ')} · {e['prompt'][:60]}",
    )
    execution_id = picked["id"]

_set_execution(execution_id)
if not execution_id:
    st.stop()

# ------------------------------------------------------------------------------------ show it
ex, err = api.get(f"/executions/{execution_id}")
if show_error(err):
    st.stop()

st.divider()
title = f"Execution #{ex['id']} · {ex['agent_name']} · `{ex['model']}`"
if ex["run_id"]:
    title += f" · run #{ex['run_id']} · repeat {ex['repeat_idx'] + 1}"
st.markdown(f"### {title}")

left, right = st.columns([3, 2])
with left:
    st.markdown("**Prompt**")
    st.info(ex["prompt"] or "–")
    st.markdown("**Answer**")
    st.markdown(ex["output"] or "_(no answer)_")
with right:
    if ex["passed"] is not None:
        verdict = "✅ Passed" if ex["passed"] else "❌ Failed"
        st.markdown(f"**Score:** {verdict} ({ex['score']:.2f})")
        if ex.get("score_detail"):
            detail, _, price = ex["score_detail"].partition(" · price: ")
            st.caption(f"Scoring: {detail}" + (f" · pricing source: {price}" if price else ""))
    st.markdown(f"**Cost:** ${ex['cost_usd']:.6f}" if ex["cost_usd"] else "**Cost:** $0 (or not priced)")
    if ex.get("freshness_verdict"):
        label = ANSWER_VERDICTS.get(ex["freshness_verdict"], ex["freshness_verdict"])
        st.markdown(
            f"**Freshness:** {label} · [claims & sources ↗]({DETECTOR_PAGE}?check={ex['freshness_check_id']})"
        )
    elif ex["output"]:
        st.caption(f"[Check this answer for stale claims ↗]({DETECTOR_PAGE}?execution={ex['id']})")
    st.caption(f"Share this trace: add `?execution={ex['id']}` to the Traces page URL.")

execution_summary(ex)
if ex["trace"]:
    st.plotly_chart(
        charts.trace_timeline(ex["trace"]),
        use_container_width=True,
        config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
    )
st.markdown("#### Steps")
render_trace(ex["trace"])
