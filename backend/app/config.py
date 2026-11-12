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

    # Judges are deliberately NOT the benchmarked agents' models (OpenAI gpt-oss, Qwen, NVIDIA Nemotron,
    # IBM Granite): a model grading its own family's answers is biased. They are strong frontier reasoners
    # from other labs, verified to grade a known pass and a known fail correctly (docs/statistics.md).
    judge_model: str = "openrouter/anthropic/claude-sonnet-5.5"
    """Model used by the `judge` scorer (live mode only)."""

    jury_models: str = (
        "openrouter/anthropic/claude-opus-5.5,openrouter/google/gemini-3.1-pro-preview,"
        "openrouter/deepseek/deepseek-v4-pro"
    )
    """Comma-separated judge models for the `jury` scorer: three labs (Anthropic, Google, DeepSeek), none of
    them used by the default agents. Add your own connections (``custom/<slug>/<model>``) if you like."""

    @property
    def jury(self) -> list[str]:
        return list(dict.fromkeys(m.strip() for m in self.jury_models.split(",") if m.strip()))

    freshness_model: str = "openrouter/anthropic/claude-sonnet-5.5"
    """Model that extracts claims and judges them against Tavily sources (stale-answer detector)."""

    freshness_fallback_models: str = (
        "groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b,openrouter/nvidia/nemotron-3.5-lightning:free"
    )
    """Comma-separated models tried in order when the main one is rate-limited or failing (each Groq
    model has its own daily token quota; the OpenRouter free model is a different provider altogether)."""

    @property
    def freshness_models(self) -> list[str]:
        extra = [m.strip() for m in self.freshness_fallback_models.split(",") if m.strip()]
        return list(dict.fromkeys([self.freshness_model, *extra]))

    guard_auth: str = "open"
    """Freshness Guard: ``open`` (anonymous callers allowed; presented credentials still verified) or
    ``required`` (every call needs a Guard API key, an OIDC ID token or the access code)."""

    guard_anonymous_rpm: int = 60
    """Requests per minute for anonymous Guard callers (keys carry their own limit)."""

    guard_oidc_issuer: str = ""
    """OIDC issuer URL (e.g. https://accounts.google.com): enables ID tokens as Guard credentials."""

    guard_oidc_audience: str = ""
    """Expected ``aud`` of ID tokens (your app's client id at the identity provider)."""

    guard_oidc_scopes_claim: str = "scope"
    """Claim holding the caller's Guard scopes (a space-separated string or a list, e.g. ``roles``)."""

    guard_oidc_default_scopes: str = "guard:annotate"
    """Scopes for a valid ID token whose claim names none."""

    guard_oidc_rpm: int = 60

    web_search_via: str = "bifrost-mcp"
    """``bifrost-mcp``: web searches go to Tavily's MCP server through Bifrost (falling back to the REST API
    if MCP is unavailable); ``rest``: straight to Tavily's REST API."""

    mcp_search_tool: str = "tavily-tavily_search"
    """Bifrost's name for the Tavily search tool: ``<MCP client>-<tool>``."""

    chaos_token: str = "agentpulse-chaos"
    """Shared with Bifrost (the chaos provider's key) so only Bifrost can call the outage-drill endpoint."""

    freshbench_per_cell: int = 2
    """Live Freshness Bench: questions per category × age bucket (6 × 6 cells → up to 72 questions)."""

    freshbench_pulse_hour_utc: int | None = None
    """Pulse: generate today's Freshness Bench and run it every day at this UTC hour (unset = off).
    Live mode spends Tavily credits (one basic search per cell) and model calls."""

    freshbench_pulse_agents: str = "2,4"
    """Agent ids the pulse runs (default: GPT-OSS 120B with and without tools)."""

    freshbench_drift_points: float = 10.0
    """Alert when an agent's Freshness Bench accuracy drops by more than this many points."""

    @field_validator("freshbench_pulse_hour_utc", mode="before")
    @classmethod
    def _pulse_hour(cls, v: object) -> object:
        """Empty (compose passes "" when unset), "off" or "none" turn the pulse off; otherwise 0–23."""
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "off", "none", "false")):
            return None
        hour = int(v)
        if not 0 <= hour <= 23:
            raise ValueError("FRESHBENCH_PULSE_HOUR_UTC must be an hour from 0 to 23 (or empty for off)")
        return hour

    @property
    def pulse_agent_ids(self) -> list[int]:
        return [int(x) for x in self.freshbench_pulse_agents.split(",") if x.strip().isdigit()]

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
