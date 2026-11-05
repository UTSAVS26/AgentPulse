"""LLM and search clients, with real and mock implementations chosen by ``MOCK_MODE``."""

from __future__ import annotations

from functools import lru_cache

from app.clients.llm import BifrostClient, LLMClient, LLMError, LLMResponse, ToolCall
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchClient, SearchError, SearchResponse, SearchResult, TavilySearchClient
from app.config import get_settings

__all__ = [
    "BifrostClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "MockLLMClient",
    "MockSearchClient",
    "SearchClient",
    "SearchError",
    "SearchResponse",
    "SearchResult",
    "TavilySearchClient",
    "ToolCall",
    "get_llm_client",
    "get_search_client",
]


@lru_cache
def get_llm_client() -> LLMClient:
    """Process-wide LLM client: :class:`MockLLMClient` in mock mode, else :class:`BifrostClient`."""
    s = get_settings()
    if s.mock_mode:
        return MockLLMClient(seed=s.mock_seed, latency_scale=s.mock_latency_scale)
    return BifrostClient(s.bifrost_url, timeout_s=s.llm_timeout_s, health_timeout_s=s.health_timeout_s)


@lru_cache
def get_search_client() -> SearchClient:
    """Process-wide search client: :class:`MockSearchClient` in mock mode, else Tavily."""
    s = get_settings()
    if s.mock_mode:
        return MockSearchClient(seed=s.mock_seed, latency_scale=s.mock_latency_scale)
    return TavilySearchClient(s.tavily_api_key, timeout_s=s.search_timeout_s)
