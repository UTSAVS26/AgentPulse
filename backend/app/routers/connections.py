"""Model connections: bring your own model (any OpenAI-compatible endpoint, self-hosted or hosted).

Security:
* API keys are encrypted at rest (``app.crypto``) and never returned: responses carry ``key_hint`` only.
* Base URLs go through ``app.net_guard`` (scheme/credentials checks; private addresses blocked on the public
  deployment).
* On the public deployment, creating, editing, deleting and testing connections need the access code
  (``require_admin``): those actions make the server call arbitrary URLs and store keys.
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import connection_routes, crypto, net_guard
from app.agents.tools import TOOLS
from app.clients.bifrost_admin import BUILTIN_PROVIDERS, NATIVE_PROVIDERS
from app.clients.llm import LLMError, OpenAICompatibleClient
from app.clients.router import CUSTOM_PREFIX, BifrostRoute, build_client, custom_model_id
from app.config import Settings, get_settings
from app.db import get_db
from app.mode import require_admin
from app.models import Agent, ModelConnection, utcnow
from app.presets import PRESETS
from app.schemas import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionTestIn,
    ConnectionTestModel,
    ConnectionTestOut,
    ConnectionUpdate,
    PresetOut,
)

router = APIRouter(tags=["connections"])
TEST_TIMEOUT_S = 45.0


# ------------------------------------------------------------------------------------ helpers
def _agents_using(db: Session, slug: str) -> list[str]:
    prefix = f"{CUSTOM_PREFIX}{slug}/"
    return [
        a.name for a in db.scalars(select(Agent).where(Agent.model.startswith(prefix)).order_by(Agent.id))
    ]


def _out(db: Session, c: ModelConnection) -> ConnectionOut:
    return ConnectionOut(
        id=c.id,
        name=c.name,
        slug=c.slug,
        kind=c.kind,
        route=c.route,
        provider=c.provider,
        route_reason=c.route_reason,
        bifrost_key_status=c.bifrost_key_status,
        preset=c.preset,
        base_url=c.base_url,
        has_api_key=bool(c.api_key_enc),
        key_hint=c.key_hint,
        models=c.models or [],
        model_ids=[custom_model_id(c.slug, m) for m in (c.models or [])],
        discovered_models=c.discovered_models,
        supports_tools=c.supports_tools,
        price_input_per_m=c.price_input_per_m,
        price_output_per_m=c.price_output_per_m,
        last_check_ok=c.last_check_ok,
        last_check_detail=c.last_check_detail,
        last_check_at=c.last_check_at,
        agents=_agents_using(db, c.slug),
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _get(db: Session, connection_id: int) -> ModelConnection:
    c = db.get(ModelConnection, connection_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"connection {connection_id} not found")
    return c


def _checked_url(url: str, settings: Settings) -> net_guard.CheckedURL:
    try:
        return net_guard.check(url, settings)
    except net_guard.UnsafeURL as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _kind_and_provider(body) -> tuple[str, str | None]:
    """``via_bifrost`` + a known provider makes a *hosted* connection (registered in Bifrost if possible)."""
    provider = (body.provider or "").strip().lower() or None
    if provider and provider not in NATIVE_PROVIDERS | BUILTIN_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown provider '{provider}'; known: {sorted(NATIVE_PROVIDERS | BUILTIN_PROVIDERS)}",
        )
    return ("hosted" if (body.via_bifrost and provider) else "openai_compatible"), provider


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "connection"


def _unique_slug(db: Session, base: str) -> str:
    slug, n = base, 2
    while db.scalar(select(ModelConnection.id).where(ModelConnection.slug == slug)) is not None:
        suffix = f"-{n}"
        slug, n = base[: 40 - len(suffix)] + suffix, n + 1
    return slug


def _name_taken(db: Session, name: str, exclude: int | None = None) -> bool:
    q = select(ModelConnection.id).where(ModelConnection.name == name)
    if exclude is not None:
        q = q.where(ModelConnection.id != exclude)
    return db.scalar(q) is not None


def probe(client: OpenAICompatibleClient, model: str | None) -> dict:
    """List models, then (with a model) a one-word chat and a tool-calling check."""
    result: dict = {"model": model}
    try:
        result["models"] = client.list_models()
    except LLMError as exc:
        result["models_error"] = exc.message
    if model:
        started = time.perf_counter()
        try:
            res = client.chat(
                model, [{"role": "user", "content": "Reply with exactly the word: pong"}], max_tokens=512
            )
            result.update(
                chat_ok=True,
                chat_latency_ms=round((time.perf_counter() - started) * 1000, 1),
                chat_reply=(res.content or "").strip()[:200] or "(empty reply)",
            )
        except LLMError as exc:
            result.update(chat_ok=False, chat_error=str(exc)[:500])
        if result.get("chat_ok"):
            try:
                res = client.chat(
                    model,
                    [{"role": "user", "content": "Use the calculator tool to compute 17 * 23."}],
                    tools=[TOOLS["calculator"].definition()],
                    max_tokens=512,
                )
                result["tools_ok"] = bool(res.tool_calls)
                result["tools_detail"] = (
                    f"called {res.tool_calls[0].name}({res.tool_calls[0].raw_arguments[:80]})"
                    if res.tool_calls
                    else "answered without calling the tool: agents with tools may not work well"
                )
            except LLMError as exc:
                result.update(tools_ok=False, tools_detail=f"tool calling failed: {str(exc)[:300]}")
    result["ok"] = bool(result.get("chat_ok") or ("models" in result and not model))
    return result


def _test(base_url: str, api_key: str | None, model: str | None, settings: Settings, label: str) -> dict:
    checked = _checked_url(base_url, settings)
    client = build_client(
        checked.url, api_key, settings.model_copy(update={"llm_timeout_s": TEST_TIMEOUT_S}), label
    )
    return {**probe(client, model), "base_url": checked.url, "hint": checked.hint}


# ------------------------------------------------------------------------------------ endpoints
@router.get("/connections/presets", response_model=list[PresetOut], summary="Known endpoints to start from")
def list_presets() -> list[dict]:
    return [{**p, "via_bifrost": p["bifrost_provider"] in NATIVE_PROVIDERS} for p in PRESETS]


@router.get("/connections", response_model=list[ConnectionOut], summary="List model connections")
def list_connections(db: Session = Depends(get_db)) -> list[ConnectionOut]:
    return [_out(db, c) for c in db.scalars(select(ModelConnection).order_by(ModelConnection.id))]


@router.get("/connections/{connection_id}", response_model=ConnectionOut, summary="Get a model connection")
def get_connection(connection_id: int, db: Session = Depends(get_db)) -> ConnectionOut:
    return _out(db, _get(db, connection_id))


@router.post(
    "/connections",
    response_model=ConnectionOut,
    status_code=201,
    summary="Add a model connection",
    description=(
        "Any OpenAI-compatible endpoint: a base URL (the root containing `/chat/completions`), the model "
        "name(s) and an optional API key (encrypted at rest, never returned). Agents then use "
        "`custom/<slug>/<model>` as their model. Nothing is called until you test it or run an agent."
    ),
    dependencies=[Depends(require_admin)],
)
def create_connection(
    body: ConnectionCreate, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> ConnectionOut:
    if _name_taken(db, body.name):
        raise HTTPException(status_code=409, detail=f"a connection named '{body.name}' already exists")
    checked = _checked_url(body.base_url, settings)
    if body.slug and db.scalar(select(ModelConnection.id).where(ModelConnection.slug == body.slug)):
        raise HTTPException(status_code=409, detail=f"slug '{body.slug}' is already used")
    key = (body.api_key or "").strip() or None
    kind, provider = _kind_and_provider(body)
    conn = ModelConnection(
        name=body.name,
        slug=body.slug or _unique_slug(db, _slugify(body.name)),
        kind=kind,
        provider=provider,
        route="direct",
        preset=body.preset,
        base_url=checked.url,
        api_key_enc=crypto.encrypt(key) if key else None,
        key_hint=crypto.mask(key) if key else None,
        models=body.models,
        price_input_per_m=body.price_input_per_m,
        price_output_per_m=body.price_output_per_m,
    )
    db.add(conn)
    db.flush()
    connection_routes.apply(db, conn, settings)
    db.commit()
    db.refresh(conn)
    return _out(db, conn)


@router.put(
    "/connections/{connection_id}",
    response_model=ConnectionOut,
    summary="Edit a model connection",
    description="Omit `api_key` (or send null) to keep the stored key; `clear_api_key: true` removes it.",
    dependencies=[Depends(require_admin)],
)
def update_connection(
    connection_id: int,
    body: ConnectionUpdate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ConnectionOut:
    conn = _get(db, connection_id)
    if _name_taken(db, body.name, exclude=connection_id):
        raise HTTPException(status_code=409, detail=f"a connection named '{body.name}' already exists")
    checked = _checked_url(body.base_url, settings)
    conn.name, conn.preset, conn.base_url, conn.models = body.name, body.preset, checked.url, body.models
    conn.price_input_per_m, conn.price_output_per_m = body.price_input_per_m, body.price_output_per_m
    key = (body.api_key or "").strip()
    if body.clear_api_key:
        conn.api_key_enc, conn.key_hint = None, None
    elif key:
        conn.api_key_enc, conn.key_hint = crypto.encrypt(key), crypto.mask(key)
    conn.kind, conn.provider = _kind_and_provider(body)
    was_bifrost = conn.route == "bifrost"
    connection_routes.apply(db, conn, settings)
    if was_bifrost and conn.route != "bifrost":
        conn.route = "bifrost"  # unregister the old key, then record the new route
        connection_routes.unregister(conn, settings)
        connection_routes.apply(db, conn, settings)
    conn.updated_at = utcnow()
    db.commit()
    db.refresh(conn)
    return _out(db, conn)


@router.delete(
    "/connections/{connection_id}",
    status_code=204,
    summary="Delete a model connection (only if no agent uses it)",
    dependencies=[Depends(require_admin)],
)
def delete_connection(connection_id: int, db: Session = Depends(get_db)) -> Response:
    conn = _get(db, connection_id)
    users = _agents_using(db, conn.slug)
    if users:
        raise HTTPException(status_code=409, detail=f"used by agents {users}: change or delete them first")
    connection_routes.unregister(conn)
    db.delete(conn)
    db.commit()
    return Response(status_code=204)


@router.post(
    "/connections/test",
    response_model=ConnectionTestOut,
    summary="Check an endpoint before saving it",
    description=(
        "Lists the endpoint's models (`GET <base>/models`) and, with `model`, sends a one-word chat and "
        "checks tool calling. Nothing is stored."
    ),
    dependencies=[Depends(require_admin)],
)
def test_endpoint(body: ConnectionTestIn, settings: Settings = Depends(get_settings)) -> dict:
    return _test(body.base_url, (body.api_key or "").strip() or None, body.model, settings, "endpoint")


@router.post(
    "/connections/{connection_id}/test",
    response_model=ConnectionTestOut,
    summary="Test a saved connection (and record the result)",
    description="Uses the stored key. `model` defaults to the connection's first model.",
    dependencies=[Depends(require_admin)],
)
def test_connection(
    connection_id: int,
    body: ConnectionTestModel | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    conn = _get(db, connection_id)
    try:
        key = crypto.decrypt(conn.api_key_enc) if conn.api_key_enc else None
    except crypto.SecretError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    model = (body.model if body else None) or (conn.models[0] if conn.models else None)
    if connection_routes.wants_bifrost(conn):
        connection_routes.apply(db, conn, settings)  # (re)register: Bifrost may have restarted
    if conn.route == "bifrost":
        slug = conn.slug
        from app.clients import get_test_bifrost

        gateway = get_test_bifrost(TEST_TIMEOUT_S)  # test budget, not the 120 s LLM one
        via = BifrostRoute(gateway, conn.provider, heal=lambda: connection_routes.reregister(slug))
        result = {
            **probe(via, model),
            "base_url": f"bifrost → {conn.provider}",
            "hint": conn.bifrost_key_status,
        }
    else:
        result = _test(conn.base_url, key, model, settings, conn.name)
    if result.get("models") is not None:
        conn.discovered_models = result["models"][:500]
    if model and result.get("chat_ok"):
        conn.supports_tools = result.get("tools_ok")
    conn.last_check_ok = result["ok"]
    conn.last_check_detail = (
        f"{model}: {result.get('chat_latency_ms')} ms"
        + ("" if result.get("tools_ok") else " · no tool calling")
        if result.get("chat_ok")
        else (result.get("chat_error") or result.get("models_error") or "ok")
    )[:500]
    conn.last_check_at = utcnow()
    db.commit()
    return result
