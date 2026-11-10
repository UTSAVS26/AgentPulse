"""Suites: build your own benchmarks. Create, edit, import/export suites and their tasks; test scorers."""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

import api
from suite_edit import COLUMNS, diff_tasks
from ui import nav_link, page_setup, show_error

page_setup("Suites", "🧪")
st.title("🧪 Benchmark suites")
st.caption(
    "A suite is a list of tasks: a prompt, the expected answer and how to score it. Built-in suites are "
    "read-only (duplicate one to change it); your own suites can be edited freely until a run uses them."
)

SCORERS = ["contains", "exact", "numeric", "regex", "judge", "jury"]
SCORER_HELP = (
    "contains: expected text appears (use | for alternatives) · exact: whole answer matches · "
    "numeric: a number in the final sentence within ± tolerance · regex: pattern matches · "
    "judge: an LLM grades the answer · jury: three judge models from different labs vote"
)


def _select(suite_id: int | None) -> None:
    st.session_state["suite_id"] = suite_id


suites, err = api.get("/suites")
if show_error(err):
    st.stop()
suites = suites or []

# ------------------------------------------------------------------------------------ create / import
flash = st.session_state.pop("suites_flash", None)
if flash:
    st.success(flash[0])
    for w in flash[1]:
        st.warning(w)
with st.expander("➕ Create a suite", expanded=not any(not s["builtin"] for s in suites)):
    blank, upload = st.tabs(["Blank suite", "Import a file (YAML or CSV)"])
    with blank:
        with st.form("new_suite", clear_on_submit=True):
            name = st.text_input("Name", max_chars=120)
            description = st.text_area("Description", height=70)
            if st.form_submit_button("Create", type="primary"):
                created, err = api.post("/suites", json={"name": name, "description": description})
                if not show_error(err):
                    _select(created["id"])
                    st.rerun()
    with upload:
        st.caption(
            "**YAML**: the same format as the built-in suites (`name`, `description`, `tasks`). "
            f"**CSV**: one row per task with columns `{', '.join(COLUMNS)}`; only `key` and `prompt` "
            "are required. Export any suite below to get a template."
        )
        file = st.file_uploader("Suite file", type=["yaml", "yml", "csv"])
        import_name = st.text_input(
            "Suite name", help="Required for CSV; overrides the name inside a YAML file."
        )
        is_csv = file is not None and file.name.lower().endswith(".csv")
        if is_csv and not import_name.strip():
            st.caption("A CSV file has no suite name inside: type one in **Suite name** above.")
        if st.button("Import", disabled=file is None or (is_csv and not import_name.strip())):
            fmt = "csv" if is_csv else "yaml"
            params = {"format": fmt, **({"name": import_name} if import_name.strip() else {})}
            result, err = api.post_text("/suites/import", file.getvalue().decode("utf-8", "replace"), params)
            if not show_error(err):
                _select(result["suite"]["id"])
                # shown after the rerun (a message written before st.rerun() is never seen)
                st.session_state["suites_flash"] = (
                    f"Imported {result['suite']['task_count']} tasks into '{result['suite']['name']}'.",
                    result["warnings"],
                )
                st.rerun()

if not suites:
    st.info("No suites yet. Create one above.")
    st.stop()

# ------------------------------------------------------------------------------------ pick a suite
ids = [s["id"] for s in suites]
by_id = {s["id"]: s for s in suites}
linked = st.query_params.get("suite")
current = int(linked) if linked and linked.isdigit() else st.session_state.get("suite_id")
suite_id = st.selectbox(
    "Suite",
    ids,
    index=ids.index(current) if current in ids else 0,
    format_func=lambda i: (
        f"{'🔒 ' if by_id[i]['builtin'] else '✏️ '}{by_id[i]['name']} · {by_id[i]['task_count']} tasks"
        + (f" · {by_id[i]['time_sensitive_count']} ⏰" if by_id[i]["time_sensitive_count"] else "")
        + (f" · {by_id[i]['runs']} runs" if by_id[i]["runs"] else "")
    ),
)
_select(suite_id)
suite, err = api.get(f"/suites/{suite_id}")
if show_error(err):
    st.stop()
builtin = suite["builtin"]

c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
c1.markdown(f"### {suite['name']}")
if suite["description"]:
    c1.caption(suite["description"])
c2.metric("Tasks", suite["task_count"])
c3.metric("Time-sensitive", suite["time_sensitive_count"])
c4.metric("Runs", suite["runs"])

if builtin:
    st.info("🔒 Built-in suite: read-only. Duplicate it to get an editable copy.")
a1, a2, a3, a4 = st.columns(4)
if a1.button("📄 Duplicate", help="Copy this suite (and its tasks) into a new editable suite."):
    copy, err = api.post(f"/suites/{suite_id}/duplicate")
    if not show_error(err):
        _select(copy["id"])
        st.rerun()
for col, fmt in ((a2, "yaml"), (a3, "csv")):
    body, err = api.download(f"/suites/{suite_id}/export", params={"format": fmt})
    if body is not None:
        col.download_button(
            f"⬇️ Export {fmt.upper()}",
            body,
            file_name=f"{re.sub(r'[^a-z0-9]+', '-', suite['name'].lower()).strip('-') or 'suite'}.{fmt}",
            mime="text/csv" if fmt == "csv" else "application/yaml",
        )
nav_link(a4, "pages/4_Run_Benchmark.py", "Run this suite", "🏁")

# ------------------------------------------------------------------------------------ settings
if not builtin:
    with st.expander("⚙️ Suite settings"):
        with st.form("suite_settings"):
            new_name = st.text_input("Name", value=suite["name"], max_chars=120)
            new_desc = st.text_area("Description", value=suite["description"], height=70)
            if st.form_submit_button("Save"):
                _, err = api.put(f"/suites/{suite_id}", json={"name": new_name, "description": new_desc})
                if not show_error(err):
                    st.rerun()
        if suite["runs"]:
            st.caption(
                f"This suite has {suite['runs']} runs, so it can't be deleted (delete the runs first)."
            )
        else:
            confirm = st.checkbox("I want to delete this suite and its tasks")
            if st.button("Delete suite", disabled=not confirm):
                _, err = api.delete(f"/suites/{suite_id}")
                if not show_error(err):
                    _select(None)
                    st.rerun()

# ------------------------------------------------------------------------------------ tasks
st.markdown("#### Tasks")
original = pd.DataFrame(
    [{"id": t["id"], **{c: t[c] for c in COLUMNS}, "used": t["executions"]} for t in suite["tasks"]],
    columns=["id", *COLUMNS, "used"],
)
column_config = {
    "id": None,  # hidden
    "key": st.column_config.TextColumn(
        "Key", help="Unique id: lowercase letters, digits, - or _", required=True
    ),
    "prompt": st.column_config.TextColumn("Prompt", width="large", required=True),
    "expected": st.column_config.TextColumn("Expected", help="Use | to separate acceptable alternatives"),
    "scorer": st.column_config.SelectboxColumn(
        "Scorer", options=SCORERS, help=SCORER_HELP, default="contains"
    ),
    "tolerance": st.column_config.NumberColumn(
        "± Tol.", min_value=0.0, default=0.0, help="numeric scorer only"
    ),
    "category": st.column_config.TextColumn("Category", default="general"),
    "time_sensitive": st.column_config.CheckboxColumn("⏰", help="Answer changes over time", default=False),
    "stale_answer": st.column_config.TextColumn("Stale answer", help="What an outdated model would say"),
    "used": st.column_config.NumberColumn("Used", help="Executions that used this task (can't be deleted)"),
}
edited = st.data_editor(
    original,
    key=f"tasks_{suite_id}",
    column_config=column_config,
    disabled=True if builtin else ["used"],
    num_rows="fixed" if builtin else "dynamic",
    hide_index=True,
    use_container_width=True,
)
no_expected = [t["key"] for t in suite["tasks"] if not t["expected"].strip()]
if no_expected:
    st.warning(
        f"No expected answer for: {', '.join(no_expected)}. These tasks can't pass until you add one "
        "(for `judge`, describe the reference answer)."
    )
st.caption(
    "Placeholders in *Expected*: `{{current_year}}`, `{{today_iso}}`, `{{today_weekday}}`. " + SCORER_HELP
)


if not builtin:
    creates, updates, deletes = diff_tasks(original, edited)
    pending = len(creates) + len(updates) + len(deletes)
    label = f"💾 Save changes ({len(creates)} new · {len(updates)} edited · {len(deletes)} deleted)"
    if st.button(label, type="primary", disabled=not pending):
        errors = []
        for tid in deletes:
            _, err = api.delete(f"/suites/{suite_id}/tasks/{tid}")
            if err:
                errors.append(err)
        for tid, data in updates:
            _, err = api.put(f"/suites/{suite_id}/tasks/{tid}", json=data)
            if err:
                errors.append(f"{data['key'] or tid}: {err}")
        for data in creates:
            _, err = api.post(f"/suites/{suite_id}/tasks", json=data)
            if err:
                errors.append(f"{data['key'] or '(new row)'}: {err}")
        if errors:
            for e in errors:
                st.error(e)
            st.caption("Rows without errors were saved. Fix the rows above and save again.")
        else:
            st.session_state.pop(f"tasks_{suite_id}", None)
            st.rerun()

# ------------------------------------------------------------------------------------ scorer preview
with st.expander("🧪 Test a scorer"):
    st.caption("Check how a task scores a sample answer before you run a benchmark. No model is called.")
    options = {f"{t['key']}: {t['prompt'][:60]}": t for t in suite["tasks"]}
    pick = st.selectbox("Task", ["(custom)", *options])
    base = options.get(pick, {"scorer": "contains", "expected": "", "tolerance": 0.0})
    p1, p2, p3 = st.columns([1, 2, 1])
    scorer = p1.selectbox(
        "Scorer",
        SCORERS,
        index=SCORERS.index(base["scorer"]) if base["scorer"] in SCORERS else 0,
        key=f"pv_scorer_{pick}",
    )
    expected = p2.text_input("Expected", value=base["expected"], key=f"pv_exp_{pick}")
    tolerance = p3.number_input(
        "Tolerance", min_value=0.0, value=float(base["tolerance"]), key=f"pv_tol_{pick}"
    )
    output = st.text_area("Sample answer", placeholder="The answer is …", height=80)
    if st.button("Score it", disabled=not output.strip()):
        res, err = api.post(
            "/scoring/preview",
            json={"output": output, "scorer": scorer, "expected": expected, "tolerance": tolerance},
        )
        if not show_error(err):
            (st.success if res["passed"] else st.error)(
                f"{'✅ Pass' if res['passed'] else '❌ Fail'} (score {res['score']:.2f}): {res['detail']}"
            )
            if res["expected_rendered"] != expected:
                st.caption(f"Expected after placeholders: `{res['expected_rendered']}`")
