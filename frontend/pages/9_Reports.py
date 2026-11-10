"""Reports: download a benchmark run as Markdown, CSV or JSON, with an inline preview."""

from __future__ import annotations

import streamlit as st

import api
from ui import page_setup, preview_markdown, run_label, show_error

page_setup("Reports", "📄")
st.title("📄 Reports")
st.caption(
    "Export a benchmark run: **Markdown** to read and share, **CSV** to analyse in a spreadsheet, "
    "**JSON** for everything (every execution included). The numbers are identical to the Compare page."
)

runs, err = api.get("/runs", params={"limit": 50})
if show_error(err):
    st.stop()
runs = [r for r in runs or [] if r["progress_done"] > 0]
if not runs:
    st.info("No benchmark results yet. Start a run on the **Run Benchmark** page.")
    st.stop()

run_ids = [r["id"] for r in runs]
labels = {r["id"]: run_label(r) for r in runs}
current = st.session_state.get("run_id")
run_id = st.selectbox(
    "Run", run_ids, index=run_ids.index(current) if current in run_ids else 0, format_func=lambda i: labels[i]
)
st.session_state["run_id"] = run_id
if next(r for r in runs if r["id"] == run_id)["status"] in ("pending", "running"):
    st.warning("This run is still in progress: the report will contain partial figures.")

FORMATS = [
    ("📝 Markdown report", {"format": "md"}, f"agentpulse-run-{run_id}.md", "text/markdown"),
    ("📊 Leaderboard (CSV)", {"format": "csv"}, f"agentpulse-run-{run_id}-leaderboard.csv", "text/csv"),
    (
        "🧾 All executions (CSV)",
        {"format": "csv", "table": "executions"},
        f"agentpulse-run-{run_id}-executions.csv",
        "text/csv",
    ),
    ("🗂️ Full data (JSON)", {"format": "json"}, f"agentpulse-run-{run_id}.json", "application/json"),
]

cols = st.columns(len(FORMATS))
preview: str | None = None
for col, (label, params, filename, mime) in zip(cols, FORMATS, strict=True):
    body, err = api.download(f"/reports/{run_id}", params=params)
    if err:
        col.error(err)
        continue
    col.download_button(label, body, file_name=filename, mime=mime, use_container_width=True)
    if params["format"] == "md":
        preview = body.decode("utf-8")

st.caption(
    f"API: `GET /reports/{run_id}?format=md|csv|json` (CSV also takes `table=leaderboard|executions`)."
)
if preview:
    st.markdown("#### Preview")
    st.caption(
        "How the Markdown file reads. Wide tables scroll sideways; download the file for the full report."
    )
    # Compact, scrollable tables: the report's tables are wider than the page.
    st.markdown(
        """<style>
        .st-key-report_preview table {
            display: block; overflow-x: auto; max-width: 100%; font-size: 0.85rem;
        }
        .st-key-report_preview th, .st-key-report_preview td { white-space: nowrap; padding: 0.35rem 0.7rem; }
        .st-key-report_preview code { white-space: nowrap; font-size: 0.8rem; }
        </style>""",
        unsafe_allow_html=True,
    )
    with st.container(border=True, height=720, key="report_preview"):
        st.markdown(preview_markdown(preview))
