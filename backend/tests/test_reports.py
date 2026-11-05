"""Reports: the three formats, numbers identical to /runs/{id}/metrics, freshness section, edge cases."""

from __future__ import annotations

import csv
import io
import json

import pytest
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, InlineExecutor, get_bench_engine
from app.bench.pricing import PriceBook
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.db import get_sessionmaker
from app.main import app
from app.models import TaskSuite
from app.reports import _cell, to_markdown
from app.seed import seed_agents, seed_suites


@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    mock = (MockLLMClient(seed=3, latency_scale=0), MockSearchClient(latency_scale=0))
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: mock,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    app.dependency_overrides[get_bench_engine] = lambda: engine
    return client


def _run(api, db_engine, suite, agents=(1, 4), repeats=2, fault_rate=0.3):
    with Session(db_engine) as db:
        sid = db.query(TaskSuite).filter_by(name=suite).one().id
    res = api.post(
        "/runs",
        json={"suite_id": sid, "agent_ids": list(agents), "repeats": repeats, "fault_rate": fault_rate},
    )
    assert res.status_code == 202
    return res.json()["id"]


def test_json_report_matches_metrics_endpoint(api, db_engine):
    rid = _run(api, db_engine, "Current Events (time-sensitive)")
    res = api.get(f"/reports/{rid}", params={"format": "json"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/json")
    assert f'filename="agentpulse-run-{rid}.json"' in res.headers["content-disposition"]
    report = res.json()
    metrics = api.get(f"/runs/{rid}/metrics").json()
    assert report["leaderboard"] == metrics["leaderboard"]
    assert report["task_matrix"] == metrics["task_matrix"]
    assert len(report["executions"]) == 2 * 6 * 2
    assert report["failures"] == [e for e in report["executions"] if not e["passed"]]
    assert all(" · price:" not in (e["score_detail"] or "") for e in report["executions"])
    best = max(metrics["leaderboard"], key=lambda r: r["reliability_score"] or -1)
    assert report["highlights"]["most_reliable"]["agent_name"] == best["agent_name"]
    fr = report["freshness"]
    assert fr and len(fr["per_agent"]) == 2
    stale = [s for s in fr["stale_or_wrong"] if s["verdict"] == "stale"]
    assert stale and all(s["claims"] and s["claims"][0]["true_today"] for s in stale)
    json.dumps(report)  # fully serialisable


def test_markdown_report_sections_and_values(api, db_engine):
    rid = _run(api, db_engine, "Current Events (time-sensitive)")
    res = api.get(f"/reports/{rid}")
    assert res.headers["content-type"].startswith("text/markdown")
    md = res.text
    for heading in (
        f"# AgentPulse benchmark report: run #{rid}",
        "## Highlights",
        "## Leaderboard",
        "## Reliability detail",
        "## Accuracy by category",
        "## Pass rate per task",
        "## Freshness (stale-answer detection)",
        "### Stale or wrong answers",
        "## Failed executions",
    ):
        assert heading in md, heading
    board = api.get(f"/runs/{rid}/metrics").json()["leaderboard"]
    for r in board:
        assert r["agent_name"] in md
        assert f"{r['accuracy'] * 100:.0f}%" in md
    assert "mock corpus" in md and "True today:" in md and "uk-prime-minister ⏰" in md


def test_csv_reports(api, db_engine):
    rid = _run(api, db_engine, "Math & Reasoning")
    lb = list(csv.DictReader(io.StringIO(api.get(f"/reports/{rid}", params={"format": "csv"}).text)))
    board = api.get(f"/runs/{rid}/metrics").json()["leaderboard"]
    assert [row["Agent"] for row in lb] == [r["agent_name"] for r in board]
    assert float(lb[0]["Accuracy"]) == pytest.approx(board[0]["accuracy"])
    assert lb[0]["Freshness"] == ""  # no time-sensitive tasks
    res = api.get(f"/reports/{rid}", params={"format": "csv", "table": "executions"})
    assert f'agentpulse-run-{rid}-executions.csv"' in res.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(res.text)))
    assert len(rows) == 2 * 8 * 2 and {"agent_name", "task_key", "passed", "cost_usd"} <= set(rows[0])


def test_markdown_without_freshness_or_failures(api, db_engine):
    rid = _run(api, db_engine, "Factual QA", agents=(2,), repeats=1, fault_rate=0)
    md = api.get(f"/reports/{rid}", params={"download": False})
    assert md.headers["content-disposition"].startswith("inline")
    assert "## Freshness" not in md.text


def test_report_errors(api):
    assert api.get("/reports/999").status_code == 404
    assert api.get("/reports/1", params={"format": "pdf"}).status_code == 422


def test_cell_escapes_pipes_newlines_and_truncates():
    assert _cell("a | b\nc") == r"a \| b c"
    assert _cell("x" * 200, 10) == "x" * 9 + "…"
    assert _cell(None) == ""


def test_partial_run_is_flagged():
    report = {
        "run": {
            "id": 1,
            "suite_name": "S",
            "mock_mode": True,
            "status": "running",
            "agent_ids": [1],
            "repeats": 1,
            "fault_rate": 0,
            "progress_done": 0,
            "progress_total": 1,
            "started_at": None,
            "duration_s": None,
        },
        "generated_at": "2026-11-05T10:00:00+00:00",
        "agentpulse_version": "x",
        "weights": {},
        "highlights": {
            k: None
            for k in (
                "most_reliable",
                "most_accurate",
                "fastest_p50_ms",
                "cheapest_per_correct_usd",
                "freshest",
            )
        },
        "leaderboard": [],
        "task_matrix": [],
        "freshness": None,
        "failures": [],
        "executions": [],
    }
    md = to_markdown(report)
    assert "figures are partial" in md and "Every execution passed." in md
