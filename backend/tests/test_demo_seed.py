"""Demo seeding: only on an empty database, mock only, completes, never touches existing data."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, InlineExecutor
from app.bench.pricing import PriceBook
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.db import get_sessionmaker
from app.models import BenchmarkRun, FreshnessCheck, TaskExecution
from app.seed import seed_agents, seed_suites
from app.seed.demo import seed_demo_data

SETTINGS = Settings(mock_mode=True, mock_latency_scale=0)


def _engine():
    mock = (MockLLMClient(seed=1, latency_scale=0), MockSearchClient(latency_scale=0))
    return BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=SETTINGS,
        clients=lambda live: mock,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )


def test_demo_seed_creates_two_finished_mock_runs_and_checks(db_engine):
    engine = _engine()
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
        ids = seed_demo_data(db, engine, SETTINGS, background=False)
    assert len(ids) == 2
    with Session(db_engine) as db:
        runs = [db.get(BenchmarkRun, i) for i in ids]
        assert all(r.status == "done" and r.mock_mode and len(r.agent_ids) == 4 for r in runs)
        assert db.query(TaskExecution).count() == (8 + 6) * 4 * 2
        manual = db.query(FreshnessCheck).filter(FreshnessCheck.source == "text").all()
        assert [c.verdict for c in manual] == ["stale", "stale"]
        assert db.query(FreshnessCheck).filter(FreshnessCheck.source == "benchmark").count() > 0


def test_demo_seed_is_skipped_when_runs_exist_or_no_agents(db_engine):
    engine = _engine()
    with Session(db_engine) as db:
        assert seed_demo_data(db, engine, SETTINGS, background=False) == []  # no agents seeded
        seed_agents(db)
        seed_suites(db)
        assert len(seed_demo_data(db, engine, SETTINGS, background=False)) == 2
        assert seed_demo_data(db, engine, SETTINGS, background=False) == []  # runs exist now
        assert db.query(BenchmarkRun).count() == 2
