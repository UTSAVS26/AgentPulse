"""Persist freshness results and turn them into API responses."""

from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session

from app.freshness.checker import VERDICTS, FreshnessResult
from app.models import Claim, FreshnessCheck
from app.schemas import ClaimOut, FreshnessCheckOut, FreshnessCheckSummary


def save_check(
    db: Session,
    result: FreshnessResult,
    *,
    input_text: str,
    question: str | None = None,
    execution_id: int | None = None,
    mock_mode: bool = False,
    source: str = "text",
    model: str | None = None,
    guard_action: str | None = None,
) -> FreshnessCheck:
    """Add a check and its claims to the session and flush (the caller commits)."""
    check = FreshnessCheck(
        execution_id=execution_id,
        input_text=input_text,
        question=question,
        freshness_score=result.freshness_score,
        verdict=result.verdict,
        extraction=result.extraction[:80],
        mock_mode=mock_mode,
        source=source,
        model=model,
        guard_action=guard_action,
        latency_ms=round(result.latency_ms, 1),
        status="done",
        claims=[
            Claim(
                text=c.text,
                time_sensitive=c.time_sensitive,
                query=c.query,
                topic=c.topic,
                signals=c.signals,
                sources=c.sources,
                verdict=c.verdict,
                confidence=c.confidence,
                explanation=c.explanation,
                correction=c.correction,
                method=c.method,
            )
            for c in result.claims
        ],
    )
    db.add(check)
    db.flush()
    return check


def _counts(check: FreshnessCheck) -> dict[str, int]:
    c = Counter(cl.verdict for cl in check.claims)
    return {v: c.get(v, 0) for v in VERDICTS}


def _base(check: FreshnessCheck) -> dict:
    return {
        "id": check.id,
        "execution_id": check.execution_id,
        "input_text": check.input_text,
        "question": check.question,
        "freshness_score": check.freshness_score,
        "verdict": check.verdict,
        "extraction": check.extraction,
        "mock_mode": check.mock_mode,
        "source": check.source,
        "model": check.model,
        "guard_action": check.guard_action,
        "latency_ms": check.latency_ms,
        "created_at": check.created_at,
        "counts": _counts(check),
    }


def check_summary(check: FreshnessCheck) -> FreshnessCheckSummary:
    return FreshnessCheckSummary.model_validate(_base(check))


def check_out(check: FreshnessCheck) -> FreshnessCheckOut:
    claims = [ClaimOut.model_validate(c) for c in check.claims]
    return FreshnessCheckOut.model_validate({**_base(check), "claims": claims})
