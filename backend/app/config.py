"""Application settings, loaded from environment variables (and `.env` when present)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the backend.

    Every field can be overridden by an environment variable of the same name
    (case-insensitive), e.g. ``MOCK_MODE=false``.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mock_mode: bool = True
    """When true, LLM and search calls are served by deterministic fakes."""

    bifrost_url: str = "http://localhost:8080"
    """Base URL of the Bifrost gateway (without the ``/v1`` suffix)."""

    tavily_api_key: str = ""

    database_url: str = "postgresql+psycopg://agentpulse:agentpulse@localhost:5432/agentpulse"

    llm_timeout_s: float = 120.0
    search_timeout_s: float = 20.0
    health_timeout_s: float = 3.0

    judge_model: str = "groq/openai/gpt-oss-120b"
    """Model used by the `judge` scorer (live mode only)."""

    seed_on_startup: bool = True
    """Insert the default demo agents on startup (existing agents are never modified)."""

    mock_seed: int = 42
    """Seed for the mock LLM/search so demo runs are reproducible."""

    mock_latency_scale: float = 0.25
    """Multiplier on simulated latency (1.0 = realistic, 0 = instant). Reported latency is unscaled."""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
