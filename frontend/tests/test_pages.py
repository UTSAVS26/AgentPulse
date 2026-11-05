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
