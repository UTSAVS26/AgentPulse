"""Task suites and their tasks: list, inspect, create, edit, duplicate, import/export, delete.

Built-in suites (seeded from ``seed/suites/*.yaml``) are read-only: duplicate one to edit it. Custom suites
are open to everyone, including on the public demo (they only ever run in the caller's mode). Anything already
used by a benchmark run is protected: a task with executions can't be deleted, and nor can a suite with runs.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bench.scoring import render_expected, score_output
from app.db import get_db
from app.models import BenchmarkRun, Task, TaskExecution, TaskSuite
from app.schemas import (
    ImportResult,
    ScorePreviewIn,
    ScorePreviewOut,
    SuiteCreate,
    SuiteDetailOut,
    SuiteOut,
    SuiteUpdate,
    TaskIn,
    TaskOut,
)

router = APIRouter(tags=["suites"])

TASK_FIELDS = list(TaskIn.model_fields)  # key, prompt, expected, scorer, tolerance, category, ...
MAX_IMPORT_BYTES = 1_000_000


# ------------------------------------------------------------------------------------ helpers
def _run_count(db: Session, suite_id: int) -> int:
    return (
        db.scalar(select(func.count()).select_from(BenchmarkRun).where(BenchmarkRun.suite_id == suite_id))
        or 0
    )


def _summary(db: Session, suite: TaskSuite) -> dict:
    return {
        "id": suite.id,
        "name": suite.name,
        "description": suite.description,
        "builtin": bool(suite.builtin),
        "runs": _run_count(db, suite.id),
        "task_count": len(suite.tasks),
        "categories": sorted({t.category for t in suite.tasks}),
        "time_sensitive_count": sum(t.time_sensitive for t in suite.tasks),
        "kind": suite.kind,
        "snapshot_date": suite.snapshot_date,
    }


def _detail(db: Session, suite: TaskSuite) -> dict:
    used = dict(
        db.execute(
            select(TaskExecution.task_id, func.count())
            .where(TaskExecution.task_id.in_([t.id for t in suite.tasks]))
            .group_by(TaskExecution.task_id)
        ).all()
    )
    tasks = [
        TaskOut.model_validate(t).model_copy(update={"executions": used.get(t.id, 0)}) for t in suite.tasks
    ]
    return {**_summary(db, suite), "tasks": tasks}


def _get_suite(db: Session, suite_id: int) -> TaskSuite:
    suite = db.get(TaskSuite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail=f"suite {suite_id} not found")
    return suite


def _editable(db: Session, suite_id: int, *, deleting: bool = False) -> TaskSuite:
    suite = _get_suite(db, suite_id)
    if suite.builtin:
        raise HTTPException(
            status_code=403, detail=f"'{suite.name}' is a built-in suite and read-only: duplicate it to edit"
        )
    if suite.kind == "freshbench" and not deleting:
        raise HTTPException(
            status_code=403,
            detail=f"'{suite.name}' is a Freshness Bench snapshot and read-only (so results stay "
            "reproducible): duplicate it to edit",
        )
    return suite


def _get_task(suite: TaskSuite, task_id: int) -> Task:
    task = next((t for t in suite.tasks if t.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found in suite {suite.id}")
    return task


def _commit(db: Session, conflict: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict) from exc


def _name_taken(db: Session, name: str, exclude_id: int | None = None) -> bool:
    q = select(TaskSuite.id).where(func.lower(TaskSuite.name) == name.lower())
    if exclude_id is not None:
        q = q.where(TaskSuite.id != exclude_id)
    return db.scalar(q) is not None


def _create(db: Session, spec: SuiteCreate) -> TaskSuite:
    if _name_taken(db, spec.name):
        raise HTTPException(status_code=409, detail=f"a suite named '{spec.name}' already exists")
    suite = TaskSuite(
        name=spec.name,
        description=spec.description,
        builtin=False,
        tasks=[Task(**t.model_dump()) for t in spec.tasks],
    )
    db.add(suite)
    _commit(db, f"a suite named '{spec.name}' already exists")
    db.refresh(suite)
    return suite


def _free_name(db: Session, base: str) -> str:
    name, n = f"{base} (copy)", 2
    while _name_taken(db, name):
        name, n = f"{base} (copy {n})", n + 1
    return name[:120]


# ------------------------------------------------------------------------------------ suites
@router.get("/suites", response_model=list[SuiteOut], summary="List task suites")
def list_suites(db: Session = Depends(get_db)) -> list[dict]:
    return [_summary(db, s) for s in db.scalars(select(TaskSuite).order_by(TaskSuite.id))]


@router.get("/suites/{suite_id}", response_model=SuiteDetailOut, summary="Get a suite with its tasks")
def get_suite(suite_id: int, db: Session = Depends(get_db)) -> dict:
    return _detail(db, _get_suite(db, suite_id))


@router.post(
    "/suites",
    response_model=SuiteDetailOut,
    status_code=201,
    summary="Create a custom suite",
    description="Tasks are optional: create an empty suite and add tasks with `POST /suites/{id}/tasks`.",
)
def create_suite(body: SuiteCreate, db: Session = Depends(get_db)) -> dict:
    return _detail(db, _create(db, body))


@router.put(
    "/suites/{suite_id}", response_model=SuiteDetailOut, summary="Rename or re-describe a custom suite"
)
def update_suite(suite_id: int, body: SuiteUpdate, db: Session = Depends(get_db)) -> dict:
    suite = _editable(db, suite_id)
    if _name_taken(db, body.name, exclude_id=suite_id):
        raise HTTPException(status_code=409, detail=f"a suite named '{body.name}' already exists")
    suite.name, suite.description = body.name, body.description
    _commit(db, f"a suite named '{body.name}' already exists")
    return _detail(db, suite)


@router.post(
    "/suites/{suite_id}/duplicate",
    response_model=SuiteDetailOut,
    status_code=201,
    summary="Copy a suite (built-in or custom) into a new editable suite",
)
def duplicate_suite(suite_id: int, name: str | None = None, db: Session = Depends(get_db)) -> dict:
    src = _get_suite(db, suite_id)
    spec = SuiteCreate(
        name=(name or "").strip() or _free_name(db, src.name),
        description=src.description,
        tasks=[TaskIn.model_validate(t, from_attributes=True) for t in src.tasks],
    )
    return _detail(db, _create(db, spec))


@router.delete(
    "/suites/{suite_id}",
    status_code=204,
    summary="Delete a custom suite (only if it has no runs)",
)
def delete_suite(suite_id: int, db: Session = Depends(get_db)) -> Response:
    suite = _editable(db, suite_id, deleting=True)
    runs = _run_count(db, suite_id)
    if runs:
        raise HTTPException(status_code=409, detail=f"suite has {runs} benchmark runs; delete them first")
    db.delete(suite)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------------------------ tasks
@router.post("/suites/{suite_id}/tasks", response_model=TaskOut, status_code=201, summary="Add a task")
def add_task(suite_id: int, body: TaskIn, db: Session = Depends(get_db)) -> TaskOut:
    suite = _editable(db, suite_id)
    if any(t.key == body.key for t in suite.tasks):
        raise HTTPException(status_code=409, detail=f"task key '{body.key}' already exists in this suite")
    task = Task(**body.model_dump())
    suite.tasks.append(task)
    db.commit()
    db.refresh(task)
    return TaskOut.model_validate(task)


@router.put("/suites/{suite_id}/tasks/{task_id}", response_model=TaskOut, summary="Edit a task")
def update_task(suite_id: int, task_id: int, body: TaskIn, db: Session = Depends(get_db)) -> TaskOut:
    suite = _editable(db, suite_id)
    task = _get_task(suite, task_id)
    if any(t.key == body.key and t.id != task_id for t in suite.tasks):
        raise HTTPException(status_code=409, detail=f"task key '{body.key}' already exists in this suite")
    keep_if_unset = {"event_date", "source_url", "source_title"}  # the dashboard table doesn't send them
    for field, value in body.model_dump().items():
        if field in keep_if_unset and field not in body.model_fields_set:
            continue
        setattr(task, field, value)
    db.commit()
    db.refresh(task)
    used = db.scalar(select(func.count()).select_from(TaskExecution).where(TaskExecution.task_id == task_id))
    return TaskOut.model_validate(task).model_copy(update={"executions": used or 0})


@router.delete(
    "/suites/{suite_id}/tasks/{task_id}",
    status_code=204,
    summary="Delete a task (only if no execution used it)",
)
def delete_task(suite_id: int, task_id: int, db: Session = Depends(get_db)) -> Response:
    suite = _editable(db, suite_id)
    task = _get_task(suite, task_id)
    used = db.scalar(select(func.count()).select_from(TaskExecution).where(TaskExecution.task_id == task_id))
    if used:
        raise HTTPException(
            status_code=409,
            detail=f"task '{task.key}' was used by {used} executions; it is kept so past runs stay intact",
        )
    suite.tasks.remove(task)
    db.delete(task)
    db.commit()
    return Response(status_code=204)


# ------------------------------------------------------------------------------------ import / export
def suite_to_yaml(suite: TaskSuite) -> str:
    """Same format as ``seed/suites/*.yaml``; default values are omitted to keep files short."""
    defaults = {f: TaskIn.model_fields[f].default for f in TASK_FIELDS}
    tasks = []
    for t in suite.tasks:
        row = {"key": t.key, "prompt": t.prompt}
        for f in TASK_FIELDS[2:]:
            value = getattr(t, f)
            if value != defaults.get(f) and value is not None:
                row[f] = value
        tasks.append(row)
    doc = {"name": suite.name, "description": suite.description, "tasks": tasks}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110)


def suite_to_csv(suite: TaskSuite) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(TASK_FIELDS)
    for t in suite.tasks:
        w.writerow(["" if getattr(t, f) is None else getattr(t, f) for f in TASK_FIELDS])
    return buf.getvalue()


@router.get("/suites/{suite_id}/export", summary="Export a suite as YAML or CSV")
def export_suite(
    suite_id: int, format: Literal["yaml", "csv"] = "yaml", db: Session = Depends(get_db)
) -> Response:
    suite = _get_suite(db, suite_id)
    slug = re.sub(r"[^a-z0-9]+", "-", suite.name.lower()).strip("-")[:60] or "suite"
    if format == "csv":
        body, media = suite_to_csv(suite), "text/csv; charset=utf-8"
    else:
        body, media = suite_to_yaml(suite), "application/yaml; charset=utf-8"
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{slug}.{format}"'},
    )


async def _raw_text(request: Request) -> str:
    """The request body as text (imports send the file as-is, not as JSON)."""
    data = await request.body()
    if len(data) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 1 MB)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="the file must be UTF-8 text") from exc


def _errors(exc: ValidationError, prefix: str = "") -> list[str]:
    return [f"{prefix}{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "x", "⏰")


def parse_csv_tasks(text: str) -> tuple[list[TaskIn], list[str]]:
    """Rows → tasks. Unknown columns are ignored (with a warning); blank optional cells use defaults."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or not {"key", "prompt"} <= {f.strip() for f in reader.fieldnames}:
        raise HTTPException(
            status_code=422, detail=f"CSV needs at least the columns 'key' and 'prompt' ({TASK_FIELDS})"
        )
    warnings = [f"ignored unknown column '{f}'" for f in reader.fieldnames if f.strip() not in TASK_FIELDS]
    tasks, errors = [], []
    for line, row in enumerate(reader, start=2):
        data = {k.strip(): (v or "").strip() for k, v in row.items() if k and k.strip() in TASK_FIELDS}
        if not any(data.values()):
            continue  # blank line
        data = {k: v for k, v in data.items() if v != "" or k in ("key", "prompt")}
        if "time_sensitive" in data:
            data["time_sensitive"] = _parse_bool(data["time_sensitive"])
        try:
            tasks.append(TaskIn.model_validate(data))
        except ValidationError as exc:
            errors += _errors(exc, f"line {line}: ")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return tasks, warnings


@router.post(
    "/suites/import",
    response_model=ImportResult,
    status_code=201,
    summary="Create a suite from a YAML or CSV file",
    description=(
        "Send the file's text as the request body (`text/plain`). **YAML**: the same format as the "
        "built-in suites (`name`, `description`, `tasks: [...]`); `name` can be overridden with `?name=`. "
        f"**CSV**: one row per task with columns {', '.join(TASK_FIELDS)} (only `key` and `prompt` "
        "required); `?name=` is required. Validation errors are reported per task or line (422)."
    ),
    openapi_extra={
        "requestBody": {"content": {"text/plain": {"schema": {"type": "string"}}}, "required": True}
    },
)
def import_suite(
    format: Literal["yaml", "csv"] = Query(...),
    name: str | None = None,
    description: str | None = None,
    body: str = Depends(_raw_text),
    db: Session = Depends(get_db),
) -> dict:
    warnings: list[str] = []
    if format == "yaml":
        try:
            doc = yaml.safe_load(body) or {}
        except yaml.YAMLError as exc:
            raise HTTPException(status_code=422, detail=f"invalid YAML: {exc}") from exc
        if not isinstance(doc, dict):
            raise HTTPException(status_code=422, detail="YAML must be a mapping with 'name' and 'tasks'")
        if name:
            doc["name"] = name
        if description is not None:
            doc["description"] = description
        try:
            spec = SuiteCreate.model_validate(doc)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_errors(exc)) from exc
    else:
        if not (name or "").strip():
            raise HTTPException(status_code=422, detail="CSV import needs ?name= for the new suite")
        tasks, warnings = parse_csv_tasks(body)
        try:
            spec = SuiteCreate(name=name, description=description or "", tasks=tasks)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_errors(exc)) from exc
    suite = _create(db, spec)
    return {"suite": _detail(db, suite), "warnings": warnings}


# ------------------------------------------------------------------------------------ scorer preview
@router.post(
    "/scoring/preview",
    response_model=ScorePreviewOut,
    summary="Score a sample answer against a task definition (no LLM)",
    description=(
        "Lets you check a task's scorer before running a benchmark. Date placeholders "
        "(`{{current_year}}`, `{{today_iso}}`, `{{today_weekday}}`) are filled in. The `judge` scorer needs "
        "an LLM, so the preview uses its `contains` fallback (labelled as such)."
    ),
)
def preview_score(body: ScorePreviewIn) -> ScorePreviewOut:
    result = score_output(body.output, scorer=body.scorer, expected=body.expected, tolerance=body.tolerance)
    detail = result.detail
    if body.scorer in ("judge", "jury"):
        graders = "an LLM judge" if body.scorer == "judge" else "a jury of judge models"
        fallback = detail.split("contains: ", 1)[-1]
        detail = (
            f"preview only: in a benchmark run, {graders} grades this answer. "
            f"Here it's approximated by checking the expected text appears ({fallback})"
        )
    return ScorePreviewOut(
        score=result.score,
        passed=result.passed,
        detail=detail,
        expected_rendered=render_expected(body.expected),
    )
