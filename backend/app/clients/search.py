"""Web search interface and the real Tavily implementation.

Used by the agents' ``web_search`` tool and by the stale-answer verifier. Tavily's REST API is
called directly with ``httpx`` (``POST https://api.tavily.com/search``) so timeouts and errors
are handled uniformly with the LLM client.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

import httpx

TAVILY_URL = "https://api.tavily.com"

Topic = Literal["general", "news", "finance"]
Depth = Literal["basic", "advanced"]


@dataclass
class SearchResult:
    title: str
    url: str
    content: str
    score: float = 0.0
    published_date: str | None = None
    """ISO-8601 date (``YYYY-MM-DD``) when the source reports one (Tavily does for ``topic=news``)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResponse:
    query: str
    answer: str | None = None
    results: list[SearchResult] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "results": [r.to_dict() for r in self.results],
            "latency_ms": self.latency_ms,
        }


class SearchError(Exception):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code

    def __str__(self) -> str:
        code = f" [{self.status_code}]" if self.status_code else ""
        return f"search_error{code}: {self.message}"


class SearchClient(Protocol):
    def search(
        self,
        query: str,
        max_results: int = 5,
        topic: Topic = "general",
        search_depth: Depth = "basic",
        include_answer: bool = True,
    ) -> SearchResponse: ...

    def ping(self) -> tuple[bool, str]: ...


def normalize_date(value: Any) -> str | None:
    """Turn Tavily's assorted date formats (RFC-2822, ISO-8601) into ``YYYY-MM-DD``."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return parsedate_to_datetime(text).astimezone(UTC).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_search_body(body: dict[str, Any], query: str, latency_ms: float) -> SearchResponse:
    results = [
        SearchResult(
            title=str(r.get("title") or ""),
            url=str(r.get("url") or ""),
            content=str(r.get("content") or "")[:2000],
            score=float(r.get("score") or 0.0),
            published_date=normalize_date(r.get("published_date")),
        )
        for r in body.get("results") or []
        if r.get("url")
    ]
    return SearchResponse(
        query=query, answer=body.get("answer") or None, results=results, latency_ms=latency_ms
    )


class TavilySearchClient:
    """Real search client backed by the Tavily API."""

    def __init__(
        self,
        api_key: str,
        timeout_s: float = 20.0,
        base_url: str = TAVILY_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport)

    def search(
        self,
        query: str,
        max_results: int = 5,
        topic: Topic = "general",
        search_depth: Depth = "basic",
        include_answer: bool = True,
    ) -> SearchResponse:
        if not self.api_key:
            raise SearchError("TAVILY_API_KEY is not set", retryable=False)
        payload = {
            "query": query[:400],
            "max_results": max(1, min(max_results, 10)),
            "topic": topic,
            "search_depth": search_depth,
            "include_answer": include_answer,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        started = time.perf_counter()
        try:
            res = self._http.post(f"{self.base_url}/search", json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise SearchError(f"no response within {self.timeout_s:.0f}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"cannot reach Tavily: {exc}", retryable=True) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        if res.status_code >= 400:
            try:
                detail = res.json().get("detail", res.text)
                if isinstance(detail, dict):
                    detail = detail.get("error", detail)
            except ValueError:
                detail = res.text
            raise SearchError(
                str(detail)[:300],
                retryable=res.status_code == 429 or res.status_code >= 500,
                status_code=res.status_code,
            )
        try:
            body = res.json()
        except ValueError as exc:
            raise SearchError("non-JSON response from Tavily", retryable=True) from exc
        return parse_search_body(body, query, latency_ms)

    def ping(self) -> tuple[bool, str]:
        """Cheap readiness check: does not spend search credits, only checks configuration."""
        return (True, "configured") if self.api_key else (False, "TAVILY_API_KEY not set")
