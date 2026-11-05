"""Compare: multi-agent comparison dashboard for one benchmark run."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
import charts
from ui import (
    ANSWER_VERDICTS,
    DETECTOR_PAGE,
    leaderboard_frame,
    page_setup,
    pct,
    run_label,
    secs,
    show_error,
    tile,
    usd,
)

page_setup("Compare", "📊")
st.title("📊 Compare agents")
st.caption("Every agent ran the same tasks through the same gateway. Pick a run to compare its agents.")

runs, err = api.get("/runs", params={"limit": 50})
if show_error(err):
    st.stop()
runs = [r for r in runs or [] if r["progress_done"] > 0]
if not runs:
    st.info("No benchmark results yet. Start a run on the **Run Benchmark** page.")
    st.stop()

run_ids = [r["id"] for r in runs]
current = st.session_state.get("run_id")
labels = {r["id"]: run_label(r) for r in runs}
run_id = st.selectbox(
    "Run", run_ids, index=run_ids.index(current) if current in run_ids else 0, format_func=lambda i: labels[i]
)
st.session_state["run_id"] = run_id

data, err = api.get(f"/runs/{run_id}/metrics", timeout=30)
if show_error(err):
    st.stop()
run = data["run"]
board = [r for r in data["leaderboard"] if r["executions"]]
if not board:
    st.info("This run has no finished executions yet.")
    st.stop()
if run["status"] in ("pending", "running"):
    st.warning(f"Run in progress ({run['progress_done']}/{run['progress_total']}): figures are partial.")

colors = charts.agent_colors(run["agent_ids"])

# ------------------------------------------------------------------------------------ headline tiles
best = max(board, key=lambda r: r["reliability_score"] or -1)
most_accurate = max(board, key=lambda r: r["accuracy"] or -1)
fastest = min((r for r in board if r["latency_ms_p50"] is not None), key=lambda r: r["latency_ms_p50"])
priced = [r for r in board if r["cost_per_correct_usd"] is not None]
cheapest = min(priced, key=lambda r: r["cost_per_correct_usd"]) if priced else None

c1, c2, c3, c4 = st.columns(4)
tile(c1, "🏆 Most reliable", pct(best["reliability_score"]), f"**{best['agent_name']}**")
tile(c2, "🎯 Most accurate", pct(most_accurate["accuracy"]), f"**{most_accurate['agent_name']}**")
tile(c3, "⚡ Fastest (p50)", secs(fastest["latency_ms_p50"]), f"**{fastest['agent_name']}**")
tile(
    c4,
    "💰 Cheapest per correct answer",
    usd(cheapest["cost_per_correct_usd"]) if cheapest else "–",
    f"**{cheapest['agent_name']}**" if cheapest else "no correct answers",
)

st.markdown("#### Leaderboard")
st.dataframe(leaderboard_frame(data["leaderboard"]), hide_index=True, use_container_width=True)

has_freshness = any(r.get("freshness_checked") for r in board)
tab_names = ["Overview", "Speed & cost", "Per task", "Failures & recovery"]
tabs = st.tabs(tab_names + (["🕰️ Freshness"] if has_freshness else []))
tab_overview, tab_cost, tab_tasks, tab_failures = tabs[:4]
CHART_CFG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]}

# ------------------------------------------------------------------------------------ overview
with tab_overview:
    left, right = st.columns([1, 1])
    with left:
        st.plotly_chart(charts.reliability_bars(board, colors), use_container_width=True, config=CHART_CFG)
        weights = " · ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in data["weights"].items())
        st.caption(f"Reliability = {weights} (undefined parts dropped and weights renormalised).")
    with right:
        names = {r["agent_id"]: r["agent_name"] for r in board}
        default = [r["agent_id"] for r in sorted(board, key=lambda r: r["rank"])][
            : charts.MAX_ALL_PAIRS_SERIES
        ]
        picked = st.multiselect(
            f"Agents on the radar (max {charts.MAX_ALL_PAIRS_SERIES})",
            list(names),
            default=default,
            format_func=lambda a: names[a],
            max_selections=charts.MAX_ALL_PAIRS_SERIES,
        )
        if picked:
            st.plotly_chart(charts.radar(board, picked, colors), use_container_width=True, config=CHART_CFG)
        st.caption(
            "Speed = fastest p50 ÷ this agent's p50 · Cost efficiency = free/local agents 100%, paid "
            "agents: cheapest paid cost per task ÷ this agent's · Stability = 1 − error rate."
        )

# ------------------------------------------------------------------------------------ speed & cost
with tab_cost:
    left, right = st.columns(2)
    with left:
        st.plotly_chart(charts.latency_chart(board), use_container_width=True, config=CHART_CFG)
        st.plotly_chart(
            charts.metric_bars(board, colors, "tokens_mean", "Tokens per task (mean)", "int"),
            use_container_width=True,
            config=CHART_CFG,
        )
    with right:
        st.plotly_chart(charts.accuracy_vs_cost(board), use_container_width=True, config=CHART_CFG)
        st.plotly_chart(
            charts.metric_bars(board, colors, "cost_usd_total", "Total cost of the run (USD)", "usd"),
            use_container_width=True,
            config=CHART_CFG,
        )
    st.caption(
        "Costs use each model's list price from Bifrost's model catalogue (local Ollama = $0). "
        + ("This is a **mock** run: token counts and costs are simulated." if run["mock_mode"] else "")
    )
    with st.expander("Data table"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Agent": r["agent_name"],
                        "p50 (s)": None
                        if r["latency_ms_p50"] is None
                        else round(r["latency_ms_p50"] / 1000, 2),
                        "p95 (s)": None
                        if r["latency_ms_p95"] is None
                        else round(r["latency_ms_p95"] / 1000, 2),
                        "Tokens / task": None if r["tokens_mean"] is None else round(r["tokens_mean"]),
                        "Total tokens": r["tokens_total"],
                        "Cost / task ($)": r["cost_usd_mean"],
                        "Total cost ($)": r["cost_usd_total"],
                        "Cost / correct ($)": r["cost_per_correct_usd"],
                        "Steps / task": None if r["steps_mean"] is None else round(r["steps_mean"], 2),
                        "Tool calls / task": None
                        if r["tool_calls_mean"] is None
                        else round(r["tool_calls_mean"], 2),
                    }
                    for r in board
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )

# ------------------------------------------------------------------------------------ per task
with tab_tasks:
    st.plotly_chart(
        charts.task_heatmap(data["task_matrix"], board), use_container_width=True, config=CHART_CFG
    )
    st.caption("⏰ = time-sensitive task (answers change over time). Cells show pass rate across repeats.")
    st.plotly_chart(charts.category_bars(board, colors), use_container_width=True, config=CHART_CFG)

# ------------------------------------------------------------------------------------ failures
with tab_failures:
    cols = st.columns(len(board)) if len(board) <= 4 else None
    for i, r in enumerate(board):
        target = cols[i] if cols else st
        tile(
            target,
            r["agent_name"],
            f"{r['recovered']}/{r['executions_with_failures']} recovered",
            f"{r['faults_injected']} faults · {r['llm_retries']} retries · {pct(r['error_rate'])} errors",
            help="Recovered = a failure happened (injected fault, tool error, rejected tool call or "
            "LLM retry) and the task was still answered correctly.",
        )
    execs, err = api.get(f"/runs/{run_id}/executions", timeout=30)
    if not show_error(err) and execs:
        failed = [e for e in execs if not e["passed"]]
        st.markdown(f"#### Failed executions ({len(failed)} of {len(execs)})")
        if failed:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Trace": f"Traces?execution={e['id']}",
                            "Agent": e["agent_name"],
                            "Task": e["task_key"],
                            "Repeat": e["repeat_idx"] + 1,
                            "Status": e["status"],
                            "Why": (e["score_detail"] or "").split(" · price:")[0],
                            "Answer": (e["output"] or e["error"] or "")[:160],
                            "Faults": e["faults_injected"],
                        }
                        for e in failed
                    ]
                ),
                hide_index=True,
                use_container_width=True,
                column_config={"Trace": st.column_config.LinkColumn("Trace", display_text="open ↗")},
            )
        else:
            st.success("Every execution passed.")

# ------------------------------------------------------------------------------------ freshness
if has_freshness:
    with tabs[4]:
        st.caption(
            "Answers to ⏰ time-sensitive tasks were checked by the **stale-answer detector**: claims "
            "extracted, searched on Tavily and judged *current*, *outdated* or *contradicted*. Freshness is "
            "reported next to, not inside, the reliability score. The detector's own LLM/search usage is not "
            "added to the agents' cost."
        )
        fresh_board = [r for r in board if r.get("freshness_checked")]
        cols = st.columns(len(fresh_board)) if len(fresh_board) <= 4 else None
        for i, r in enumerate(fresh_board):
            tile(
                cols[i] if cols else st,
                r["agent_name"],
                pct(r["freshness"]),
                f"{r['stale_answers']} stale of {r['freshness_checked']} checked",
                help="Freshness = mean share of an answer's time-sensitive claims that are current.",
            )
        st.plotly_chart(charts.freshness_bars(board), use_container_width=True, config=CHART_CFG)

        execs, err = api.get(f"/runs/{run_id}/executions", timeout=30)
        if not show_error(err) and execs:
            flagged = [e for e in execs if e.get("freshness_verdict") in ("stale", "wrong")]
            st.markdown(f"#### Stale or wrong answers ({len(flagged)})")
            if flagged:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Check": f"{DETECTOR_PAGE}?execution={e['id']}",
                                "Trace": f"Traces?execution={e['id']}",
                                "Agent": e["agent_name"],
                                "Task": e["task_key"],
                                "Repeat": e["repeat_idx"] + 1,
                                "Freshness": ANSWER_VERDICTS.get(e["freshness_verdict"], ""),
                                "Answer": (e["output"] or "")[:160],
                                "Passed": "✅" if e["passed"] else "❌",
                            }
                            for e in flagged
                        ]
                    ),
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Check": st.column_config.LinkColumn("Sources", display_text="why ↗"),
                        "Trace": st.column_config.LinkColumn("Trace", display_text="open ↗"),
                    },
                )
            else:
                st.success("No stale answers in this run.")
