"""Live Freshness Bench: a benchmark written from today's news, and each agent's knowledge cutoff."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
import charts
from ui import RUN_STATUS, page_setup, pct, run_label, show_error

health = page_setup("Freshness Bench", "📡")
st.title("📡 Live Freshness Bench")
st.caption(
    "A benchmark that can't be memorised, because it didn't exist yesterday. **Tavily** finds dated news "
    "from this week back to two years ago; a model writes one question per article and a second check "
    "throws out questions the article doesn't support, ambiguous ones and guessable ones. Running agents "
    "on it shows **how accuracy falls for newer events**, and so each model's *effective knowledge cutoff*. "
    "Agents with web search should stay flat: they look things up."
)
live = health is not None and not health.get("mock_mode")
if health is not None and not live:
    st.info(
        "**Mock mode:** questions come from a *fictional* news corpus (invented teams, companies and "
        "places, dated relative to today), and mock models have built-in knowledge cutoffs. "
        + (
            "Unlock live mode in the sidebar to generate from real news."
            if health.get("live_available")
            else ""
        )
    )

cfg, err = api.get("/freshbench/config")
if show_error(err):
    st.stop()


# ------------------------------------------------------------------------------------ 1. generate
st.markdown("### 1. Generate today's bench")


def render_job() -> None:
    """Progress of the newest generation job (polls while it runs)."""
    jobs, err = api.get("/freshbench/jobs")
    if err or not jobs:
        return
    job = jobs[0]
    if job["status"] == "running":
        total = max(job["progress_total"], 1)
        st.progress(
            job["progress_done"] / total,
            text=f"Generating… {job['progress_done']}/{total} " f"category × age cells · {job['note']}",
        )
        st.session_state["fb_polling"] = True
        return
    if st.session_state.pop("fb_polling", False):
        if job["status"] == "done":
            st.session_state["fb_suite"] = job["suite_id"]
        st.rerun()  # refresh the whole page (new snapshot in the lists below)
    if job["status"] == "done" and job.get("summary"):
        s = job["summary"]
        rejected = ", ".join(f"{n} {why}" for why, n in s["rejected"].items()) or "none"
        st.success(
            f"Last generation ({'live' if job['live'] else 'mock'}, "
            f"{job['finished_at'][:16].replace('T', ' ')}"
            f" UTC): **{s['questions']} questions** from {s['articles']} articles · rejected: {rejected}."
        )
    elif job["status"] == "failed":
        st.error(f"Last generation failed: {job['error']}")


with st.container(border=True):
    cats = list(cfg["categories"])
    bucket_labels = {b["key"]: b["label"] for b in cfg["buckets"]}
    c1, c2 = st.columns(2)
    chosen_cats = c1.multiselect("News categories", cats, default=cats)
    chosen_buckets = c2.multiselect(
        "Event ages", list(bucket_labels), default=list(bucket_labels), format_func=bucket_labels.get
    )
    c3, c4, c5 = st.columns([1, 1, 2])
    per_cell = c3.slider("Questions per cell", 1, 3, int(cfg["per_cell"]), help="Per category × event age.")
    scorer = c4.selectbox(
        "Grading",
        ["contains", "judge", "jury"],
        help="contains: the answer (or an accepted alias) appears in the reply · judge / jury: graded by "
        "judge models (live mode, paid).",
    )
    cells = len(chosen_cats) * len(chosen_buckets)
    c5.caption(
        f"**{cells} cells → up to {cells * per_cell} questions.** "
        + (
            f"Live cost: {cells} Tavily searches (basic, 1 credit each) and about "
            f"{2 * cells * per_cell + cells} "
            "model calls (writer + validator). Takes a few minutes."
            if live
            else "Mock: free and instant."
        )
    )
    force = st.checkbox("Make a new snapshot even if today's exists", value=False)
    if st.button("📡 Generate today's bench", type="primary", disabled=not cells):
        res, err = api.post(
            "/freshbench/generate",
            json={
                "categories": chosen_cats,
                "buckets": chosen_buckets,
                "per_cell": per_cell,
                "scorer": scorer,
                "force": force,
            },
        )
        if not show_error(err):
            if res["job"] is None:
                st.session_state["fb_suite"] = res["existing_suite_id"]
                st.info(f"{res['message']}: it's selected below. Tick the box above to make a new one.")
            else:
                st.session_state["fb_polling"] = True
    running = st.session_state.get("fb_polling", False)
    st.fragment(run_every=2 if running else None)(render_job)()

# ------------------------------------------------------------------------------------ 2. questions
snaps, err = api.get("/freshbench/snapshots")
if show_error(err):
    st.stop()
if not snaps:
    st.info("No Freshness Bench yet: generate one above.")
    st.stop()

st.markdown("### 2. The questions")
ids = [s["suite_id"] for s in snaps]
by_id = {s["suite_id"]: s for s in snaps}
wanted = st.session_state.get("fb_suite")
suite_id = st.selectbox(
    "Snapshot",
    ids,
    index=ids.index(wanted) if wanted in ids else 0,
    format_func=lambda i: f"{by_id[i]['name']} · {by_id[i]['questions']} questions"
    + (f" · {by_id[i]['runs']} runs" if by_id[i]["runs"] else ""),
)
st.session_state["fb_suite"] = suite_id
snap = by_id[suite_id]
st.caption(snap["description"])
cols = st.columns(len(cfg["buckets"]))
for col, b in zip(cols, cfg["buckets"], strict=True):
    col.metric(b["label"], snap["per_bucket"].get(b["key"], 0))

suite, err = api.get(f"/suites/{suite_id}")
if not show_error(err):
    with st.expander(f"Show all {snap['questions']} questions with their sources"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Event date": t["event_date"],
                        "Category": t["category"],
                        "Question": t["prompt"],
                        "Answer": t["expected"].replace("|", " / "),
                        "Source": t["source_url"],
                    }
                    for t in suite["tasks"]
                ]
            ),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Source": st.column_config.LinkColumn("Source", display_text="open"),
                "Question": st.column_config.TextColumn("Question", width="large"),
            },
        )

# ------------------------------------------------------------------------------------ 3. run
st.markdown("### 3. Run agents on it")
agents, err = api.get("/agents")
if show_error(err) or not agents:
    st.stop()
agent_by_label = {f"{a['name']}  ·  {a['model']}": a for a in agents}
pair = [lab for lab, a in agent_by_label.items() if a["name"].startswith("GPT-OSS 120B")]
with st.container(border=True):
    chosen = st.multiselect(
        "Agents",
        list(agent_by_label),
        default=pair or list(agent_by_label)[:2],
        help="Tip: the same model with and without web search separates 'knows it' from 'can look it up'.",
    )
    r1, r2 = st.columns([1, 3])
    repeats = r1.slider("Repeats", 1, 3, 1, key="fb_repeats")
    r2.caption(f"**{snap['questions'] * len(chosen) * repeats} executions** · 0% fault injection")
    if st.button("🏁 Run this bench", type="primary", disabled=not chosen):
        run, err = api.post(
            "/runs",
            json={
                "suite_id": suite_id,
                "agent_ids": [agent_by_label[c]["id"] for c in chosen],
                "repeats": repeats,
                "fault_rate": 0.0,
                "check_freshness": False,
            },
        )
        if not show_error(err):
            st.session_state["fb_run"] = run["id"]

# ------------------------------------------------------------------------------------ 4. curve
st.markdown("### 4. Knowledge cutoff")
runs, err = api.get("/runs", params={"limit": 100})
fb_runs = [r for r in runs or [] if r["suite_id"] in by_id]
if show_error(err) or not fb_runs:
    st.info("No runs of a Freshness Bench yet: start one above.")
else:
    run_ids = [r["id"] for r in fb_runs]
    wanted_run = st.session_state.get("fb_run")
    labels = {r["id"]: run_label(r) for r in fb_runs}
    run_id = st.selectbox(
        "Run",
        run_ids,
        index=run_ids.index(wanted_run) if wanted_run in run_ids else 0,
        format_func=labels.get,
    )
    st.session_state["fb_run"] = run_id
    summary = next(r for r in fb_runs if r["id"] == run_id)

    def render_curve() -> None:
        run, err = api.get(f"/runs/{run_id}")
        if show_error(err):
            return
        active = run["status"] in ("pending", "running")
        if active:
            st.progress(
                run["progress_done"] / max(run["progress_total"], 1),
                text=f"{RUN_STATUS[run['status']]}: {run['progress_done']}/{run['progress_total']} answers",
            )
        curve, err = api.get(f"/runs/{run_id}/knowledge-curve", timeout=60)
        if show_error(err) or not curve or not curve["agents"]:
            if not active:
                st.info("No answers yet.")
            return
        ids_in_run = [a["agent_id"] for a in curve["agents"]]
        colors = charts.agent_colors(ids_in_run)
        st.plotly_chart(
            charts.knowledge_curve(curve, colors),
            use_container_width=True,
            config={"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
        )
        rows = []
        for a in curve["agents"]:
            c = a["cutoff"] or {}
            newest = next((b for b in a["buckets"] if b["accuracy"] is not None), None)
            rows.append(
                {
                    "Agent": a["agent_name"],
                    "Effective knowledge cutoff": c.get("date", "")[:7]
                    if c.get("status") == "estimated"
                    else f"– ({c.get('status', 'n/a')})",
                    "95% CI": f"{c['ci'][0][:7]} – {c['ci'][1][:7]}" if c.get("ci") else "–",
                    "Accuracy, older events": pct(c.get("plateau")),
                    "Accuracy, newest events": f"{pct(newest['accuracy'])} ({newest['label']})"
                    if newest
                    else "–",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(
            "**Effective knowledge cutoff** = the event date where the agent's accuracy falls to half of its "
            "accuracy on older events (a logistic fit to every answer; interval: task-level bootstrap). "
            "*No cutoff detected* = it answers the newest events about as well as old ones (usually because "
            "it searched the web). Bars on the chart: 95% intervals."
        )
        if not active and st.session_state.get("_fb_was_active") == run_id:
            st.session_state["_fb_was_active"] = None
            st.rerun()
        if active:
            st.session_state["_fb_was_active"] = run_id

    polling = summary["status"] in ("pending", "running") or st.session_state.get("_fb_was_active") == run_id
    st.fragment(run_every=2 if polling else None)(render_curve)()

# ------------------------------------------------------------------------------------ 5. history
history, err = api.get("/freshbench/history")
if not err and history and any(len(a["points"]) for a in history["agents"]):
    st.markdown("### Over time")
    for alert in history["alerts"]:
        st.warning(f"⚠️ **Drift:** {alert['message']}")
    hist_colors = charts.agent_colors([a["agent_id"] for a in history["agents"]])
    if any(len(a["points"]) > 1 for a in history["agents"]):
        st.plotly_chart(charts.freshbench_history(history, hist_colors), use_container_width=True)
    pulse = cfg["pulse"]
    if pulse["hour_utc"] is None:
        st.caption(
            "Run a new bench every day to track drift: set `FRESHBENCH_PULSE_HOUR_UTC` (and optionally "
            "`FRESHBENCH_PULSE_AGENTS`) and AgentPulse generates and runs it automatically. See "
            "docs/freshbench.md."
        )
    else:
        st.caption(
            f"🫀 Pulse: every day after {pulse['hour_utc']:02d}:00 UTC "
            f"({'live' if pulse['live'] else 'mock'}), "
            f"agents {pulse['agent_ids']}. Last check: {pulse.get('last_check') or '–'}."
        )
