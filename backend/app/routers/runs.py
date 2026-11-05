"""Benchmark runs: start, poll progress, list executions, metrics, cancel, delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, RunValidationError, get_bench_engine, task_lookup
from app.bench.metrics import ExecRecord, run_metrics
from app.db import get_db
from app.mode import Mode, get_mode, require_admin
from app.models import Agent, BenchmarkRun, Task, TaskExecution, TaskSuite
from app.schemas import ExecutionOut, RunCreate, RunExecutionOut, RunOut

router = APIRouter(tags=["runs"])


def _get_run(db: Session, run_id: int) -> BenchmarkRun:
    run = db.get(BenchmarkRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


def _agent_info(db: Session, ids: list[int]) -> dict[int, dict]:
    found = {a.id: a for a in db.scalars(select(Agent).where(Agent.id.in_(ids)))}
    return {
        i: {
            "name": found[i].name if i in found else f"(deleted agent {i})",
            "model": found[i].model if i in found else "",
        }
        for i in ids
    }


def run_out(db: Session, run: BenchmarkRun) -> RunOut:
    suite = db.get(TaskSuite, run.suite_id)
    agents = _agent_info(db, run.agent_ids)
    duration = None
    if run.started_at:
        end = run.finished_at or None
        if end:
            duration = (end - run.started_at).total_seconds()
    return RunOut(
        id=run.id,
        suite_id=run.suite_id,
        suite_name=suite.name if suite else "(deleted)",
        agent_ids=run.agent_ids,
        agent_names=[agents[i]["name"] for i in run.agent_ids],
        repeats=run.repeats,
        fault_rate=run.fault_rate,
        mock_mode=run.mock_mode,
        status=run.status,
        progress_done=run.progress_done,
        progress_total=run.progress_total,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_s=duration,
    )


@router.post(
    "/runs",
    response_model=RunOut,
    status_code=202,
    summary="Start a benchmark run",
    description=(
        "Creates a run of `suite × agents × repeats` and starts it in the background. "
        "Poll `GET /runs/{id}` for progress and `GET /runs/{id}/metrics` for results."
    ),
)
def create_run(
    body: RunCreate,
    db: Session = Depends(get_db),
    engine: BenchmarkEngine = Depends(get_bench_engine),
    mode: Mode = Depends(get_mode),
) -> RunOut:
    try:
        run = engine.create_run(
            db,
            suite_id=body.suite_id,
            agent_ids=body.agent_ids,
            repeats=body.repeats,
            fault_rate=body.fault_rate,
            live=mode.live,
        )
    except RunValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    engine.start(run.id)
    db.refresh(run)
    return run_out(db, run)


@router.get("/runs", response_model=list[RunOut], summary="List runs (newest first)")
def list_runs(limit: int = 50, db: Session = Depends(get_db)) -> list[RunOut]:
    runs = db.scalars(select(BenchmarkRun).order_by(BenchmarkRun.id.desc()).limit(max(1, min(limit, 200))))
    return [run_out(db, r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunOut, summary="Run status and progress")
def get_run(run_id: int, db: Session = Depends(get_db)) -> RunOut:
    return run_out(db, _get_run(db, run_id))


@router.get(
    "/runs/{run_id}/executions",
    response_model=list[RunExecutionOut],
    summary="All executions of a run (without traces; see /executions/{id})",
)
def run_executions(run_id: int, db: Session = Depends(get_db)) -> list[RunExecutionOut]:
    run = _get_run(db, run_id)
    agents = _agent_info(db, run.agent_ids)
    tasks = {t.id: t for t in db.scalars(select(Task).where(Task.suite_id == run.suite_id))}
    rows = db.scalars(
        select(TaskExecution)
        .where(TaskExecution.run_id == run_id)
        .order_by(TaskExecution.agent_id, TaskExecution.task_id, TaskExecution.repeat_idx)
    )
    out = []
    for e in rows:
        base = ExecutionOut.model_validate(e).model_dump()
        task = tasks.get(e.task_id)
        out.append(
            RunExecutionOut(
                **base,
                agent_name=agents.get(e.agent_id, {}).get("name", "?"),
                task_key=task.key if task else "?",
                category=task.category if task else "?",
            )
        )
    return out


@router.get(
    "/runs/{run_id}/metrics",
    summary="Leaderboard and per-agent metrics for a run",
    description=(
        "Accuracy, consistency, pass@k, error rate, recovery rate, latency p50/p95, tokens, cost and the "
        "composite reliability score per agent, plus a task × agent pass-rate matrix. "
        "Available while the run is in progress (partial results). Formulas: docs/metrics.md."
    ),
)
def get_run_metrics(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = _get_run(db, run_id)
    records = [
        ExecRecord.from_orm(e)
        for e in db.scalars(select(TaskExecution).where(TaskExecution.run_id == run_id))
    ]
    metrics = run_metrics(records, _agent_info(db, run.agent_ids), task_lookup(db, run.suite_id))
    return {"run": run_out(db, run).model_dump(mode="json"), **metrics}


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunOut,
    summary="Cancel a pending or running run",
    dependencies=[Depends(require_admin)],
)
def cancel_run(
    run_id: int, db: Session = Depends(get_db), engine: BenchmarkEngine = Depends(get_bench_engine)
) -> RunOut:
    _get_run(db, run_id)
    run = engine.cancel(db, run_id)
    return run_out(db, run)


@router.delete(
    "/runs/{run_id}",
    status_code=204,
    summary="Delete a finished run and its executions",
    dependencies=[Depends(require_admin)],
)
def delete_run(run_id: int, db: Session = Depends(get_db)) -> Response:
    run = _get_run(db, run_id)
    if run.status in ("pending", "running"):
        raise HTTPException(status_code=409, detail="cancel the run before deleting it")
    db.delete(run)
    db.commit()
    return Response(status_code=204)
