"""The chaos provider behind outage drills (called by Bifrost, not by people).

``bifrost/config.json`` registers a custom OpenAI-compatible provider ``chaos`` whose base URL is this router.
Bifrost sends ``chaos/r40/groq/openai/gpt-oss-20b`` here as model ``r40/groq/openai/gpt-oss-20b``: 40% of the
calls get a 503 (a simulated provider outage), the rest are forwarded back through Bifrost to the real model.
With a request-level ``fallbacks`` list, Bifrost itself retries a failed call on the fallback model, which is
what an outage drill measures. Only Bifrost (which holds ``CHAOS_TOKEN`` as the provider key) may call it.
"""

from __future__ import annotations

import random
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import chaos_token
from app.clients.bifrost_admin import BifrostAdminError
from app.clients.chaos import OUTAGE_MESSAGE, parse_chaos
from app.config import Settings, get_settings
from app.connection_routes import get_admin
from app.db import get_db
from app.mode import require_admin

router = APIRouter(tags=["outage drills (internal)"], include_in_schema=False)
router_public = APIRouter(tags=["outage drills"])


def _authorized(db: Session, settings: Settings, authorization: str | None) -> bool:
    token = (authorization or "").removeprefix("Bearer ").strip()
    return chaos_token.matches(db, settings, token)


def _error(status: int, message: str, kind: str = "server_error") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": kind}})


@router.post("/chaos/v1/chat/completions")
@router.post("/chaos/chat/completions")
async def chaos_chat(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
):
    if not _authorized(db, settings, authorization):
        return _error(401, "the chaos provider only answers Bifrost", "authentication_error")
    body: dict[str, Any] = await request.json()
    parsed = parse_chaos(str(body.get("model") or ""))
    if parsed is None:
        return _error(400, "chaos model ids look like r40/<provider>/<model>", "invalid_request_error")
    rate, real = parsed
    if random.random() < rate:
        return _error(503, OUTAGE_MESSAGE)
    upstream = {k: v for k, v in body.items() if k not in ("fallbacks", "stream")} | {"model": real}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        try:
            res = await client.post(f"{settings.bifrost_url}/v1/chat/completions", json=upstream)
        except httpx.HTTPError as exc:
            return _error(502, f"chaos: cannot reach Bifrost: {type(exc).__name__}")
    try:
        return JSONResponse(status_code=res.status_code, content=res.json())
    except ValueError:
        return _error(502, "chaos: non-JSON reply from Bifrost")


@router.get("/chaos/v1/models")
@router.get("/chaos/models")
def chaos_models() -> dict:
    return {"object": "list", "data": []}  # drills address models explicitly; nothing to list


# ------------------------------------------------------------------------------------ the token itself
class TokenIn(BaseModel):
    token: str | None = Field(
        default=None,
        min_length=chaos_token.MIN_LENGTH,
        max_length=200,
        description="Your own token; omit to generate a random one.",
    )


@router_public.get("/chaos/token", summary="Status of the outage-drill token (never the token itself)")
def token_status(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    return chaos_token.status(db, settings, get_admin(settings))


@router_public.post(
    "/chaos/token",
    summary="Create (random or your own) and activate the outage-drill token",
    dependencies=[Depends(require_admin)],
)
def set_token(
    body: TokenIn | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        return chaos_token.set_token(db, settings, get_admin(settings), body.token if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BifrostAdminError as exc:
        code = 409 if "locked" in str(exc) else 502
        raise HTTPException(status_code=code, detail=f"{exc}. The previous token is still active.") from exc


@router_public.delete(
    "/chaos/token",
    summary="Go back to the CHAOS_TOKEN setting",
    dependencies=[Depends(require_admin)],
)
def reset_token(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    try:
        return chaos_token.reset(db, settings, get_admin(settings))
    except BifrostAdminError as exc:
        raise HTTPException(status_code=502, detail=f"{exc}. The dashboard token is still active.") from exc
