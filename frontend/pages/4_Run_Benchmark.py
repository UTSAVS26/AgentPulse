"""Run Benchmark: start a suite × agents × repeats run, watch progress live, see the leaderboard."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
from ui import (
    ANSWER_VERDICTS,
    RUN_STATUS,
    leaderboard_frame,
    nav_link,
    page_setup,
    pct,
    run_label,
    show_error,
    statistics_caption,
)

health = page_setup("Run Benchmark", "🏁")
st.title("🏁 Run Benchmark")
st.caption(
    "Every selected agent attempts every task in the suite, `repeats` times. Results are scored, "
    "priced from token usage and aggregated into a reliability leaderboard."
)

suites, err = api.get("/suites")
agents, err2 = api.get("/agents")
if show_error(err or err2):
    st.stop()
if not suites or not agents:
    st.info("You need at least one suite and one agent.")
    st.stop()


def _chaos_token_panel() -> None:
    """The secret only Bifrost may use to call the outage-drill endpoint: create or rotate it here."""
    tok, err = api.get("/chaos/token")
    if err or not tok:
        return
    where = "created on this dashboard" if tok["source"] == "dashboard" else "from the CHAOS_TOKEN setting"
    st.caption(
        f"🔑 **Drill token** ({where}"
        + (f", {tok['created_at'][:16].replace('T', ' ')} UTC" if tok["created_at"] else "")
        + "): the secret Bifrost presents to the chaos provider, so nobody else can trigger it."
    )
    if tok["is_default"]:
        st.warning(
            "The shipped default token is in use. Create your own below: it is stored encrypted, set in "
            "Bifrost at runtime, and never shown again."
        )
    if not tok["can_manage"]:
        st.info(tok["note"])
        return
    with st.popover("🔑 Create or change the drill token"):
        own = st.text_input(
            "Your own token (optional, 24+ characters)",
            type="password",
            key="chaos_own_token",
            help="Leave empty to generate a strong random one.",
        )
        c1, c2 = st.columns(2)
        if c1.button("Create token", type="primary", key="chaos_create"):
            res, err = api.post("/chaos/token", json={"token": own.strip()} if own.strip() else None)
            if not show_error(err):
                st.session_state["chaos_flash"] = (
                    "New drill token active in Bifrost. The old one no longer works."
                )
                st.rerun()
        if tok["source"] == "dashboard" and c2.button("Back to CHAOS_TOKEN", key="chaos_reset"):
            _, err = api.delete("/chaos/token")
            if not show_error(err):
                st.session_state["chaos_flash"] = "Using the CHAOS_TOKEN setting again."
                st.rerun()
    flash = st.session_state.pop("chaos_flash", None)
    if flash:
        st.success(flash)


# ------------------------------------------------------------------------------------ configure
with st.container(border=True):
    st.subheader("Configure a run")
    suite_by_label = {
        f"{s['name']} · {s['task_count']} tasks"
        + (f" · {s['time_sensitive_count']} time-sensitive" if s["time_sensitive_count"] else ""): s
        for s in suites
    }
    labels_list = list(suite_by_label)
    wanted = st.session_state.get("suite_id")
    default_idx = next((i for i, lab in enumerate(labels_list) if suite_by_label[lab]["id"] == wanted), 0)
    s1, s2 = st.columns([4, 1])
    suite = suite_by_label[s1.selectbox("Task suite", labels_list, index=default_idx)]
    st.session_state["suite_id"] = suite["id"]
    nav_link(s2, "pages/3_Suites.py", "Edit tasks", "🧪")
    st.caption(suite["description"])
    if not suite["task_count"]:
        st.warning("This suite has no tasks yet. Add some on the **Suites** page.")
    with st.expander("Show tasks"):
        detail, err = api.get(f"/suites/{suite['id']}")
        if not show_error(err):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Key": t["key"],
                            "Prompt": t["prompt"],
                            "Expected": t["expected"],
                            "Scorer": t["scorer"] + (f" ±{t['tolerance']:g}" if t["tolerance"] else ""),
                            "Category": t["category"],
                            "Time-sensitive": "⏰" if t["time_sensitive"] else "",
                        }
                        for t in detail["tasks"]
                    ]
                ),
                hide_index=True,
                use_container_width=True,
            )

    agent_by_label = {f"{a['name']}  ·  {a['model']}": a for a in agents}
    default = [label for label, a in agent_by_label.items() if a["model"].startswith("groq/")][:4]
    chosen = st.multiselect("Agents", list(agent_by_label), default=default or list(agent_by_label)[:2])
    c1, c2 = st.columns(2)
    repeats = c1.slider(
        "Repeats", 1, 5, 3, help="Each agent attempts each task this many times (needed for consistency)."
    )
    fault_rate = c2.slider(
        "Fault injection rate",
        0.0,
        0.8,
        0.2,
        0.05,
        help="Probability that each tool call fails (reliability test).",
    )
    with st.expander("🌩️ Provider outage drill (Bifrost fallbacks)"):
        st.caption(
            "Simulate a provider outage: a share of every agent's model calls is routed through a *chaos* "
            "provider in Bifrost that fails them (HTTP 503). With fallbacks on, **Bifrost itself** retries "
            "each failed call on the agent's fallback models (or the same model through its real provider). "
            "Compare runs with fallbacks on and off to see what the gateway saves. Your own model "
            "connections are not drilled."
        )
        _chaos_token_panel()
        d1, d2 = st.columns([2, 1])
        outage_pct = d1.slider("Model calls failed by the outage", 0, 80, 0, 10, format="%d%%")
        outage_rate = outage_pct / 100
        drill_fallbacks = d2.checkbox("Bifrost fallbacks on", value=True, disabled=outage_rate == 0)

    check_freshness = True
    if suite["time_sensitive_count"]:
        check_freshness = st.checkbox(
            "🕰️ Check answers to time-sensitive tasks for staleness",
            value=True,
            help="Runs the stale-answer detector (claim extraction + Tavily search + LLM verdict) on every "
            "answer to a ⏰ task. Identical answers are checked once per run. In live mode this uses Tavily "
            "credits (1 per distinct claim).",
        )
    selected = [agent_by_label[c] for c in chosen]
    total = suite["task_count"] * len(selected) * repeats
    st.markdown(
        f"**{total} executions** = {suite['task_count']} tasks × {len(selected)} agents × {repeats} repeats"
    )
    live = health is not None and not health.get("mock_mode")
    if live:
        if any(a["model"].startswith("ollama/") for a in selected):
            st.warning("Local Ollama agents take ~15–30 s per task on CPU, so this run will be slower.")
        if any(a["model"].endswith(":free") for a in selected):
            st.info("Free OpenRouter models are rate-limited. 429s are retried with backoff.")
    if st.button("Start benchmark", type="primary", disabled=not selected or not suite["task_count"]):
        run, err = api.post(
            "/runs",
            json={
                "suite_id": suite["id"],
                "agent_ids": [a["id"] for a in selected],
                "repeats": repeats,
                "fault_rate": fault_rate,
                "check_freshness": check_freshness,
                "outage_rate": outage_rate,
                "drill_fallbacks": drill_fallbacks,
            },
        )
        if not show_error(err):
            st.session_state["run_id"] = run["id"]
            st.rerun()

# ------------------------------------------------------------------------------------ results
runs, err = api.get("/runs", params={"limit": 30})
if show_error(err) or not runs:
    st.info("No runs yet. Configure one above.")
    st.stop()

run_ids = [r["id"] for r in runs]
current = st.session_state.get("run_id", run_ids[0])
index = run_ids.index(current) if current in run_ids else 0
labels = {r["id"]: run_label(r) for r in runs}
run_id = st.selectbox("Run", run_ids, index=index, format_func=lambda i: labels[i])
st.session_state["run_id"] = run_id


def render_run() -> None:
    data, err = api.get(f"/runs/{run_id}/metrics", timeout=30)
    if show_error(err):
        return
    run = data["run"]
    active = run["status"] in ("pending", "running")

    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    c1.markdown(f"**Status:** {RUN_STATUS.get(run['status'], run['status'])}")
    if run.get("outage_rate"):
        c1.caption(
            f"🌩️ Outage drill: {run['outage_rate']:.0%} of model calls failed on purpose · Bifrost fallbacks "
            + ("on" if run.get("drill_fallbacks") else "off")
        )
    c2.metric("Executions", f"{run['progress_done']}/{run['progress_total']}")
    c3.metric("Mode", "mock" if run["mock_mode"] else "live")
    c4.metric("Duration", f"{run['duration_s']:.0f} s" if run["duration_s"] else "–")
    st.progress(run["progress_done"] / max(run["progress_total"], 1))
    if run["error"]:
        st.error(run["error"])
    if active and st.button("Cancel run"):
        _, err = api.post(f"/runs/{run_id}/cancel")
        if not show_error(err):
            st.rerun()

    board = data["leaderboard"]
    if any(r["executions"] for r in board):
        st.markdown("#### Leaderboard" + (" (partial)" if active else ""))
        st.dataframe(leaderboard_frame(board), hide_index=True, use_container_width=True)
        if not active:
            st.caption(statistics_caption(data))
        weights = " · ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in data["weights"].items())
        st.caption(
            f"Reliability = weighted mean of {weights}. Undefined components (e.g. consistency with "
            "1 repeat, recovery with no failures) are dropped and the weights renormalised. "
            "See docs/metrics.md."
        )
    elif active:
        st.info("Waiting for the first results…")

    if not active and st.session_state.get("_was_active") == run_id:
        st.session_state["_was_active"] = None
        st.rerun()  # stop polling: re-render the page once in its final state
    if active:
        st.session_state["_was_active"] = run_id


summary = next(r for r in runs if r["id"] == run_id)
polling = summary["status"] in ("pending", "running")
st.fragment(run_every=2 if polling else None)(render_run)()

# ------------------------------------------------------------------------------------ executions
if not polling:
    execs, err = api.get(f"/runs/{run_id}/executions", timeout=30)
    if not show_error(err) and execs:
        with st.expander(f"All executions ({len(execs)})"):
            df = pd.DataFrame(
                [
                    {
                        "Trace": f"Traces?execution={e['id']}",
                        "ID": e["id"],
                        "Agent": e["agent_name"],
                        "Task": e["task_key"],
                        "Repeat": e["repeat_idx"] + 1,
                        "Passed": "✅" if e["passed"] else "❌",
                        "Status": e["status"],
                        "Answer": (e["output"] or e["error"] or "")[:160],
                        "Scoring": (e["score_detail"] or "").split(" · price:")[0],
                        "Faults": e["faults_injected"],
                        "Recovered": "🛡️" if e["recovered"] else "",
                        "Freshness": ANSWER_VERDICTS.get(e.get("freshness_verdict") or "", ""),
                        "Latency (s)": round(e["latency_ms"] / 1000, 2),
                        "Tokens": e["prompt_tokens"] + e["completion_tokens"],
                    }
                    for e in execs
                ]
            )
            only_failed = st.checkbox("Only failed")
            if only_failed:
                df = df[df["Passed"] == "❌"]
            st.dataframe(
                df,
                hide_index=True,
                use_container_width=True,
                column_config={"Trace": st.column_config.LinkColumn("Trace", display_text="open ↗")},
            )
            st.caption(
                f"Pass rate {pct(sum(e['passed'] for e in execs) / len(execs))}. "
                "Click **open ↗** for the step-by-step trace, or see **Compare** for charts."
            )
