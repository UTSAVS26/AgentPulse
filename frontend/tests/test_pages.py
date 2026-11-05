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
