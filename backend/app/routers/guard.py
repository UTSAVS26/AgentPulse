"""Freshness Guard: ``/guard/v1/chat/completions`` and ``/guard/v1/models`` (OpenAI-compatible), stats, keys.

Point any OpenAI SDK at ``<backend>/guard/v1``; the SDK's ``api_key`` is the credential: a Guard API key
(``apg_…``), an OIDC ID token, or the access code. Who may do what is decided in :mod:`app.guard.auth`.
"""

from __future__ import annotations

import hmac
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.clients import clients_for
from app.clients.llm import LLMError
from app.config import Settings, get_settings
from app.db import get_db
from app.guard import auth
from app.guard.service import MODES, Guard, completion_json, sse_chunks
from app.mode import ACCESS_CODE_HEADER, is_public_deployment
from app.models import FreshnessCheck, GuardKey

router = APIRouter(tags=["freshness guard"])
GUARD_HEADER = "X-AgentPulse-Guard"
ERROR_TYPES = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}


def openai_error(
    status: int, message: str, kind: str = "invalid_request_error", code: str | None = None, headers=None
):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": kind, "code": code}},
        headers=headers,
    )


def _auth_error(exc: auth.AuthError) -> JSONResponse:
    return openai_error(
        exc.status, exc.message, ERROR_TYPES.get(exc.status, "api_error"), exc.code, exc.headers()
    )


def _caller(
    db: Session, settings: Settings, authorization: str | None, x_access_code: str | None, scope: str | None
) -> auth.Principal:
    principal = auth.resolve(db, settings, authorization, x_access_code)
    if scope:
        principal.require(scope)
    auth.limiter.check(principal)
    return principal


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
        principal = await run_in_threadpool(
            _caller, db, settings, authorization, x_access_code, auth.MODE_SCOPE[mode]
        )
    except auth.AuthError as exc:
        return _auth_error(exc)
    live = auth.wants_live(principal, settings)

    llm, search = clients_for(live)
    guard = Guard(llm, search, settings, live=live)

    def work():  # model call + check: seconds of blocking I/O, so off the event loop
        res = guard.complete(body)
        return res, guard.guard(db, res, messages, mode, caller=principal.caller)

    try:
        res, outcome = await run_in_threadpool(work)
    except LLMError as exc:
        return _llm_error(exc)
    completion = completion_json(res, outcome.content, body["model"])
    info = {**outcome.agentpulse, "mode": "live" if live else "mock", "caller": principal.caller}
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
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
):
    try:
        principal = _caller(db, settings, authorization, x_access_code, None)
    except auth.AuthError as exc:
        return _auth_error(exc)
    llm, _ = clients_for(auth.wants_live(principal, settings))
    try:
        ids = llm.list_models()
    except LLMError as exc:
        return _llm_error(exc)
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": m.split("/", 1)[0]} for m in ids],
    }


@router.get("/guard/whoami", summary="Who the presented credential is and what it may do")
def whoami(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
):
    try:
        principal = auth.resolve(db, settings, authorization, x_access_code)
    except auth.AuthError as exc:
        return _auth_error(exc)
    return {
        **principal.describe(),
        "live": auth.wants_live(principal, settings),
        "guard_auth": settings.guard_auth,
        "oidc": bool(settings.guard_oidc_issuer and settings.guard_oidc_audience),
    }


@router.get("/guard/stats", summary="Guarded calls per model: verdicts and actions")
def stats(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
):
    try:
        _caller(db, settings, authorization, x_access_code, "guard:stats")
    except auth.AuthError as exc:
        return _auth_error(exc)
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


# ------------------------------------------------------------------------------------ key management
def guard_admin(
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Issuing and revoking keys needs the access code whenever one is configured (also locally); without one
    (a private machine) whoever runs the stack is the admin."""
    code = settings.live_access_code
    if code and not (x_access_code and hmac.compare_digest(x_access_code.encode(), code.encode())):
        raise HTTPException(status_code=403, detail="managing Guard API keys needs the access code")
    if not code and is_public_deployment(settings):  # pragma: no cover - public implies a code
        raise HTTPException(status_code=403, detail="managing Guard API keys needs the access code")


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["support-bot"])
    scopes: list[str] = Field(default_factory=lambda: ["guard:annotate"], examples=[["guard:annotate"]])
    expires_in_days: int | None = Field(default=90, ge=1, le=3650, description="null = never expires")
    rpm: int = Field(default=60, ge=1, le=10_000, description="Requests per minute for this key.")


def _key_out(k: GuardKey) -> dict[str, Any]:
    def iso(dt):
        return (
            None
            if dt is None
            else (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat(timespec="seconds")
        )

    expired = k.expires_at is not None and auth._aware(k.expires_at) <= datetime.now(UTC)
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "scopes": k.scopes,
        "rpm": k.rpm,
        "created_at": iso(k.created_at),
        "expires_at": iso(k.expires_at),
        "last_used_at": iso(k.last_used_at),
        "revoked_at": iso(k.revoked_at),
        "status": "revoked" if k.revoked_at else ("expired" if expired else "active"),
    }


@router.post(
    "/guard/keys",
    status_code=201,
    summary="Issue a Guard API key (the secret is shown once)",
    dependencies=[Depends(guard_admin)],
)
def create_key(body: KeyCreate, db: Session = Depends(get_db)) -> dict:
    try:
        key, secret = auth.issue_key(
            db, name=body.name, scopes=body.scopes, expires_in_days=body.expires_in_days, rpm=body.rpm
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**_key_out(key), "key": secret, "note": "Store this key now: it is not shown again."}


@router.get("/guard/keys", summary="Guard API keys (never the secrets)", dependencies=[Depends(guard_admin)])
def list_keys(db: Session = Depends(get_db)) -> list[dict]:
    return [_key_out(k) for k in db.scalars(select(GuardKey).order_by(GuardKey.id.desc()))]


@router.delete(
    "/guard/keys/{key_id}",
    status_code=204,
    summary="Revoke a Guard API key",
    dependencies=[Depends(guard_admin)],
)
def revoke_key(key_id: int, db: Session = Depends(get_db)) -> Response:
    key = db.get(GuardKey, key_id)
    if key is None:
        raise HTTPException(status_code=404, detail=f"key {key_id} not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        db.commit()
    return Response(status_code=204)
