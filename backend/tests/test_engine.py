"""Benchmark engine end-to-end in mock mode, plus suites/runs API, pricing and migrations."""

import math
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.bench.engine import (
    BenchmarkEngine,
    InlineExecutor,
    RunValidationError,
    get_bench_engine,
    mark_interrupted_runs,
)
from app.bench.pricing import PriceBook
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.db import add_missing_columns, get_sessionmaker
from app.main import app
from app.models import Agent, BenchmarkRun, TaskExecution, TaskSuite
from app.seed import seed_agents, seed_suites

SETTINGS = Settings(mock_mode=True, mock_latency_scale=0)


@pytest.fixture()
def seeded(db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    return db_engine


@pytest.fixture()
def engine(seeded):
    mock = (MockLLMClient(seed=7, latency_scale=0), MockSearchClient(latency_scale=0))
    return BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=SETTINGS,
        clients=lambda live: mock,  # tests never call real providers; live runs use the same fakes
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )


@pytest.fixture()
def api(client, engine):
    app.dependency_overrides[get_bench_engine] = lambda: engine
    return client


def suite_id(db_engine, name):
    with Session(db_engine) as db:
        return db.query(TaskSuite).filter_by(name=name).one().id


# ------------------------------------------------------------------------------------ seeding
def test_seed_suites_creates_three_suites_and_is_idempotent(db_engine):
    with Session(db_engine) as db:
        assert seed_suites(db) == 22
        assert seed_suites(db) == 0
        names = sorted(s.name for s in db.query(TaskSuite))
        assert names == ["Current Events (time-sensitive)", "Factual QA", "Math & Reasoning"]
        ce = db.query(TaskSuite).filter_by(name="Current Events (time-sensitive)").one()
        assert all(t.time_sensitive for t in ce.tasks)
        uk = next(t for t in ce.tasks if t.key == "uk-prime-minister")
        assert (uk.expected, uk.stale_answer) == ("Burnham", "Keir Starmer")


def test_seed_suites_updates_changed_expected_answer(db_engine, tmp_path: Path):
    (tmp_path / "s.yaml").write_text(
        "name: T\ntasks:\n  - key: pm\n    prompt: Who is PM?\n    expected: Old\n", encoding="utf-8"
    )
    with Session(db_engine) as db:
        assert seed_suites(db, tmp_path) == 1
        (tmp_path / "s.yaml").write_text(
            "name: T\ntasks:\n  - key: pm\n    prompt: Who is PM?\n    expected: New\n", encoding="utf-8"
        )
        assert seed_suites(db, tmp_path) == 1
        assert db.query(TaskSuite).filter_by(name="T").one().tasks[0].expected == "New"


# ---------------------------------------------------------------- full mock run (phase gate)
def test_full_mock_benchmark_3_agents_3_repeats_20pct_faults(engine, seeded):
    sid = suite_id(seeded, "Math & Reasoning")
    with Session(seeded) as db:
        agent_ids = [a.id for a in db.query(Agent).order_by(Agent.id).limit(3)]
        run = engine.create_run(db, suite_id=sid, agent_ids=agent_ids, repeats=3, fault_rate=0.2)
        assert run.progress_total == 8 * 3 * 3

    engine.start(run.id)  # InlineExecutor: runs synchronously

    with Session(seeded) as db:
        run = db.get(BenchmarkRun, run.id)
        assert run.status == "done" and run.error is None
        assert run.progress_done == run.progress_total == 72
        execs = db.query(TaskExecution).filter_by(run_id=run.id).all()
        assert len(execs) == 72
        assert all(e.score is not None and e.passed is not None and e.score_detail for e in execs)
        assert sum(e.faults_injected for e in execs) > 0, "20% fault rate should inject some faults"
        assert all(
            e.recovered == bool(e.passed and (e.faults_injected or e.retries or e.tool_errors)) for e in execs
        )
        assert any(e.cost_usd > 0 for e in execs)  # snapshot prices for groq models
        assert len({(e.agent_id, e.task_id, e.repeat_idx) for e in execs}) == 72


def test_metrics_endpoint_after_mock_run_is_sane(api, seeded):
    sid = suite_id(seeded, "Math & Reasoning")
    run = api.post("/runs", json={"suite_id": sid, "agent_ids": [1, 2, 4], "repeats": 3, "fault_rate": 0.2})
    assert run.status_code == 202, run.text
    rid = run.json()["id"]
    assert api.get(f"/runs/{rid}").json()["status"] == "done"

    m = api.get(f"/runs/{rid}/metrics").json()
    board = m["leaderboard"]
    assert len(board) == 3 and m["total_executions"] == 72
    for row in board:
        for key in (
            "accuracy",
            "consistency",
            "error_rate",
            "reliability_score",
            "latency_ms_p50",
            "latency_ms_p95",
        ):
            v = row[key]
            assert v is not None and not math.isnan(v), (row["agent_name"], key)
            if key not in ("latency_ms_p50", "latency_ms_p95"):
                assert 0.0 <= v <= 1.0
        assert row["latency_ms_p95"] >= row["latency_ms_p50"]
    scores = [r["reliability_score"] for r in board]
    assert scores == sorted(scores, reverse=True)

    execs = api.get(f"/runs/{rid}/executions").json()
    assert len(execs) == 72 and {e["task_key"] for e in execs} >= {"arith-basic", "compound-interest"}
    detail = api.get(f"/executions/{execs[0]['id']}").json()
    assert detail["trace"]


def test_time_sensitive_suite_shows_stale_answers_without_tools(engine, seeded):
    sid = suite_id(seeded, "Current Events (time-sensitive)")
    with Session(seeded) as db:
        tools_agent = db.query(Agent).filter_by(name="GPT-OSS 120B · tools").one().id
        baseline = db.query(Agent).filter_by(name="GPT-OSS 120B · no tools (baseline)").one().id
        run = engine.create_run(db, suite_id=sid, agent_ids=[tools_agent, baseline], repeats=3, fault_rate=0)
    engine.start(run.id)
    with Session(seeded) as db:
        execs = db.query(TaskExecution).filter_by(run_id=run.id).all()
        acc = {
            a: sum(e.passed for e in execs if e.agent_id == a) / sum(1 for e in execs if e.agent_id == a)
            for a in (tools_agent, baseline)
        }
        assert acc[tools_agent] > acc[baseline]
        stale = [e for e in execs if e.agent_id == baseline and "Starmer" in e.output]
        assert stale, "baseline should give the stale answer for the UK PM at least once"


# ------------------------------------------------------------------------------------ validation / lifecycle
def test_create_run_validation(engine, seeded):
    sid = suite_id(seeded, "Factual QA")
    with Session(seeded) as db:
        with pytest.raises(RunValidationError, match="suite 999"):
            engine.create_run(db, suite_id=999, agent_ids=[1], repeats=1, fault_rate=0)
        with pytest.raises(RunValidationError, match="agents not found"):
            engine.create_run(db, suite_id=sid, agent_ids=[1, 999], repeats=1, fault_rate=0)
        with pytest.raises(RunValidationError, match="too large"):
            engine.create_run(db, suite_id=sid, agent_ids=[1], repeats=300, fault_rate=0)


def test_runs_api_validation_errors(api, seeded):
    assert api.post("/runs", json={"suite_id": 999, "agent_ids": [1]}).status_code == 422
    assert api.post("/runs", json={"suite_id": 1, "agent_ids": []}).status_code == 422
    assert api.post("/runs", json={"suite_id": 1, "agent_ids": [1], "repeats": 0}).status_code == 422
    assert api.post("/runs", json={"suite_id": 1, "agent_ids": [1], "fault_rate": 1.5}).status_code == 422
    assert api.get("/runs/999").status_code == 404
    assert api.get("/runs/999/metrics").status_code == 404


def test_cancel_pending_run_and_delete(api, engine, seeded):
    with Session(seeded) as db:
        run = engine.create_run(db, suite_id=1, agent_ids=[1], repeats=1, fault_rate=0)
    cancelled = api.post(f"/runs/{run.id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    engine.execute(run.id)  # must be a no-op for a cancelled run
    with Session(seeded) as db:
        assert db.query(TaskExecution).filter_by(run_id=run.id).count() == 0
    assert api.delete(f"/runs/{run.id}").status_code == 204
    assert api.get(f"/runs/{run.id}").status_code == 404


def test_cannot_delete_running_run(api, engine, seeded):
    with Session(seeded) as db:
        rid = engine.create_run(db, suite_id=1, agent_ids=[1], repeats=1, fault_rate=0).id
        db.get(BenchmarkRun, rid).status = "running"
        db.commit()
    assert api.delete(f"/runs/{rid}").status_code == 409


def test_interrupted_runs_are_marked_failed_on_startup(engine, seeded):
    with Session(seeded) as db:
        run = engine.create_run(db, suite_id=1, agent_ids=[1], repeats=1, fault_rate=0)
        db.get(BenchmarkRun, run.id).status = "running"
        db.commit()
        assert mark_interrupted_runs(db) == 1
        assert db.get(BenchmarkRun, run.id).status == "failed"
        assert "interrupted" in db.get(BenchmarkRun, run.id).error


def test_execution_crash_is_recorded_and_run_continues(engine, seeded, monkeypatch):
    calls = {"n": 0}
    import app.bench.engine as eng

    real = eng.run_agent

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(eng, "run_agent", flaky)
    with Session(seeded) as db:
        run = engine.create_run(
            db, suite_id=suite_id(seeded, "Factual QA"), agent_ids=[1], repeats=1, fault_rate=0
        )
    engine.start(run.id)
    with Session(seeded) as db:
        execs = db.query(TaskExecution).filter_by(run_id=run.id).all()
        assert len(execs) == 8 and db.get(BenchmarkRun, run.id).status == "done"
        crashed = [e for e in execs if e.error and "internal error" in e.error]
        assert len(crashed) == 1 and crashed[0].passed is False


# ------------------------------------------------------------------------------------ suites API
def test_suites_api(api, seeded):
    suites = api.get("/suites").json()
    assert {s["name"] for s in suites} == {
        "Factual QA",
        "Math & Reasoning",
        "Current Events (time-sensitive)",
    }
    detail = api.get(f"/suites/{suites[0]['id']}").json()
    assert detail["task_count"] == len(detail["tasks"]) > 0

    body = {"name": "Custom", "tasks": [{"key": "a", "prompt": "1+1?", "expected": "2", "scorer": "numeric"}]}
    created = api.post("/suites", json=body)
    assert created.status_code == 201 and created.json()["task_count"] == 1
    assert api.post("/suites", json=body).status_code == 409
    bad = {"name": "Bad", "tasks": [{"key": "a", "prompt": "x", "scorer": "vibes"}]}
    assert api.post("/suites", json=bad).status_code == 422
    dup = {"name": "Dup", "tasks": [{"key": "a", "prompt": "x"}, {"key": "a", "prompt": "y"}]}
    assert api.post("/suites", json=dup).status_code == 422
    assert api.delete(f"/suites/{created.json()['id']}").status_code == 204


# ------------------------------------------------------------------------------------ pricing
def test_pricebook_uses_bifrost_catalogue_then_fallbacks(monkeypatch):
    catalogue = {"data": [{"id": "groq/x", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json=catalogue, request=httpx.Request("GET", "http://b")),
    )
    book = PriceBook("http://bifrost:8080")
    assert book.cost("groq/x", 1000, 500) == (pytest.approx(0.002), "bifrost")
    assert book.cost("ollama/granite4:3b", 10_000, 10_000) == (0.0, "local")
    assert book.price("groq/openai/gpt-oss-20b").source == "snapshot"
    assert book.price("openrouter/some/model:free").source == "free"
    assert book.price("mystery/model").source == "unknown"


def test_pricebook_survives_gateway_failure(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", down)
    cost, source = PriceBook("http://bifrost:8080").cost("groq/openai/gpt-oss-20b", 1_000_000, 0)
    assert (cost, source) == (pytest.approx(0.075), "snapshot")


# ------------------------------------------------------------------------------------ migration helper
def test_add_missing_columns_upgrades_old_schema(tmp_path):
    old = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with old.begin() as conn:  # an old "tasks" table without stale_answer
        conn.execute(text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, suite_id INTEGER, key VARCHAR(120))"))
    added = add_missing_columns(old)
    assert "tasks.stale_answer" in added
    assert "stale_answer" in {c["name"] for c in inspect(old).get_columns("tasks")}
    assert add_missing_columns(old) == []  # idempotent
    old.dispose()


def _catalogue_response(price="0.000001"):
    body = {"data": [{"id": "groq/x", "pricing": {"prompt": price, "completion": "0.000002"}}]}
    return httpx.Response(200, json=body, request=httpx.Request("GET", "http://b"))


def test_pricebook_loads_on_freshly_booted_machine(monkeypatch):
    """Regression: time.monotonic() starts near 0 after boot (Linux CI runners). The first lookup
    must still fetch the catalogue."""
    import app.bench.pricing as pricing

    monkeypatch.setattr(pricing.time, "monotonic", lambda: 5.0)  # 5 s after boot
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _catalogue_response())
    assert PriceBook("http://bifrost:8080").price("groq/x").source == "bifrost"


def test_pricebook_caches_then_refreshes_after_ttl_and_retries_soon_after_failure(monkeypatch):
    import app.bench.pricing as pricing

    clock = {"t": 100.0}
    calls = {"n": 0}
    responses = []

    def fake_get(*a, **k):
        calls["n"] += 1
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(pricing.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(httpx, "get", fake_get)
    book = PriceBook("http://bifrost:8080", ttl_s=3600)

    responses.append(httpx.ConnectError("bifrost starting"))
    assert book.price("groq/x").source == "unknown"  # failed load -> fallback
    clock["t"] += 30
    book.price("groq/x")
    assert calls["n"] == 1  # no hammering within the retry window

    clock["t"] += 31  # > 60 s after the failure
    responses.append(_catalogue_response("0.000001"))
    assert book.price("groq/x").input_per_token == pytest.approx(1e-6)
    assert calls["n"] == 2

    clock["t"] += 3599
    book.price("groq/x")
    assert calls["n"] == 2  # cached within TTL

    clock["t"] += 2
    responses.append(_catalogue_response("0.000005"))
    assert book.price("groq/x").input_per_token == pytest.approx(5e-6)  # refreshed after TTL
    assert calls["n"] == 3
