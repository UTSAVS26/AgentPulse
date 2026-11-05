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
    at = app("app.py")
    assert at.title[0].value == "📈 AgentPulse"
    sidebar_text = " ".join(m.value for m in at.sidebar.markdown) + " ".join(
        w.value for w in [*at.sidebar.warning, *at.sidebar.info]
    )
    assert "System status" in sidebar_text
    assert "MODE" in sidebar_text


def test_agents_page_lists_every_agent():
    agents = requests.get(f"{BACKEND}/agents", timeout=10).json()
    at = app("pages/1_Agents.py")
    table = at.dataframe[0].value
    assert len(table) == len(agents)
    assert set(table["Name"]) == {a["name"] for a in agents}


def test_try_agent_shows_answer_metrics_and_trace():
    at = app("pages/1_Agents.py")
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
    at = app("pages/1_Agents.py")
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
    for page in ("app.py", "pages/1_Agents.py"):
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
    at = app("pages/2_Run_Benchmark.py")
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
    at = AppTest.from_file(str(FRONTEND / "pages/2_Run_Benchmark.py"), default_timeout=TIMEOUT)
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
    at = AppTest.from_file(str(FRONTEND / "pages/2_Run_Benchmark.py"), default_timeout=TIMEOUT)
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
    at = AppTest.from_file(str(FRONTEND / "app.py"), default_timeout=TIMEOUT)
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
    at = AppTest.from_file(str(FRONTEND / "pages/3_Compare.py"), default_timeout=TIMEOUT)
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
    at = AppTest.from_file(str(FRONTEND / "pages/3_Compare.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception
    assert any("No benchmark results yet" in i.value for i in at.info)


def test_traces_page_opens_execution_from_link(finished_run):
    execs = requests.get(f"{BACKEND}/runs/{finished_run['id']}/executions", timeout=10).json()
    target = next((e for e in execs if e["faults_injected"]), execs[0])
    at = AppTest.from_file(str(FRONTEND / "pages/4_Traces.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(f"Execution #{target['id']}" in m.value for m in at.markdown)
    assert len(_plotly_charts(at)) == 1  # timeline
    detail = requests.get(f"{BACKEND}/executions/{target['id']}", timeout=10).json()
    assert len(at.expander) >= len(detail["trace"])


def test_traces_page_browse_by_run(finished_run):
    at = AppTest.from_file(str(FRONTEND / "pages/4_Traces.py"), default_timeout=TIMEOUT)
    at.session_state["run_id"] = finished_run["id"]
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert {s.label for s in at.selectbox} >= {"Run", "Agent", "Task", "Repeat"}
    assert any(m.value.startswith("### Execution #") for m in at.markdown)


def test_phase4_pages_degrade_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    for page in ("pages/3_Compare.py", "pages/4_Traces.py"):
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
    at = app("pages/5_Stale_Answer_Detector.py")
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
    at = AppTest.from_file(str(FRONTEND / "pages/5_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.query_params["check"] = str(check["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio[0].value == "History"
    assert any("Up to date" in s.value for s in at.success)
    assert next(s for s in at.selectbox if s.label == "Check").value == check["id"]


def test_detector_agent_answer_tab_shows_existing_check(freshness_run):
    execs = requests.get(f"{BACKEND}/runs/{freshness_run['id']}/executions", timeout=10).json()
    target = next(e for e in execs if e["freshness_verdict"] == "stale")
    at = AppTest.from_file(str(FRONTEND / "pages/5_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio[0].value == "An agent's answer"
    assert any("Already checked" in c.value for c in at.caption)
    assert any("Stale answer" in w.value for w in at.warning)


def test_compare_page_freshness_tab(freshness_run):
    at = AppTest.from_file(str(FRONTEND / "pages/3_Compare.py"), default_timeout=TIMEOUT)
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
    at = AppTest.from_file(str(FRONTEND / "pages/4_Traces.py"), default_timeout=TIMEOUT)
    at.query_params["execution"] = str(target["id"])
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(m.value.startswith("**Freshness:**") and "?check=" in m.value for m in at.markdown)


def test_detector_degrades_when_backend_down(monkeypatch):
    import api

    monkeypatch.setattr(api, "BACKEND_URL", "http://127.0.0.1:9")
    at = AppTest.from_file(str(FRONTEND / "pages/5_Stale_Answer_Detector.py"), default_timeout=TIMEOUT)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Cannot reach the backend" in e.value for e in [*at.error, *at.sidebar.error])
