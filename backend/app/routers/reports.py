"""Report downloads for a benchmark run (Markdown, CSV, JSON)."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BenchmarkRun
from app.reports import build_report, to_csv, to_markdown

router = APIRouter(tags=["reports"])

MEDIA = {"md": "text/markdown; charset=utf-8", "csv": "text/csv; charset=utf-8", "json": "application/json"}


@router.get(
    "/reports/{run_id}",
    summary="Download a benchmark report",
    description=(
        "`format=md`: a readable report (highlights, leaderboard, reliability detail, accuracy by category, "
        "pass rate per task, freshness and stale answers, failed executions). "
        "`format=csv`: `table=leaderboard` (one row per agent) or `table=executions` (one row per attempt). "
        "`format=json`: everything, including every execution. Numbers are identical to "
        "`GET /runs/{id}/metrics`. Add `download=false` to view inline."
    ),
    responses={200: {"content": {m: {} for m in MEDIA.values()}}},
)
def get_report(
    run_id: int,
    format: Literal["md", "csv", "json"] = Query(default="md"),
    table: Literal["leaderboard", "executions"] = Query(default="leaderboard"),
    download: bool = True,
    db: Session = Depends(get_db),
) -> Response:
    run = db.get(BenchmarkRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    report = build_report(db, run)
    if format == "md":
        body, name = to_markdown(report), f"agentpulse-run-{run_id}.md"
    elif format == "csv":
        body, name = to_csv(report, table), f"agentpulse-run-{run_id}-{table}.csv"
    else:
        body, name = json.dumps(report, indent=2, ensure_ascii=False), f"agentpulse-run-{run_id}.json"
    headers = {"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{name}"'}
    return Response(content=body, media_type=MEDIA[format], headers=headers)
