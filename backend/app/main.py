"""FastAPI entrypoint for the AgentPulse backend."""

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from app import __version__
from app.config import Settings, get_settings

app = FastAPI(
    title="AgentPulse API",
    version=__version__,
    description=(
        "Benchmark AI agents on identical tasks (accuracy, reliability, tokens, cost, latency) "
        "and detect stale answers with live Tavily verification. LLM traffic goes through Bifrost."
    ),
)


class HealthResponse(BaseModel):
    """Liveness information about the backend."""

    status: str = Field(examples=["ok"])
    version: str = Field(examples=[__version__])
    mock_mode: bool = Field(description="True when LLM and search calls are simulated.")


@app.get("/health", response_model=HealthResponse, summary="Backend health check", tags=["system"])
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Return backend liveness and whether mock mode is active."""
    return HealthResponse(status="ok", version=__version__, mock_mode=settings.mock_mode)
