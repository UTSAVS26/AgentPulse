"""Demo data so a fresh deployment never opens on empty pages.

On startup, when the database has **no runs at all**, two small **mock** benchmark runs are created and
executed in a background thread, and two example stale-answer checks are saved:

* Math & Reasoning: 4 agents × 2 repeats, 20% tool faults (accuracy, recovery, cost, latency);
* Current Events: the same agents (freshness, stale answers, tools vs no-tools baseline);
* a mock **Freshness Bench** snapshot (fictional dated news), run by GPT-OSS 120B with and without tools,
  so the knowledge-cutoff curve is there on first open. It is created first and executed last, so Compare
  still opens on Current Events.

They are always mock (labelled as such everywhere), cost nothing and need no keys. They never run again
once any run exists, so user data is never touched. Disable with ``SEED_DEMO_DATA=false``.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine
from app.clients import get_mock_llm, get_mock_search
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.freshbench.generator import Generator
from app.freshbench.jobs import save_snapshot
from app.freshness.checker import FreshnessChecker
from app.freshness.store import save_check
from app.models import Agent, BenchmarkRun, FreshnessCheck, TaskSuite

log = logging.getLogger(__name__)

DEMO_AGENTS = (
    "GPT-OSS 120B · tools",
    "GPT-OSS 20B · tools",
    "Qwen3.8 27B · tools",
    "GPT-OSS 120B · no tools (baseline)",
)
DEMO_SUITES = ("Math & Reasoning", "Current Events (time-sensitive)")  # last = newest = shown first
FRESHBENCH_AGENTS = ("GPT-OSS 120B · tools", "GPT-OSS 120B · no tools (baseline)")
DEMO_TEXTS = (
    (
        "The current UK Prime Minister is Rishi Sunak. Argentina won the latest FIFA World Cup. "
        "Water boils at 100 °C at sea level.",
        None,
    ),
    ("The answer is Python 3.13.", "What is the latest stable version of Python?"),
)


def seed_demo_data(
    db: Session, engine: BenchmarkEngine, settings: Settings, background: bool = True
) -> list[int]:
    """Create the demo runs and checks if the database has no runs.

    The example checks are saved right away; the runs (and the demo Freshness Bench) are created and executed
    on a background thread so startup stays fast on small instances. Returns the new run ids when
    ``background`` is False (tests), else an empty list."""
    if db.scalar(select(func.count(BenchmarkRun.id))):
        return []
    agent_ids = [a for a in (db.scalar(select(Agent.id).where(Agent.name == n)) for n in DEMO_AGENTS) if a]
    if not agent_ids:
        return []

    if not db.scalar(select(func.count(FreshnessCheck.id))):
        checker = FreshnessChecker(
            get_mock_llm(), get_mock_search(), settings.freshness_models, retry_backoff_s=0
        )
        for text, question in DEMO_TEXTS:
            save_check(
                db, checker.check(text, question=question), input_text=text, question=question, mock_mode=True
            )
        db.commit()

    def work() -> list[int]:
        with engine.sessions() as session:
            if session.scalar(select(func.count(BenchmarkRun.id))):
                return []  # someone started a run meanwhile: never mix demo data into real data
            # Creation order sets the run ids: the bench first, so Compare opens on Current Events.
            fb_run = _freshbench_run(session, engine, settings)
            run_ids = []
            for name in DEMO_SUITES:
                suite = session.scalar(select(TaskSuite).where(TaskSuite.name == name))
                if suite is None:
                    continue
                run = engine.create_run(
                    session, suite_id=suite.id, agent_ids=agent_ids, repeats=2, fault_rate=0.2, live=False
                )
                run_ids.append(run.id)
        log.info("Seeding demo runs %s and Freshness Bench run %s (mock)", run_ids, fb_run)
        for rid in [*run_ids, *([fb_run] if fb_run else [])]:  # sequential, on this thread
            engine.execute(rid)
        log.info("Demo runs finished")
        return [*([fb_run] if fb_run else []), *run_ids]

    if background:
        threading.Thread(target=work, name="demo-seed", daemon=True).start()
        return []
    return work()


def _freshbench_run(db: Session, engine: BenchmarkEngine, settings: Settings) -> int | None:
    """A mock Freshness Bench snapshot (instant: zero-latency mock clients) and a pending run on it."""
    agent_ids = [
        a for a in (db.scalar(select(Agent.id).where(Agent.name == n)) for n in FRESHBENCH_AGENTS) if a
    ]
    if not agent_ids:
        return None
    try:
        result = Generator(
            MockLLMClient(latency_scale=0),
            MockSearchClient(latency_scale=0),
            settings.freshness_models,
            today=datetime.now(UTC).date(),
            per_cell=settings.freshbench_per_cell,
            retry_backoff_s=0,
        ).run()
        if not result.questions:
            return None
        suite = save_snapshot(db, result, live=False, scorer="contains")
        run = engine.create_run(
            db,
            suite_id=suite.id,
            agent_ids=agent_ids,
            repeats=1,
            fault_rate=0.0,
            live=False,
            check_freshness=False,
        )
        return run.id
    except Exception:  # noqa: BLE001 - demo data must never break startup
        log.exception("Could not seed the demo Freshness Bench")
        return None
