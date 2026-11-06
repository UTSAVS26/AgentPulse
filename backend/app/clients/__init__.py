"""LLM and search clients, with real and mock implementations.

Both sets exist side by side so a single deployment can serve **mock** requests to the public and
**live** requests to holders of an access code (see :mod:`app.mode`).
"""

from __future__ import annotations

from functools import lru_cache

from app.clients.llm import BifrostClient, LLMClient, LLMError, LLMResponse, OpenAICompatibleClient, ToolCall
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.router import ConnectionRegistry, ModelRouter
from app.clients.search import SearchClient, SearchError, SearchResponse, SearchResult, TavilySearchClient
from app.config import get_settings

__all__ = [
    "BifrostClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "MockLLMClient",
    "MockSearchClient",
    "ModelRouter",
    "OpenAICompatibleClient",
    "SearchClient",
    "SearchError",
    "SearchResponse",
    "SearchResult",
    "TavilySearchClient",
    "ToolCall",
    "clients_for",
    "get_live_llm",
    "get_live_search",
    "get_mock_llm",
    "get_mock_search",
]


@lru_cache
def get_mock_llm() -> MockLLMClient:
    s = get_settings()
    return MockLLMClient(seed=s.mock_seed, latency_scale=s.mock_latency_scale)


@lru_cache
def get_mock_search() -> MockSearchClient:
    s = get_settings()
    return MockSearchClient(seed=s.mock_seed, latency_scale=s.mock_latency_scale)


@lru_cache
def get_bifrost() -> BifrostClient:
    s = get_settings()
    return BifrostClient(s.bifrost_url, timeout_s=s.llm_timeout_s, health_timeout_s=s.health_timeout_s)


@lru_cache
def get_test_bifrost(timeout_s: float = 45.0) -> BifrostClient:
    """Bifrost client with a short timeout, for connection tests (the 120 s LLM budget is for runs)."""
    s = get_settings()
    return BifrostClient(s.bifrost_url, timeout_s=timeout_s, health_timeout_s=s.health_timeout_s)


@lru_cache
def get_live_llm() -> ModelRouter:
    """Live LLM traffic: Bifrost for built-in providers, direct clients for user model connections."""
    from app.db import get_sessionmaker

    bifrost = get_bifrost()
    return ModelRouter(bifrost, ConnectionRegistry(get_sessionmaker(), bifrost=bifrost))


@lru_cache
def get_live_search() -> TavilySearchClient:
    s = get_settings()
    return TavilySearchClient(s.tavily_api_key, timeout_s=s.search_timeout_s)


def clients_for(live: bool) -> tuple[LLMClient, SearchClient]:
    """The (LLM, search) client pair for live or mock mode."""
    return (get_live_llm(), get_live_search()) if live else (get_mock_llm(), get_mock_search())
