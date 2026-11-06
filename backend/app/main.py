"""FastAPI entrypoint for the AgentPulse backend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import __version__
from app.bench.engine import get_bench_engine, mark_interrupted_runs
from app.clients import LLMError
from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db, ping_db
from app.mode import ACCESS_CODE_HEADER, Mode, get_mode, is_public_deployment, live_available
from app.routers import agents as agents_router
from app.routers import connections as connections_router
from app.routers import freshness as freshness_router
from app.routers import reports as reports_router
from app.routers import runs as runs_router
from app.routers import suites as suites_router
from app.seed import seed_agents, seed_suites
from app.seed.demo import seed_demo_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    settings = get_settings()
    with get_sessionmaker()() as db:
        mark_interrupted_runs(db)
        if settings.seed_on_startup:
            seed_agents(db)
            seed_suites(db)
            if settings.seed_demo_data:
                seed_demo_data(db, get_bench_engine(), settings)
    yield


app = FastAPI(
    title="AgentPulse API",
    version=__version__,
    lifespan=lifespan,
    description=(
        "Benchmark AI agents on identical tasks (accuracy, reliability, tokens, cost, latency) "
        "and detect stale answers with live Tavily verification. LLM traffic goes through Bifrost."
    ),
)


app.include_router(agents_router.router)
app.include_router(connections_router.router)
app.include_router(suites_router.router)
app.include_router(runs_router.router)
app.include_router(freshness_router.router)
app.include_router(reports_router.router)


class ComponentStatus(BaseModel):
    ok: bool
    detail: str = Field(examples=["ok"])


class HealthResponse(BaseModel):
    """Backend health, including each dependency."""

    status: Literal["ok", "degraded"] = Field(
        description="`ok` when every dependency needed for the current mode is healthy.",
        examples=["ok"],
    )
    version: str = Field(examples=[__version__])
    mock_mode: bool = Field(description="True when THIS request is served by the simulated LLM/search.")
    live_available: bool = Field(
        description="Whether live mode can be used (always locally with MOCK_MODE=false; with the "
        f"`{ACCESS_CODE_HEADER}` header on public deployments)."
    )
    public_deployment: bool = Field(description="Public demo: mock by default; live and admin need a code.")
    components: dict[str, ComponentStatus] = Field(
        description="`database`, `llm` (Bifrost or mock) and `search` (Tavily or mock)."
    )
    connections: dict[str, int] = Field(
        default_factory=dict,
        description="Your model connections by last test result: `total`, `ok`, `failing`, `untested`, "
        "`via_bifrost`. Informational: a failing connection doesn't make the backend degraded.",
    )


@app.get("/health", response_model=HealthResponse, summary="Backend health check", tags=["system"])
def health(
    settings: Settings = Depends(get_settings),
    mode: Mode = Depends(get_mode),
) -> HealthResponse:
    """Check the database, the LLM gateway and the search provider for this request's mode.

    Fast and free: the search check only verifies that Tavily is configured (no credits spent).
    Send `X-Access-Code` to check (and validate the code for) live mode.
    """
    checks = {"database": ping_db(), "llm": mode.llm.ping(), "search": mode.search.ping()}
    components = {name: ComponentStatus(ok=ok, detail=detail) for name, (ok, detail) in checks.items()}
    status = "ok" if all(c.ok for c in components.values()) else "degraded"
    return HealthResponse(
        status=status,
        version=__version__,
        mock_mode=mode.mock,
        live_available=live_available(settings),
        public_deployment=is_public_deployment(settings),
        components=components,
        connections=_connection_summary(),
    )


def _connection_summary() -> dict[str, int]:
    from sqlalchemy import select

    from app.models import ModelConnection

    try:
        with get_sessionmaker()() as db:
            rows = db.execute(select(ModelConnection.last_check_ok, ModelConnection.route)).all()
    except Exception:  # the database check above already reports a broken database
        return {}
    return {
        "total": len(rows),
        "ok": sum(ok is True for ok, _ in rows),
        "failing": sum(ok is False for ok, _ in rows),
        "untested": sum(ok is None for ok, _ in rows),
        "via_bifrost": sum(route == "bifrost" for _, route in rows),
    }


class ModelsResponse(BaseModel):
    mock_mode: bool
    models: list[str] = Field(examples=[["groq/openai/gpt-oss-20b", "ollama/granite4:3b"]])


@app.get("/models", response_model=ModelsResponse, summary="List routable models", tags=["system"])
def list_models(mode: Mode = Depends(get_mode)) -> ModelsResponse:
    """Every `provider/model` id that Bifrost can route to (or the mock's model list in mock mode)."""
    try:
        models = mode.llm.list_models()
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ModelsResponse(mock_mode=mode.mock, models=models)
