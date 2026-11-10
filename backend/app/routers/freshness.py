"""Stale-answer detection API: check a text or an execution's answer against live search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.freshness.checker import FreshnessChecker
from app.freshness.store import check_out, check_summary, save_check
from app.mode import Mode, get_mode, require_admin
from app.models import FreshnessCheck, TaskExecution
from app.schemas import FreshnessCheckOut, FreshnessCheckRequest, FreshnessCheckSummary

router = APIRouter(tags=["freshness"])


def _get_check(db: Session, check_id: int) -> FreshnessCheck:
    check = db.get(FreshnessCheck, check_id)
    if check is None:
        raise HTTPException(status_code=404, detail=f"freshness check {check_id} not found")
    return check


@router.post(
    "/freshness/check",
    response_model=FreshnessCheckOut,
    status_code=201,
    summary="Detect stale claims in a text or an agent's answer",
    description=(
        "Extracts atomic claims, flags the time-sensitive ones, searches Tavily for each and asks an LLM "
        "judge whether the claim is still **current**, **outdated** (was true, no longer is), "
        "**contradicted** or **unverifiable**. Timeless claims are not searched. Claims about today's "
        "date are checked against the clock. Returns the claims with their sources (and publication "
        "dates) plus a freshness score. Never fails because of a search/LLM outage: affected claims are "
        "`unverifiable` with the reason. Live mode can take 5–20 s."
    ),
)
def create_check(
    body: FreshnessCheckRequest,
    db: Session = Depends(get_db),
    mode: Mode = Depends(get_mode),
    settings: Settings = Depends(get_settings),
) -> FreshnessCheckOut:
    text, question = (body.text or "").strip(), body.question
    if body.execution_id is not None:
        execution = db.get(TaskExecution, body.execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail=f"execution {body.execution_id} not found")
        if not (execution.output or "").strip():
            raise HTTPException(status_code=422, detail="this execution has no answer to check")
        text, question = execution.output, question or execution.prompt
    checker = FreshnessChecker(
        mode.llm,
        mode.search,
        settings.freshness_models,
        search_depth=body.search_depth,
        retry_backoff_s=0.5 if mode.live else 0.0,
    )
    result = checker.check(text, question=question)
    check = save_check(
        db,
        result,
        input_text=text,
        question=question,
        execution_id=body.execution_id,
        mock_mode=mode.mock,
        source="text" if body.execution_id is None else "execution",
    )
    if body.execution_id is not None:
        execution = db.get(TaskExecution, body.execution_id)
        execution.freshness_score = result.freshness_score
        execution.freshness_verdict = result.verdict
    db.commit()
    db.refresh(check)
    return check_out(check)


@router.get(
    "/freshness/checks",
    response_model=list[FreshnessCheckSummary],
    summary="Recent freshness checks (newest first)",
)
def list_checks(
    limit: int = 30,
    execution_id: int | None = None,
    manual_only: bool = False,
    source: str | None = None,
    db: Session = Depends(get_db),
) -> list[FreshnessCheckSummary]:
    """`execution_id` filters to one execution's checks; `manual_only` hides the automatic checks (benchmark
    runs and Freshness Guard calls); `source` keeps one source (`text`, `execution`, `benchmark`, `guard`)."""
    q = select(FreshnessCheck).order_by(FreshnessCheck.id.desc()).limit(max(1, min(limit, 200)))
    if execution_id is not None:
        q = q.where(FreshnessCheck.execution_id == execution_id)
    if manual_only:
        q = q.where(
            (FreshnessCheck.source.is_(None)) | (FreshnessCheck.source.not_in(("benchmark", "guard")))
        )
    if source:
        q = q.where(FreshnessCheck.source == source)
    return [check_summary(c) for c in db.scalars(q)]


@router.get(
    "/freshness/checks/{check_id}",
    response_model=FreshnessCheckOut,
    summary="A freshness check with its claims and sources",
)
def get_check(check_id: int, db: Session = Depends(get_db)) -> FreshnessCheckOut:
    return check_out(_get_check(db, check_id))


@router.delete(
    "/freshness/checks/{check_id}",
    status_code=204,
    summary="Delete a freshness check",
    dependencies=[Depends(require_admin)],
)
def delete_check(check_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get_check(db, check_id))
    db.commit()
    return Response(status_code=204)
