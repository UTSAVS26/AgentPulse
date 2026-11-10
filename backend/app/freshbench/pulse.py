"""Pulse: generate today's Freshness Bench and run it every day, and track each agent over time.

``FRESHBENCH_PULSE_HOUR_UTC`` turns the scheduler on. Every few minutes it checks whether today's snapshot
exists for the mode it runs in; after the configured hour, if not, it generates one and starts a run with
``FRESHBENCH_PULSE_AGENTS``. :func:`history` lists every Freshness Bench run per agent with a drift alert when
the newest accuracy falls well below the agent's previous ones.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.freshbench import jobs
from app.mode import live_available
from app.models import Agent, BenchmarkRun, TaskExecution, TaskSuite

log = logging.getLogger(__name__)

CHECK_EVERY_S = 300
state: dict[str, Any] = {"enabled": False, "last_check": None, "last_started": None, "last_error": None}


def pulse_live(settings: Settings) -> bool:
    return live_available(settings) and not settings.mock_mode


def tick(settings: Settings, sessions, engine_factory, now: datetime | None = None) -> str:
    """One scheduler check; returns what it did (for logs and tests)."""
    now = now or datetime.now(UTC)
    state["last_check"] = now.isoformat(timespec="seconds")
    if settings.freshbench_pulse_hour_utc is None:
        return "off"
    if now.hour < settings.freshbench_pulse_hour_utc:
        return "waiting for the hour"
    live = pulse_live(settings)
    with sessions() as db:
        if jobs.find_snapshot(db, now.date(), live) is not None:
            return "today's snapshot exists"
    if jobs.running_job():
        return "a generation is running"
    agent_ids = settings.pulse_agent_ids

    def run_it(suite_id: int) -> None:
        engine = engine_factory()
        with sessions() as db:
            known = set(db.scalars(select(Agent.id).where(Agent.id.in_(agent_ids))))
            if not known:
                state["last_error"] = "FRESHBENCH_PULSE_AGENTS names no existing agent"
                return
            run = engine.create_run(
                db,
                suite_id=suite_id,
                agent_ids=sorted(known),
                repeats=1,
                fault_rate=0.0,
                live=live,
                check_freshness=False,
            )
            engine.start(run.id)

    jobs.start_generation(sessions, settings, live=live, source="pulse", on_done=run_it)
    state["last_started"] = now.isoformat(timespec="seconds")
    return "started"


def start_scheduler(settings: Settings, sessions, engine_factory) -> threading.Thread | None:
    if settings.freshbench_pulse_hour_utc is None:
        return None
    state["enabled"] = True
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(5 if state["last_check"] is None else CHECK_EVERY_S):
            try:
                log.info("Freshness Bench pulse: %s", tick(settings, sessions, engine_factory))
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                state["last_error"] = str(exc)[:300]
                log.exception("Freshness Bench pulse failed")

    thread = threading.Thread(target=loop, name="freshbench-pulse", daemon=True)
    thread.start()
    return thread


def history(db: Session, drift_points: float) -> dict[str, Any]:
    """Accuracy per agent per Freshness Bench run (oldest first), with drift alerts."""
    rows = db.execute(
        select(
            BenchmarkRun.id,
            BenchmarkRun.mock_mode,
            TaskSuite.id,
            TaskSuite.name,
            TaskSuite.snapshot_date,
            TaskExecution.agent_id,
            func.count(),
            func.sum(case((TaskExecution.passed.is_(True), 1), else_=0)),
        )
        .join(TaskSuite, TaskSuite.id == BenchmarkRun.suite_id)
        .join(TaskExecution, TaskExecution.run_id == BenchmarkRun.id)
        .where(TaskSuite.kind == "freshbench", BenchmarkRun.status == "done", TaskExecution.status != "error")
        .group_by(
            BenchmarkRun.id,
            BenchmarkRun.mock_mode,
            TaskSuite.id,
            TaskSuite.name,
            TaskSuite.snapshot_date,
            TaskExecution.agent_id,
        )
        .order_by(TaskSuite.snapshot_date, BenchmarkRun.id)
    ).all()
    names = dict(db.execute(select(Agent.id, Agent.name)).all())
    points: dict[int, list[dict]] = defaultdict(list)
    for run_id, mock, suite_id, suite_name, snapshot, agent_id, n, passed in rows:
        points[agent_id].append(
            {
                "run_id": run_id,
                "suite_id": suite_id,
                "suite_name": suite_name,
                "snapshot_date": snapshot,
                "mock_mode": bool(mock),
                "executions": int(n),
                "accuracy": round((passed or 0) / n, 4) if n else None,
            }
        )
    agents, alerts = [], []
    for agent_id, series in points.items():
        name = names.get(agent_id, f"(deleted agent {agent_id})")
        agents.append({"agent_id": agent_id, "agent_name": name, "points": series})
        if len(series) >= 2:
            latest, previous = series[-1], [p["accuracy"] for p in series[-4:-1] if p["accuracy"] is not None]
            if previous and latest["accuracy"] is not None:
                drop = (sum(previous) / len(previous) - latest["accuracy"]) * 100
                if drop > drift_points:
                    alerts.append(
                        {
                            "agent_id": agent_id,
                            "agent_name": name,
                            "drop_points": round(drop, 1),
                            "latest": latest,
                            "message": f"{name} answered {drop:.0f} points fewer Freshness Bench questions "
                            f"correctly on {latest['snapshot_date']} than in its previous runs.",
                        }
                    )
    return {"agents": agents, "alerts": alerts, "drift_points": drift_points, "pulse": dict(state)}
