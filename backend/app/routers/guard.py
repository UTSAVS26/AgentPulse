"""Freshness Guard: ``/guard/v1/chat/completions`` and ``/guard/v1/models`` (OpenAI-compatible), plus stats.

Point any OpenAI SDK at ``<backend>/guard/v1``. The API key is ignored locally; on a public deployment with
``LIVE_ACCESS_CODE`` it is the access code (live mode), and ``mock`` (or no key) means mock mode.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.clients import clients_for
from app.clients.llm import LLMError
from app.config import Settings, get_settings
from app.db import get_db
from app.guard.service import MODES, Guard, completion_json, sse_chunks
from app.mode import ACCESS_CODE_HEADER, resolve_live
from app.models import FreshnessCheck

router = APIRouter(tags=["freshness guard"])
GUARD_HEADER = "X-AgentPulse-Guard"
NO_CODE = {"", "mock", "none", "sk-mock", "not-needed"}


def openai_error(status: int, message: str, kind: str = "invalid_request_error", code: str | None = None):
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": kind, "code": code}}
    )


def _live(settings: Settings, authorization: str | None, x_access_code: str | None) -> bool:
    code = x_access_code
    if not code and authorization and settings.live_access_code:
        token = authorization.removeprefix("Bearer ").removeprefix("bearer ").strip()
        code = None if token.lower() in NO_CODE else token
    return resolve_live(settings, code)


def _llm_error(exc: LLMError) -> JSONResponse:
    status = exc.status_code or 502
    if status == 429:
        return openai_error(429, str(exc), "rate_limit_error", "rate_limited")
    if status in (400, 404, 422):
        return openai_error(status, str(exc), "invalid_request_error", exc.kind or None)
    if exc.kind == "timeout":
        return openai_error(504, str(exc), "api_error", "timeout")
    return openai_error(502, str(exc), "api_error", exc.kind or "upstream_error")


@router.post(
    "/guard/v1/chat/completions", summary="OpenAI-compatible chat completion, fact-checked for freshness"
)
async def chat_completions(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
    x_agentpulse_guard: str | None = Header(default=None, alias=GUARD_HEADER),
):
    try:
        body: dict[str, Any] = await request.json()
    except ValueError:
        return openai_error(400, "the request body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body.get("model"):
        return openai_error(400, "'model' is required (e.g. groq/openai/gpt-oss-120b)", code="missing_model")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        return openai_error(400, "'messages' must be a non-empty list of messages", code="missing_messages")
    mode = (x_agentpulse_guard or body.pop("guard", None) or "annotate").strip().lower()
    if mode not in MODES:
        return openai_error(
            400, f"unknown guard mode '{mode}'; use one of {list(MODES)}", code="bad_guard_mode"
        )
    if (body.get("n") or 1) != 1:
        return openai_error(400, "only n=1 is supported", code="unsupported_n")
    try:
        live = _live(settings, authorization, x_access_code)
    except HTTPException as exc:
        return openai_error(exc.status_code, str(exc.detail), "authentication_error", "invalid_access_code")

    llm, search = clients_for(live)
    guard = Guard(llm, search, settings, live=live)

    def work():  # model call + check: seconds of blocking I/O, so off the event loop
        res = guard.complete(body)
        return res, guard.guard(db, res, messages, mode)

    try:
        res, outcome = await run_in_threadpool(work)
    except LLMError as exc:
        return _llm_error(exc)
    completion = completion_json(res, outcome.content, body["model"])
    info = {**outcome.agentpulse, "mode": "live" if live else "mock"}
    completion["agentpulse"] = info
    headers = {
        "X-AgentPulse-Guard": mode,
        "X-AgentPulse-Action": outcome.action,
        "X-AgentPulse-Verdict": str(info.get("verdict") or "unchecked"),
        "X-AgentPulse-Freshness": ""
        if info.get("freshness_score") is None
        else f"{info['freshness_score']:.2f}",
        "X-AgentPulse-Check-Id": str(info.get("check_id") or ""),
        "X-AgentPulse-Mode": info["mode"],
    }
    if body.get("stream"):
        return StreamingResponse(
            sse_chunks(completion, info), media_type="text/event-stream", headers=headers
        )
    return JSONResponse(completion, headers=headers)


@router.get("/guard/v1/models", summary="Models the Guard can forward to (OpenAI format)")
def list_models(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
):
    try:
        live = _live(settings, authorization, x_access_code)
    except HTTPException as exc:
        return openai_error(exc.status_code, str(exc.detail), "authentication_error", "invalid_access_code")
    llm, _ = clients_for(live)
    try:
        ids = llm.list_models()
    except LLMError as exc:
        return _llm_error(exc)
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": m.split("/", 1)[0]} for m in ids],
    }


@router.get("/guard/stats", summary="Guarded calls per model: verdicts and actions")
def stats(db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(FreshnessCheck.model, FreshnessCheck.verdict, FreshnessCheck.guard_action, func.count())
        .where(FreshnessCheck.source == "guard")
        .group_by(FreshnessCheck.model, FreshnessCheck.verdict, FreshnessCheck.guard_action)
    ).all()
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"checked": 0, "verdicts": {}, "actions": {}})
    for model, verdict, action, n in rows:
        m = per[model or "(unknown)"]
        m["checked"] += n
        m["verdicts"][verdict or "?"] = m["verdicts"].get(verdict or "?", 0) + n
        m["actions"][action or "?"] = m["actions"].get(action or "?", 0) + n
    models = []
    for model, m in sorted(per.items(), key=lambda kv: -kv[1]["checked"]):
        flagged = m["verdicts"].get("stale", 0) + m["verdicts"].get("wrong", 0)
        models.append({"model": model, **m, "flagged": flagged, "flagged_rate": flagged / m["checked"]})
    return {"models": models, "checked": sum(m["checked"] for m in models)}
