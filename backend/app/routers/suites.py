"""Task suites: list, inspect, create and delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.mode import require_admin
from app.models import BenchmarkRun, Task, TaskSuite
from app.schemas import SuiteCreate, SuiteDetailOut, SuiteOut, TaskOut

router = APIRouter(tags=["suites"])


def _summary(suite: TaskSuite) -> dict:
    return {
        "id": suite.id,
        "name": suite.name,
        "description": suite.description,
        "task_count": len(suite.tasks),
        "categories": sorted({t.category for t in suite.tasks}),
        "time_sensitive_count": sum(t.time_sensitive for t in suite.tasks),
    }


def _get_suite(db: Session, suite_id: int) -> TaskSuite:
    suite = db.get(TaskSuite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail=f"suite {suite_id} not found")
    return suite


@router.get("/suites", response_model=list[SuiteOut], summary="List task suites")
def list_suites(db: Session = Depends(get_db)) -> list[dict]:
    return [_summary(s) for s in db.scalars(select(TaskSuite).order_by(TaskSuite.id))]


@router.get("/suites/{suite_id}", response_model=SuiteDetailOut, summary="Get a suite with its tasks")
def get_suite(suite_id: int, db: Session = Depends(get_db)) -> dict:
    suite = _get_suite(db, suite_id)
    return {**_summary(suite), "tasks": [TaskOut.model_validate(t) for t in suite.tasks]}


@router.post("/suites", response_model=SuiteDetailOut, status_code=201, summary="Create a custom suite")
def create_suite(body: SuiteCreate, db: Session = Depends(get_db)) -> dict:
    suite = TaskSuite(
        name=body.name,
        description=body.description,
        tasks=[Task(**t.model_dump()) for t in body.tasks],
    )
    db.add(suite)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"a suite named '{body.name}' already exists") from exc
    db.refresh(suite)
    return {**_summary(suite), "tasks": [TaskOut.model_validate(t) for t in suite.tasks]}


@router.delete(
    "/suites/{suite_id}",
    status_code=204,
    summary="Delete a suite (only if it has no runs)",
    dependencies=[Depends(require_admin)],
)
def delete_suite(suite_id: int, db: Session = Depends(get_db)) -> Response:
    suite = _get_suite(db, suite_id)
    runs = db.scalar(select(func.count()).select_from(BenchmarkRun).where(BenchmarkRun.suite_id == suite_id))
    if runs:
        raise HTTPException(status_code=409, detail=f"suite has {runs} benchmark runs; delete them first")
    db.delete(suite)
    db.commit()
    return Response(status_code=204)
