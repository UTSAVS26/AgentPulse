"""FastAPI entrypoint for the AgentPulse backend."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import __version__
from app.clients import LLMClient, LLMError, SearchClient, get_llm_client, get_search_client
from app.config import Settings, get_settings
from app.db import get_sessionmaker, init_db, ping_db
from app.routers import agents as agents_router
from app.seed import seed_agents

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if get_settings().seed_on_startup:
        with get_sessionmaker()() as db:
            seed_agents(db)
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
    mock_mode: bool = Field(description="True when LLM and search calls are simulated.")
    components: dict[str, ComponentStatus] = Field(
        description="`database`, `llm` (Bifrost or mock) and `search` (Tavily or mock)."
    )


@app.get("/health", response_model=HealthResponse, summary="Backend health check", tags=["system"])
def health(
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm_client),
    search: SearchClient = Depends(get_search_client),
) -> HealthResponse:
    """Check the database, the LLM gateway and the search provider.

    Fast and free: the search check only verifies that Tavily is configured (no credits spent).
    """
    checks = {"database": ping_db(), "llm": llm.ping(), "search": search.ping()}
    components = {name: ComponentStatus(ok=ok, detail=detail) for name, (ok, detail) in checks.items()}
    status = "ok" if all(c.ok for c in components.values()) else "degraded"
    return HealthResponse(
        status=status, version=__version__, mock_mode=settings.mock_mode, components=components
    )


class ModelsResponse(BaseModel):
    mock_mode: bool
    models: list[str] = Field(examples=[["groq/openai/gpt-oss-20b", "ollama/granite4:3b"]])


@app.get("/models", response_model=ModelsResponse, summary="List routable models", tags=["system"])
def list_models(
    settings: Settings = Depends(get_settings), llm: LLMClient = Depends(get_llm_client)
) -> ModelsResponse:
    """Every `provider/model` id that Bifrost can route to (or the mock's model list in mock mode)."""
    try:
        models = llm.list_models()
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ModelsResponse(mock_mode=settings.mock_mode, models=models)
