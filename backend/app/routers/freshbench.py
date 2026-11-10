"""Live Freshness Bench API: generate today's snapshot, follow the job, list snapshots, history + drift."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, get_sessionmaker
from app.freshbench import jobs, pulse
from app.freshbench.buckets import BUCKET_KEYS, BUCKETS, CATEGORIES, age_days, bucket_for
from app.mode import Mode, get_mode, require_admin
from app.models import BenchmarkRun, TaskSuite

router = APIRouter(tags=["freshness bench"])


class GenerateIn(BaseModel):
    categories: list[str] | None = Field(
        default=None, description=f"Subset of {list(CATEGORIES)} (default: all)."
    )
    buckets: list[str] | None = Field(
        default=None, description=f"Subset of {list(BUCKET_KEYS)} (default: all)."
    )
    per_cell: int | None = Field(default=None, ge=1, le=5, description="Questions per category × age bucket.")
    scorer: Literal["contains", "judge", "jury"] = "contains"
    force: bool = Field(default=False, description="Make a new snapshot even if today's already exists.")

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, v: list[str] | None) -> list[str] | None:
        bad = [c for c in v or [] if c not in CATEGORIES]
        if bad:
            raise ValueError(f"unknown categories {bad}; available: {list(CATEGORIES)}")
        return v

    @field_validator("buckets")
    @classmethod
    def _known_buckets(cls, v: list[str] | None) -> list[str] | None:
        bad = [b for b in v or [] if b not in BUCKET_KEYS]
        if bad:
            raise ValueError(f"unknown buckets {bad}; available: {list(BUCKET_KEYS)}")
        return v


@router.get("/freshbench/config", summary="Categories, age buckets and pulse settings")
def config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "categories": CATEGORIES,
        "buckets": [b.__dict__ for b in BUCKETS],
        "per_cell": settings.freshbench_per_cell,
        "pulse": {
            "hour_utc": settings.freshbench_pulse_hour_utc,
            "agent_ids": settings.pulse_agent_ids,
            "live": pulse.pulse_live(settings),
            **pulse.state,
        },
    }


@router.post("/freshbench/generate", status_code=202, summary="Generate today's Freshness Bench snapshot")
def generate(
    body: GenerateIn,
    mode: Mode = Depends(get_mode),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> dict:
    """Starts a background job (see ``GET /freshbench/jobs/{id}``). Live mode searches Tavily (one basic
    search per category × age bucket) and calls the models in ``FRESHNESS_MODEL`` / fallbacks; mock mode uses
    a fictional corpus. Today's snapshot is reused unless ``force`` (which needs the access code on the public
    demo)."""
    today = datetime.now(UTC).date()
    existing = jobs.find_snapshot(db, today, mode.live)
    if existing is not None and not body.force:
        return {"job": None, "existing_suite_id": existing.id, "message": f"'{existing.name}' already exists"}
    if body.force and not mode.live:
        require_admin(None, settings)  # anonymous visitors of the public demo reuse today's mock snapshot
    try:
        job = jobs.start_generation(
            get_sessionmaker(),
            settings,
            live=mode.live,
            categories=body.categories,
            buckets=body.buckets,
            per_cell=body.per_cell,
            scorer=body.scorer,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job": job.to_dict(), "existing_suite_id": None, "message": "generation started"}


@router.get("/freshbench/jobs", summary="Recent generation jobs (newest first)")
def list_jobs() -> list[dict]:
    return [j.to_dict() for j in jobs.jobs()]


@router.get("/freshbench/jobs/{job_id}", summary="One generation job: status, progress, result")
def get_job(job_id: int) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found (jobs are kept in memory)")
    return job.to_dict()


@router.get("/freshbench/snapshots", summary="Freshness Bench snapshots (newest first)")
def snapshots(db: Session = Depends(get_db)) -> list[dict]:
    suites = db.scalars(
        select(TaskSuite).where(TaskSuite.kind == "freshbench").order_by(TaskSuite.id.desc())
    ).all()
    runs = dict(
        db.execute(
            select(BenchmarkRun.suite_id, func.count())
            .where(BenchmarkRun.suite_id.in_([s.id for s in suites]))
            .group_by(BenchmarkRun.suite_id)
        ).all()
    )
    out = []
    for s in suites:
        per_bucket = {b.key: 0 for b in BUCKETS}
        for t in s.tasks:
            if t.event_date and s.snapshot_date:
                b = bucket_for(age_days(t.event_date, s.snapshot_date))
                if b:
                    per_bucket[b.key] += 1
        out.append(
            {
                "suite_id": s.id,
                "name": s.name,
                "snapshot_date": s.snapshot_date,
                "questions": len(s.tasks),
                "per_bucket": per_bucket,
                "categories": sorted({t.category for t in s.tasks}),
                "runs": runs.get(s.id, 0),
                "description": s.description,
            }
        )
    return out


@router.get(
    "/freshbench/history", summary="Accuracy per agent across Freshness Bench runs, with drift alerts"
)
def history(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return pulse.history(db, settings.freshbench_drift_points)
