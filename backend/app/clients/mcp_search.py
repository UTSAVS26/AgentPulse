"""Web search through Bifrost's MCP gateway: Tavily's hosted MCP server, set up in ``bifrost/config.json``.

With ``WEB_SEARCH_VIA=bifrost-mcp`` the agents' ``web_search`` tool, the stale-answer detector and the
Freshness Bench search via ``POST /v1/mcp/tool/execute`` on Bifrost, so model calls and tool calls go through
one gateway with one audit trail. If MCP is unavailable (Bifrost down, MCP client not connected, a tool error)
the search
falls back to Tavily's REST API and the response's ``route`` says why.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.clients.search import (
    Depth,
    SearchError,
    SearchResponse,
    TavilySearchClient,
    Topic,
    parse_search_body,
)


class BifrostMCPSearchClient:
    def __init__(
        self,
        bifrost_url: str,
        tool: str = "tavily-tavily_search",
        rest: TavilySearchClient | None = None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = f"{bifrost_url.rstrip('/')}/v1/mcp/tool/execute"
        self.tool = tool
        self.rest = rest
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=5.0), transport=transport)

    def ping(self) -> tuple[bool, str]:
        return (
            (True, "configured (via Bifrost MCP)")
            if self.rest is None or self.rest.api_key
            else self.rest.ping()
        )

    def search(
        self,
        query: str,
        max_results: int = 5,
        topic: Topic = "general",
        search_depth: Depth = "basic",
        include_answer: bool = True,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> SearchResponse:
        args: dict[str, Any] = {
            "query": query[:400],
            "max_results": max(1, min(max_results, 10)),
            "topic": topic if topic in ("general", "news") else "general",
            "search_depth": search_depth,
            "include_published_date": True,
        }
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
        started = time.perf_counter()
        try:
            res = self._http.post(
                self.url,
                json={
                    "id": "agentpulse-search",
                    "type": "function",
                    "function": {"name": self.tool, "arguments": json.dumps(args)},
                },
            )
            body = res.json()
            if res.status_code != 200 or "content" not in body:
                err = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
                raise ValueError(f"HTTP {res.status_code}: {(err or res.text)[:160]}")
            payload = json.loads(body["content"])
            out = parse_search_body(payload, query, (time.perf_counter() - started) * 1000)
            out.route = "bifrost-mcp"
            return out
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:200]
        if self.rest is None:
            raise SearchError(f"Tavily via Bifrost MCP failed: {reason}", retryable=True)
        out = self.rest.search(
            query,
            max_results=max_results,
            topic=topic,
            search_depth=search_depth,
            include_answer=include_answer,
            start_date=start_date,
            end_date=end_date,
        )
        out.route = f"rest (Bifrost MCP unavailable: {reason})"
        return out
