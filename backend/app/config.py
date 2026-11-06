"""Application settings, loaded from environment variables (and `.env` when present)."""

from functools import lru_cache

from pydantic import field_validator
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

    live_access_code: str = ""
    """If set while MOCK_MODE=true, requests with header `X-Access-Code: <code>` run live (public demo)."""

    judge_model: str = "groq/openai/gpt-oss-120b"
    """Model used by the `judge` scorer (live mode only)."""

    freshness_model: str = "groq/openai/gpt-oss-120b"
    """Model that extracts claims and judges them against Tavily sources (stale-answer detector)."""

    freshness_fallback_models: str = (
        "groq/openai/gpt-oss-20b,groq/qwen/qwen3.8-27b,openrouter/nvidia/nemotron-3.5-lightning:free"
    )
    """Comma-separated models tried in order when the main one is rate-limited or failing (each Groq
    model has its own daily token quota; the OpenRouter free model is a different provider altogether)."""

    @property
    def freshness_models(self) -> list[str]:
        extra = [m.strip() for m in self.freshness_fallback_models.split(",") if m.strip()]
        return list(dict.fromkeys([self.freshness_model, *extra]))

    freshness_search_depth: str = "advanced"
    """Tavily depth for the automatic checks during benchmark runs (advanced = 2 credits per search,
    much better dated sources than basic). Results are cached per run, so each distinct claim is
    searched once."""

    bifrost_setup_token: str = ""
    """Bifrost's setup token (``BIFROST_SETUP_TOKEN``, shared with the Bifrost container). Unlocks its
    management API so hosted providers can be registered at runtime. Empty = hosted connections are
    called directly."""

    app_secret: str = ""
    """Encrypts stored API keys of model connections. If empty, a random secret is generated once and kept
    in ``app_secret_file`` (keys saved under one secret can't be read with another)."""

    app_secret_file: str = ""
    """Where the generated secret is kept when ``APP_SECRET`` is empty (default: the temp directory)."""

    allow_private_endpoints: bool | None = None
    """Allow model connections to private/loopback addresses (Ollama, LM Studio on your machine). Default:
    allowed locally, blocked on the public deployment (SSRF protection)."""

    localhost_alias: str = ""
    """In Docker, ``localhost`` in a connection URL means the container itself. Set to
    ``host.docker.internal`` (compose does) so ``http://localhost:11434/v1`` reaches the host."""

    seed_on_startup: bool = True
    """Insert the default demo agents on startup (existing agents are never modified)."""

    seed_demo_data: bool = True
    """When the database has no runs, create two mock demo runs and two example freshness checks
    (requires ``seed_on_startup``)."""

    mock_seed: int = 42
    """Seed for the mock LLM/search so demo runs are reproducible."""

    mock_latency_scale: float = 0.25
    """Multiplier on simulated latency (1.0 = realistic, 0 = instant). Reported latency is unscaled."""

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        """Hosted Postgres providers (Neon, Supabase…) hand out ``postgres://`` or
        ``postgresql://`` URLs; SQLAlchemy needs the psycopg (v3) driver name."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix) :]
        return v


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
