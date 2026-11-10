"""Background generation jobs and snapshot storage.

Generation takes minutes live (dozens of searches and model calls), so it runs in a thread and reports
progress. One job runs at a time per process. Jobs live in memory: after a restart the snapshot suites
(the results) remain, only the job log is gone.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients import clients_for
from app.config import Settings
from app.freshbench.buckets import BUCKETS, CATEGORIES
from app.freshbench.generator import GenerationResult, Generator
from app.models import Task, TaskSuite

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    live: bool
    params: dict[str, Any]
    status: str = "running"  # running | done | failed
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    progress_done: int = 0
    progress_total: int = 0
    note: str = "starting"
    suite_id: int | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None
    source: str = "manual"  # manual | pulse

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_jobs: OrderedDict[int, Job] = OrderedDict()
_ids = itertools.count(1)
_lock = threading.Lock()


def jobs() -> list[Job]:
    with _lock:
        return list(reversed(_jobs.values()))


def get_job(job_id: int) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def running_job() -> Job | None:
    with _lock:
        return next((j for j in _jobs.values() if j.status == "running"), None)


def snapshot_name(day: date, live: bool) -> str:
    return f"Freshness Bench · {day.isoformat()}" + ("" if live else " (mock)")


def find_snapshot(db: Session, day: date, live: bool) -> TaskSuite | None:
    """The newest snapshot of ``day`` in this mode, if any."""
    base = snapshot_name(day, live)
    suites = db.scalars(
        select(TaskSuite).where(TaskSuite.kind == "freshbench", TaskSuite.snapshot_date == day.isoformat())
    ).all()
    mine = [s for s in suites if s.name == base or s.name.startswith(f"{base} #")]
    return max(mine, key=lambda s: s.id, default=None)


def save_snapshot(db: Session, result: GenerationResult, *, live: bool, scorer: str) -> TaskSuite:
    day = date.fromisoformat(result.today)
    base = name = snapshot_name(day, live)
    n = 2
    while db.scalar(select(TaskSuite.id).where(TaskSuite.name == name)):
        name, n = f"{base} #{n}", n + 1
    summary = result.summary()
    rejected = sum(summary["rejected"].values())
    source = "Tavily news search" if live else "a simulated (fictional) news corpus"
    description = (
        f"{summary['questions']} questions written from {summary['articles']} dated news articles "
        f"({source}, {result.today}) across {', '.join(sorted(summary['per_category']))}; "
        f"{rejected} candidate questions were rejected by validation. Event ages range from this week to "
        "2 years ago, so accuracy per age shows each agent's knowledge cutoff. Read-only snapshot."
    )
    suite = TaskSuite(
        name=name,
        description=description[:4000],
        builtin=False,
        kind="freshbench",
        snapshot_date=result.today,
        tasks=[Task(**q.task(scorer)) for q in result.questions],
    )
    db.add(suite)
    db.commit()
    db.refresh(suite)
    return suite


def start_generation(
    sessions: sessionmaker,
    settings: Settings,
    *,
    live: bool,
    categories: list[str] | None = None,
    buckets: list[str] | None = None,
    per_cell: int | None = None,
    scorer: str = "contains",
    source: str = "manual",
    on_done: Callable[[int], None] | None = None,
    run_inline: bool = False,
) -> Job:
    """Start a generation job (a thread, or inline for tests). Raises RuntimeError if one is running."""
    params = {
        "categories": categories or list(CATEGORIES),
        "buckets": buckets or [b.key for b in BUCKETS],
        "per_cell": per_cell or settings.freshbench_per_cell,
        "scorer": scorer,
    }
    with _lock:
        if any(j.status == "running" for j in _jobs.values()):
            raise RuntimeError("a Freshness Bench generation is already running")
        job = Job(id=next(_ids), live=live, params=params, source=source)
        _jobs[job.id] = job
        while len(_jobs) > 20:
            _jobs.popitem(last=False)

    def progress(done: int, total: int, note: str) -> None:
        job.progress_done, job.progress_total, job.note = done, total, note

    def work() -> None:
        try:
            llm, search = clients_for(live)
            gen = Generator(
                llm,
                search,
                settings.freshness_models,
                today=datetime.now(UTC).date(),
                categories=params["categories"],
                buckets=params["buckets"],
                per_cell=params["per_cell"],
                retry_backoff_s=0.5 if live else 0.0,
                progress=progress,
            )
            result = gen.run()
            job.summary = result.summary()
            if not result.questions:
                raise RuntimeError(
                    "no question passed validation"
                    + (f" ({'; '.join(result.errors[:3])})" if result.errors else "")
                )
            with sessions() as db:
                suite = save_snapshot(db, result, live=live, scorer=params["scorer"])
                job.suite_id = suite.id
            job.status, job.note = "done", f"{len(result.questions)} questions saved"
            if on_done:
                on_done(suite.id)
        except Exception as exc:  # noqa: BLE001 - a job must always finish with a status
            log.exception("Freshness Bench generation failed")
            job.status, job.error, job.note = "failed", str(exc)[:500], "failed"
        finally:
            job.finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    if run_inline:
        work()
    else:
        threading.Thread(target=work, name=f"freshbench-job-{job.id}", daemon=True).start()
    return job
