"""UI smoke tests: render real pages with Streamlit's AppTest against a RUNNING backend.

Run with the stack up (``make up``), ideally in mock mode so runs are fast::

    make ui-test

Each test fails on any uncaught exception in a page, then checks key elements.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

FRONTEND = Path(__file__).resolve().parents[1]
BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")
TIMEOUT = 180


def _backend_up() -> bool:
    try:
        return requests.get(f"{BACKEND}/health", timeout=3).ok
    except requests.RequestException:
        return False


if not _backend_up():
    pytest.fail(f"Backend not reachable at {BACKEND}. Start the stack first: make up", pytrace=False)


def app(page: str) -> AppTest:
    at = AppTest.from_file(str(FRONTEND / page), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_home_renders_with_status_sidebar():
    at = app("Home.py")
    assert at.title[0].value == "📈 AgentPulse"
    sidebar_text = " ".join(m.value for m in at.sidebar.markdown) + " ".join(
        w.value for w in [*at.sidebar.warning, *at.sidebar.info]
    )
    assert "System status" in sidebar_text
    assert "MODE" in sidebar_text


def test_agents_page_lists_every_agent():
    agents = requests.get(f"{BACKEND}/agents", timeout=10).json()
    at = app("pages/2_Agents.py")
    table = at.dataframe[0].value
    assert len(table) == len(agents)
    assert set(table["Name"]) == {a["name"] for a in agents}


def test_try_agent_shows_answer_metrics_and_trace():
    at = app("pages/2_Agents.py")
    at.radio[0].set_value("Arithmetic (calculator)").run()
    run_btn = next(b for b in at.button if b.label == "Run agent")
    run_btn.click().run()
    assert not at.exception, [e.value for e in at.exception]
    headings = [m.value for m in at.markdown]
    assert any(h.startswith("#### Answer") for h in headings)
    assert any(h == "#### Trace" for h in headings)
    assert {m.label for m in at.metric} >= {"Latency", "Tokens", "Model calls", "Tool calls"}
    assert len(at.expander) >= 2  # at least one model call + final answer


def test_create_then_delete_agent_through_form():
    name = f"UI test agent {uuid.uuid4().hex[:6]}"
    at = app("pages/2_Agents.py")
    form_inputs = [t for t in at.text_input if t.label == "Name"]
    form_inputs[0].set_value(name)
    save = next(b for b in at.button if b.label == "Save")
    save.click().run()
    assert not at.exception, [e.value for e in at.exception]

    created = [a for a in requests.get(f"{BACKEND}/agents", timeout=10).json() if a["name"] == name]
    assert len(created) == 1, "agent was not created through the form"
    requests.delete(f"{BACKEND}/agents/{created[0]['id']}", timeout=10).raise_for_status()


def test_pages_degrade_gracefully_when_backend_is_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")  # nothing listens here
    for page in ("Home.py", "pages/2_Agents.py"):
        at = AppTest.from_file(str(FRONTEND / page), default_timeout=TIMEOUT)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        errors = [e.value for e in [*at.error, *at.sidebar.error]]
        assert any("Cannot reach the backend" in e for e in errors), errors


def _wait_for_run(run_id: int, timeout_s: float = 240) -> dict:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = requests.get(f"{BACKEND}/runs/{run_id}", timeout=10).json()
        if run["status"] not in ("pending", "running"):
            return run
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not finish in {timeout_s}s")


def test_run_benchmark_page_starts_run_and_shows_leaderboard():
    at = app("pages/4_Run_Benchmark.py")
    assert any(s.label == "Task suite" for s in at.selectbox)
    agents_select = next(m for m in at.multiselect if m.label == "Agents")
    agents_select.set_value(agents_select.options[:2])
    next(s for s in at.slider if s.label == "Repeats").set_value(2)
    at.run()
    next(b for b in at.button if b.label == "Start benchmark").click().run()
    assert not at.exception, [e.value for e in at.exception]

    run_id = at.session_state["run_id"]
    # Simulate someone starting another run from a browser while this test waits.
    started = requests.get(f"{BACKEND}/runs/{run_id}", timeout=10).json()
    other = requests.post(
        f"{BACKEND}/runs",
        json={"suite_id": started["suite_id"], "agent_ids": started["agent_ids"][:1], "repeats": 1},
        timeout=10,
    ).json()["id"]
    assert other > run_id
    run = _wait_for_run(run_id)
    assert run["status"] == "done", run
    assert len(run["agent_ids"]) == 2 and run["repeats"] == 2
    _wait_for_run(other)

    # Re-render with THIS run selected. (The page defaults to the newest run; other runs may be
    # started concurrently, e.g. from a browser, so the test must not rely on "newest".)
    at = AppTest.from_file(str(FRONTEND / "pages/4_Run_Benchmark.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = run_id
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["run_id"] == run_id
    board = next(d.value for d in at.dataframe if "Reliability" in d.value.columns)
    assert {"Rank", "Agent", "Reliability", "Accuracy", "Consistency"} <= set(board.columns)
    assert len(board) == 2
    assert any("Leaderboard" in m.value for m in at.markdown)


def test_run_benchmark_page_degrades_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/4_Run_Benchmark.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Cannot reach the backend" in e.value for e in at.error)


def test_public_deployment_access_code_unlock_flow(monkeypatch):
    """Simulated public deployment: mock by default; a wrong code shows an error; the right code
    unlocks live mode and is then sent with every request."""
    import api

    sent_codes = []

    def fake_health(code=None):
        code = code or api.access_code()
        sent_codes.append(code)
        base = {"status": "ok", "version": "test", "public_deployment": True, "live_available": True}
        comps = {"database": {"ok": True, "detail": "ok"}}
        if code is None:
            return {**base, "mock_mode": True, "components": comps}, None
        if code == "c0rrect":
            return {**base, "mock_mode": False, "components": comps}, None
        return None, "Backend error 401: invalid access code"

    monkeypatch.setattr(api, "health", fake_health)
    at = AppTest.from_file(str(FRONTEND / "Home.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("MOCK MODE" in w.value for w in at.sidebar.warning)
    code_box = next(t for t in at.sidebar.text_input if t.label == "Access code")

    code_box.set_value("wrong")
    next(b for b in at.sidebar.button if b.label == "Unlock").click().run()
    assert not at.exception
    assert any("Invalid access code" in e.value for e in at.sidebar.error)
    assert "access_code" not in at.session_state or not at.session_state["access_code"]

    code_box = next(t for t in at.sidebar.text_input if t.label == "Access code")
    code_box.set_value("c0rrect")
    next(b for b in at.sidebar.button if b.label == "Unlock").click().run()
    assert not at.exception
    assert at.session_state["access_code"] == "c0rrect"
    assert any("LIVE MODE" in i.value for i in at.sidebar.info)
    assert any("unlocked" in s.value for s in at.sidebar.success)
    assert sent_codes[-1] == "c0rrect"

    next(b for b in at.sidebar.button if b.label == "Lock (back to mock)").click().run()
    assert any("MOCK MODE" in w.value for w in at.sidebar.warning)


# ------------------------------------------------------------------------------------ Phase 4 pages
@pytest.fixture(scope="module")
def finished_run():
    """A small finished mock run with 3 agents (created via the API)."""
    suites = requests.get(f"{BACKEND}/suites", timeout=10).json()
    math_suite = next(s for s in suites if s["name"] == "Math & Reasoning")
    run = requests.post(
        f"{BACKEND}/runs",
        json={"suite_id": math_suite["id"], "agent_ids": [1, 2, 4], "repeats": 2, "fault_rate": 0.3},
        timeout=10,
    ).json()
    done = _wait_for_run(run["id"])
    assert done["status"] == "done", done
    return done


def _plotly_charts(at):
    return at.get("plotly_chart")


def test_compare_page_renders_tiles_leaderboard_and_charts(finished_run):
    at = AppTest.from_file(str(FRONTEND / "pages/5_Compare.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = finished_run["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = {m.label for m in at.metric}
    assert {
        "🏆 Most reliable",
        "🎯 Most accurate",
        "⚡ Fastest (p50)",
        "💰 Cheapest per correct answer",
    } <= labels
    board = next(d.value for d in at.dataframe if "Reliability" in d.value.columns)
    assert len(board) == 3
    # overview (2) + speed & cost (4) + per task (2) — all tabs render in AppTest
    assert len(_plotly_charts(at)) >= 8
    radar_pick = next(m for m in at.multiselect if m.label.startswith("Agents on the radar"))
    assert len(radar_pick.value) == 3
    failed = [d.value for d in at.dataframe if "Trace" in d.value.columns]
    assert all(v.startswith("Traces?execution=") for df in failed for v in df["Trace"])


def test_compare_page_empty_state(monkeypatch):
    import api

    real_get = api.get
    monkeypatch.setattr(
        api, "get", lambda path, **kw: ([], None) if path == "/runs" else real_get(path, **kw)
    )
    at = AppTest.from_file(str(FRONTEND / "pages/5_Compare.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("No benchmark results yet" in i.value for i in at.info)


def test_traces_page_opens_execution_from_link(finished_run):
    execs = requests.get(f"{BACKEND}/runs/{finished_run['id']}/executions", timeout=10).json()
    target = next((e for e in execs if e["faults_injected"]), execs[0])
    at = AppTest.from_file(str(FRONTEND / "pages/6_Traces.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(f"Execution #{target['id']}" in m.value for m in at.markdown)
    assert len(_plotly_charts(at)) == 1  # timeline
    detail = requests.get(f"{BACKEND}/executions/{target['id']}", timeout=10).json()
    assert len(at.expander) >= len(detail["trace"])


def test_traces_page_browse_by_run(finished_run):
    at = AppTest.from_file(str(FRONTEND / "pages/6_Traces.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = finished_run["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert {s.label for s in at.selectbox} >= {"Run", "Agent", "Task", "Repeat"}
    assert any(m.value.startswith("### Execution #") for m in at.markdown)


def test_phase4_pages_degrade_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    for page in ("pages/5_Compare.py", "pages/6_Traces.py"):
        at = AppTest.from_file(str(FRONTEND / page), default_timeout=TIMEOUT)
        at.run()
        assert not at.exception, (page, [e.value for e in at.exception])
        assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error]), page


# ------------------------------------------------------------------------------------ phase 5: freshness
@pytest.fixture(scope="module")
def freshness_run():
    """A finished mock run of the time-sensitive suite (answers are freshness-checked)."""
    suites = requests.get(f"{BACKEND}/suites", timeout=10).json()
    current = next(s for s in suites if s["time_sensitive_count"])
    run = requests.post(
        f"{BACKEND}/runs",
        json={"suite_id": current["id"], "agent_ids": [1, 4], "repeats": 2, "fault_rate": 0.0},
        timeout=10,
    ).json()
    done = _wait_for_run(run["id"])
    assert done["status"] == "done" and done["check_freshness"] is True, done
    return done


def test_detector_checks_example_text_and_shows_verdicts():
    at = app("pages/7_Stale_Answer_Detector.py")
    example = next(s for s in at.selectbox if s.label == "Load an example")
    example.set_value("A stale answer").run()
    assert "Rishi Sunak" in at.text_area(key="fc_text").value
    next(b for b in at.button if b.label == "Check freshness").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Stale answer" in w.value for w in at.warning)
    labels = {m.label: m.value for m in at.metric}
    assert labels["Freshness"] == "0%" and labels["⚠️ Outdated"] == "2" and labels["➖ Timeless"] == "1"
    text = " ".join(m.value for m in at.markdown)
    assert "True today:" in text and "Sources** (newest first)" in text
    assert at.query_params["check"]


def test_detector_opens_a_check_from_its_link_and_lists_history():
    check = requests.post(
        f"{BACKEND}/freshness/check",
        json={"text": "Andy Burnham is the current UK Prime Minister."},
        timeout=60,
    ).json()
    at = AppTest.from_file(str(FRONTEND / "pages/7_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.query_params["check"] = str(check["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio[0].value == "History"
    assert any("Up to date" in s.value for s in at.success)
    assert next(s for s in at.selectbox if s.label == "Check").value == check["id"]


def test_detector_agent_answer_tab_shows_existing_check(freshness_run):
    execs = requests.get(f"{BACKEND}/runs/{freshness_run['id']}/executions", timeout=10).json()
    target = next(e for e in execs if e["freshness_verdict"] == "stale")
    at = AppTest.from_file(str(FRONTEND / "pages/7_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio[0].value == "An agent's answer"
    assert any("Already checked" in c.value for c in at.caption)
    assert any("Stale answer" in w.value for w in at.warning)


def test_compare_page_freshness_tab(freshness_run):
    at = AppTest.from_file(str(FRONTEND / "pages/5_Compare.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = freshness_run["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    board = next(d.value for d in at.dataframe if "Reliability" in d.value.columns)
    assert {"Freshness", "Stale answers"} <= set(board.columns)
    titles = [c.proto.spec for c in _plotly_charts(at)]
    assert any("Freshness of answers" in t for t in titles)
    flagged = next(d.value for d in at.dataframe if "Check" in d.value.columns)
    assert len(flagged) and all(v.startswith("Stale_Answer_Detector?execution=") for v in flagged["Check"])


def test_traces_page_links_to_freshness_check(freshness_run):
    execs = requests.get(f"{BACKEND}/runs/{freshness_run['id']}/executions", timeout=10).json()
    target = next(e for e in execs if e["freshness_verdict"])
    at = AppTest.from_file(str(FRONTEND / "pages/6_Traces.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.value.startswith("**Freshness:**") and "?check=" in m.value for m in at.markdown)


def test_detector_degrades_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/7_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error])


# ------------------------------------------------------------------------------------ phase 6: reports
def test_reports_page_offers_four_downloads_and_previews(freshness_run):
    at = AppTest.from_file(str(FRONTEND / "pages/10_Reports.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = freshness_run["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    buttons = at.get("download_button")
    assert len(buttons) == 4
    text = " ".join(m.value for m in at.markdown)
    assert f"# AgentPulse benchmark report: run #{freshness_run['id']}" in text
    assert "## Freshness (stale-answer detection)" in text


def test_reports_page_empty_and_backend_down(monkeypatch):
    import api

    real_get = api.get
    monkeypatch.setattr(
        api, "get", lambda path, **kw: ([], None) if path == "/runs" else real_get(path, **kw)
    )
    at = AppTest.from_file(str(FRONTEND / "pages/10_Reports.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception and any("No benchmark results yet" in i.value for i in at.info)
    monkeypatch.setattr(api, "get", real_get)
    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/10_Reports.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error])


# ------------------------------------------------------------------------------------ v2.1: suites
def _custom_suite() -> dict:
    name = f"UI suite {uuid.uuid4().hex[:6]}"
    suite = requests.post(
        f"{BACKEND}/suites",
        json={
            "name": name,
            "tasks": [
                {"key": "peru", "prompt": "Capital of Peru?", "expected": "Lima"},
                {"key": "sum", "prompt": "What is 2+3?", "expected": "5", "scorer": "numeric"},
            ],
        },
        timeout=10,
    ).json()
    return suite


def test_suites_page_builtin_is_read_only():
    builtin = next(s for s in requests.get(f"{BACKEND}/suites", timeout=10).json() if s["builtin"])
    at = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
    at.query_params["suite"] = str(builtin["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Built-in suite: read-only" in i.value for i in at.info)
    assert not any(b.label.startswith("💾 Save changes") for b in at.button)
    assert len(at.get("download_button")) == 2  # YAML + CSV export


def test_suites_page_create_blank_suite():
    name = f"Blank {uuid.uuid4().hex[:6]}"
    at = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
    at.run()
    form_name = next(t for t in at.text_input if t.label == "Name")
    form_name.set_value(name)
    next(b for b in at.button if b.label == "Create").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.value == f"### {name}" for m in at.markdown)
    assert any(b.label.startswith("💾 Save changes (0 new") for b in at.button)


def test_suites_page_custom_suite_and_scorer_preview():
    suite = _custom_suite()
    at = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
    at.query_params["suite"] = str(suite["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert {m.label: m.value for m in at.metric}["Tasks"] == "2"
    pick = next(s for s in at.selectbox if s.label == "Task")
    pick.set_value(next(o for o in pick.options if o.startswith("sum:"))).run()
    next(t for t in at.text_area if t.label == "Sample answer").set_value("The total is 5.").run()
    next(b for b in at.button if b.label == "Score it").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("✅ Pass" in s.value for s in at.success)


def test_suites_page_jury_task_in_scorer_preview():
    """Regression: picking a jury-scored task in 'Test a scorer' crashed the page (ValueError)."""
    suite = requests.post(
        f"{BACKEND}/suites",
        json={
            "name": f"UI jury {uuid.uuid4().hex[:6]}",
            "tasks": [{"key": "peru", "prompt": "Capital of Peru?", "expected": "Lima", "scorer": "jury"}],
        },
        timeout=10,
    ).json()
    at = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
    at.query_params["suite"] = str(suite["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    pick = next(s for s in at.selectbox if s.label == "Task")
    pick.set_value(next(o for o in pick.options if o.startswith("peru:"))).run()
    assert not at.exception, [e.value for e in at.exception]
    assert next(s for s in at.selectbox if s.label == "Scorer").value == "jury"
    next(t for t in at.text_area if t.label == "Sample answer").set_value("It is Lima.").run()
    next(b for b in at.button if b.label == "Score it").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("preview only" in s.value for s in at.success)


def test_run_benchmark_preselects_suite_being_edited():
    suite = _custom_suite()
    at = AppTest.from_file(str(FRONTEND / "pages/4_Run_Benchmark.py"), default_timeout=TIMEOUT)
    at.session_state["suite_id"] = suite["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert next(s for s in at.selectbox if s.label == "Task suite").value.startswith(suite["name"])


def test_suites_page_degrades_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error])


# ------------------------------------------------------------------------------------ v2.2: model connections
def _connection() -> dict:
    name = f"UI conn {uuid.uuid4().hex[:6]}"
    res = requests.post(
        f"{BACKEND}/connections",
        json={
            "name": name,
            "preset": "ollama",
            "base_url": "http://localhost:11434/v1",
            "models": ["tiny-model"],
        },
        timeout=10,
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_models_page_lists_presets_and_connections():
    conn = _connection()
    at = AppTest.from_file(str(FRONTEND / "pages/1_Models.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    preset = next(s for s in at.selectbox if s.label == "Start from")
    assert {"ollama", "lmstudio", "openai", "custom"} <= set(preset.options) or len(preset.options) >= 10
    text = " ".join(m.value for m in at.markdown)
    assert f"**{conn['name']}**" in text and f"`{conn['model_ids'][0]}`" in text
    assert "SECRET" not in text


def test_models_page_preset_prefills_base_url():
    at = AppTest.from_file(str(FRONTEND / "pages/1_Models.py"), default_timeout=TIMEOUT)
    at.run()
    next(s for s in at.selectbox if s.label == "Start from").set_value("lmstudio").run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.text_input(key="new_base_url").value == "http://localhost:1234/v1"
    assert at.text_input(key="new_name").value == "LM Studio"


def test_agents_page_offers_connection_models():
    conn = _connection()
    at = AppTest.from_file(str(FRONTEND / "pages/2_Agents.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    picker = next(s for s in at.selectbox if s.label == "Model")
    assert f"🔌 {conn['name']} · tiny-model" in picker.options  # AppTest lists display labels
    at.session_state["prefill_agent_model"] = conn["model_ids"][0]
    at.run()
    assert next(s for s in at.selectbox if s.label == "Model").value == conn["model_ids"][0]


def test_models_page_degrades_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/1_Models.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error])


def test_models_page_offers_bifrost_routing_for_native_providers():
    at = AppTest.from_file(str(FRONTEND / "pages/1_Models.py"), default_timeout=TIMEOUT)
    at.run()
    next(s for s in at.selectbox if s.label == "Start from").set_value("gemini").run()
    assert not at.exception, [e.value for e in at.exception]
    box = next(c for c in at.checkbox if c.label.startswith("🛰️ Route through the Bifrost gateway"))
    assert box.value is True
    next(s for s in at.selectbox if s.label == "Start from").set_value("groq").run()
    # only the add form's checkbox: saved Bifrost-capable connections have one in their edit form too
    assert not any(c.label == "🛰️ Route through the Bifrost gateway (recommended)" for c in at.checkbox)
    assert any("would mix the two" in c.value for c in at.caption)


# ------------------------------------------------------------------------------------ v2.4: routes everywhere
def test_agents_table_and_leaderboard_show_routes(finished_run):
    _connection()
    at = app("pages/2_Agents.py")
    table = at.dataframe[0].value
    assert "Route" in table.columns and (table["Route"] == "🛰️ Bifrost").any()
    at = AppTest.from_file(str(FRONTEND / "pages/5_Compare.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = finished_run["id"]
    at.run()
    board = next(d.value for d in at.dataframe if "Reliability" in d.value.columns)
    assert set(board["Route"]) == {"🛰️ Bifrost"}


def test_sidebar_summarises_your_models():
    _connection()
    at = app("Home.py")
    assert any(
        c.value.startswith(("⚪ your models", "🟢 your models", "🔴 your models")) for c in at.sidebar.caption
    )


def test_home_explains_every_page():
    at = app("Home.py")
    text = " ".join(m.value for m in at.markdown)
    for page in (
        "Models",
        "Agents",
        "Suites",
        "Run Benchmark",
        "Compare",
        "Traces",
        "Stale-Answer Detector",
        "Freshness Bench",
        "Freshness Guard",
        "Reports",
    ):
        assert "#### " in text and page in text, page
    assert text.count("**What it does:**") == 10 and text.count("**Why:**") == 10
    assert any("mock mode" in i.value or "live mode" in i.value for i in [*at.info, *at.success])


# ------------------------------------------------------------------------------------ v3.2: freshness bench
def _freshbench_run() -> tuple[int, int, bool]:
    """Today's mock snapshot (generated if missing) and a finished 2-agent run on it.

    Returns (suite_id, run_id, created_suite)."""
    import time

    res = requests.post(f"{BACKEND}/freshbench/generate", json={}, timeout=30).json()
    created = res["job"] is not None
    if created:
        for _ in range(240):
            job = requests.get(f"{BACKEND}/freshbench/jobs/{res['job']['id']}", timeout=10).json()
            if job["status"] != "running":
                break
            time.sleep(0.5)
        assert job["status"] == "done", job
        suite_id = job["suite_id"]
    else:
        suite_id = res["existing_suite_id"]
    run = requests.post(
        f"{BACKEND}/runs",
        json={
            "suite_id": suite_id,
            "agent_ids": [2, 4],
            "repeats": 1,
            "fault_rate": 0,
            "check_freshness": False,
        },
        timeout=30,
    ).json()
    for _ in range(600):
        status = requests.get(f"{BACKEND}/runs/{run['id']}", timeout=10).json()["status"]
        if status not in ("pending", "running"):
            break
        time.sleep(0.5)
    return suite_id, run["id"], created


def test_freshness_bench_page_generate_and_knowledge_curve():
    at = app("pages/8_Freshness_Bench.py")
    assert any(b.label == "📡 Generate today's bench" for b in at.button)
    suite_id, run_id, created = _freshbench_run()
    try:
        at = AppTest.from_file(str(FRONTEND / "pages/8_Freshness_Bench.py"), default_timeout=TIMEOUT)
        at.session_state["fb_suite"] = suite_id
        at.session_state["fb_run"] = run_id
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert {m.label for m in at.metric} >= {"≤ 7 days", "1–2 years"}
        assert any("Effective knowledge cutoff" in c.value for c in at.caption)
        assert len(at.dataframe) >= 2  # questions + cutoff table

        cmp = AppTest.from_file(str(FRONTEND / "pages/5_Compare.py"), default_timeout=TIMEOUT)
        cmp.session_state["run_id"] = run_id
        cmp.run()
        assert not cmp.exception, [e.value for e in cmp.exception]
        assert any(t.label == "📡 Knowledge cutoff" for t in cmp.tabs)

        suites = AppTest.from_file(str(FRONTEND / "pages/3_Suites.py"), default_timeout=TIMEOUT)
        suites.query_params["suite"] = str(suite_id)
        suites.run()
        assert not suites.exception, [e.value for e in suites.exception]
        assert any("Freshness Bench snapshot: read-only" in i.value for i in suites.info)
        assert not any(b.label.startswith("💾 Save changes") for b in suites.button)
    finally:
        requests.delete(f"{BACKEND}/runs/{run_id}", timeout=10)
        if created:
            requests.delete(f"{BACKEND}/suites/{suite_id}", timeout=10)


# ------------------------------------------------------------------------------------ v3.3: freshness guard
def test_freshness_guard_page_sends_a_question_through_the_guard():
    at = app("pages/9_Freshness_Guard.py")
    assert any(b.label == "🛡️ Send through the Guard" for b in at.button)
    next(r for r in at.radio if r.label == "Guard mode").set_value("block").run()
    next(b for b in at.button if b.label == "🛡️ Send through the Guard").click().run()
    assert not at.exception, [e.value for e in at.exception]
    report = [*at.success, *at.warning, *at.error, *at.info]
    assert any("freshness" in r.value or "Not checked" in r.value for r in report), [r.value for r in report]
    assert any(m.value.startswith("**What your app receives**") for m in at.markdown)


def test_guard_api_speaks_openai():
    res = requests.post(
        f"{BACKEND}/guard/v1/chat/completions",
        json={"model": "groq/openai/gpt-oss-20b", "messages": [{"role": "user", "content": "Write a haiku"}]},
        timeout=60,
    )
    body = res.json()
    assert res.ok and body["object"] == "chat.completion" and body["agentpulse"]["checked"] is False
    assert res.headers["X-AgentPulse-Action"] == "skipped"
    models = requests.get(f"{BACKEND}/guard/v1/models", timeout=30).json()
    assert models["object"] == "list" and models["data"]


def test_detector_opens_a_linked_guard_check_not_another_one():
    """Regression: a check hidden from History (e.g. Guard) linked via ?check=<id> showed another one."""
    res = requests.post(
        f"{BACKEND}/guard/v1/chat/completions",
        json={
            "model": "groq/openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "Who is the current UK prime minister?"}],
        },
        timeout=120,
    ).json()
    check_id = res["agentpulse"].get("check_id")
    assert check_id, res["agentpulse"]
    at = AppTest.from_file(str(FRONTEND / "pages/7_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.query_params["check"] = str(check_id)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert next(s for s in at.selectbox if s.label == "Check").value == check_id


def test_run_benchmark_shows_the_drill_token_and_never_the_token():
    at = app("pages/4_Run_Benchmark.py")
    captions = " ".join(c.value for c in at.caption)
    assert "Drill token" in captions and "nobody else can trigger it" in captions
    status = requests.get(f"{BACKEND}/chaos/token", timeout=10).json()
    assert {"source", "created_at", "is_default", "can_manage"} <= set(status) and "token" not in status
    assert all("agentpulse-chaos" not in str(w.value) for w in at.text_input)  # no secret in any input
