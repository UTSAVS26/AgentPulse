"""Phase 3.4: outage drills with Bifrost fallbacks, gateway overhead, Tavily through Bifrost's MCP gateway."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app.agents.runtime import AgentSpec
from app.bench.engine import BenchmarkEngine, InlineExecutor, drill_route, get_bench_engine
from app.bench.pricing import PriceBook
from app.clients.chaos import OUTAGE_MESSAGE, chaos_model, parse_chaos
from app.clients.llm import BifrostClient, parse_completion
from app.clients.mcp_search import BifrostMCPSearchClient
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchError, TavilySearchClient
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import TaskSuite
from app.seed import seed_agents, seed_suites


# ------------------------------------------------------------------------------------ chaos ids + parsing
def test_chaos_model_ids_round_trip():
    assert chaos_model("groq/openai/gpt-oss-20b", 0.4) == "chaos/r40/groq/openai/gpt-oss-20b"
    assert parse_chaos("chaos/r40/groq/openai/gpt-oss-20b") == (0.4, "groq/openai/gpt-oss-20b")
    assert parse_chaos("r5/openrouter/x/y") == (0.05, "openrouter/x/y")  # as Bifrost forwards it
    assert parse_chaos("groq/openai/gpt-oss-20b") is None and chaos_model("m/x", 2.0).startswith("chaos/r99/")


def test_parse_completion_reads_bifrost_fallback_and_upstream_latency():
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "extra_fields": {
            "provider": "groq",
            "upstream_latency": 240,
            "routing_info": {"provider": "groq", "is_fallback": True, "primary_provider": "chaos"},
        },
    }
    res = parse_completion(body, "chaos/r40/groq/openai/gpt-oss-20b", latency_ms=251.0)
    assert res.fallback_used and res.primary_provider == "chaos" and res.provider == "groq"
    assert res.upstream_latency_ms == 240 and res.gateway_overhead_ms == pytest.approx(11.0)
    plain = parse_completion({"choices": [{"message": {"content": "x"}}]}, "groq/m", 10.0)
    assert not plain.fallback_used and plain.gateway_overhead_ms is None


def test_bifrost_client_sends_fallbacks_only_when_given():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = BifrostClient("http://bifrost:8080", transport=httpx.MockTransport(handler))
    client.chat("groq/m", [{"role": "user", "content": "hi"}], fallbacks=["openrouter/m"])
    client.chat("groq/m", [{"role": "user", "content": "hi"}])
    assert seen[0]["fallbacks"] == ["openrouter/m"] and "fallbacks" not in seen[1]


def test_drill_route():
    spec = AgentSpec(name="a", model="groq/openai/gpt-oss-20b")
    assert drill_route(spec, 0.0, True) == {}
    assert drill_route(spec, 0.3, True) == {
        "route_model": "chaos/r30/groq/openai/gpt-oss-20b",
        "fallbacks": ["groq/openai/gpt-oss-20b"],
    }
    assert drill_route(spec, 0.3, False)["fallbacks"] == []
    own = AgentSpec(name="b", model="groq/openai/gpt-oss-20b", fallbacks=["openrouter/openai/gpt-oss-20b"])
    assert drill_route(own, 0.3, True)["fallbacks"] == ["openrouter/openai/gpt-oss-20b"]
    assert drill_route(AgentSpec(name="c", model="custom/x/y"), 0.5, True) == {}  # direct connections


# ------------------------------------------------------------------------------- drills end to end (mock)
@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: (MockLLMClient(seed=5, latency_scale=0), MockSearchClient(latency_scale=0)),
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    app.dependency_overrides[get_bench_engine] = lambda: engine
    return client


def _run(api, db_engine, **drill):
    with Session(db_engine) as db:
        sid = db.query(TaskSuite).filter_by(name="Factual QA").one().id
    run = api.post("/runs", json={"suite_id": sid, "agent_ids": [1, 2], "repeats": 2, **drill}).json()
    return run, api.get(f"/runs/{run['id']}/metrics").json()["leaderboard"]


def test_outage_drill_with_fallbacks_is_rescued_by_the_gateway(api, db_engine):
    run, board = _run(api, db_engine, outage_rate=0.5, drill_fallbacks=True)
    assert run["outage_rate"] == 0.5 and run["drill_fallbacks"] is True
    for row in board:
        assert row["fallback_calls"] > 0 and row["rescued_by_gateway"] > 0 and row["outage_failures"] == 0
        assert 0 < row["fallback_rate"] < 1 and row["gateway_overhead_ms_p50"] > 0
    ex = api.get(f"/runs/{run['id']}/executions").json()
    rescued = next(e for e in ex if e["fallback_calls"])
    trace = api.get(f"/executions/{rescued['id']}").json()["trace"]
    calls = [s["payload"] for s in trace if s["type"] == "llm_call"]
    assert any(c.get("fallback") and c["primary_provider"] == "chaos" for c in calls)
    assert all(c["route_model"].startswith("chaos/r50/") and "gateway_ms" in c for c in calls)


def test_outage_drill_without_fallbacks_fails_calls(api, db_engine):
    run, board = _run(api, db_engine, outage_rate=0.5, drill_fallbacks=False)
    assert all(r["fallback_calls"] == 0 for r in board)
    assert sum(r["outage_failures"] for r in board) > 0
    assert (
        api.post(
            "/runs", json={"suite_id": run["suite_id"], "agent_ids": [1], "outage_rate": 0.95}
        ).status_code
        == 422
    )


def test_no_drill_records_overhead_but_no_fallbacks(api, db_engine):
    run, board = _run(api, db_engine)
    assert run["outage_rate"] is None and run["drill_fallbacks"] is None
    assert all(r["fallback_calls"] == 0 and r["gateway_overhead_ms_p50"] > 0 for r in board)


def test_agent_fallbacks_round_trip_and_validation(api):
    body = {
        "name": "With fallback",
        "model": "groq/openai/gpt-oss-20b",
        "fallbacks": ["openrouter/openai/gpt-oss-20b"],
    }
    created = api.post("/agents", json=body).json()
    assert created["fallbacks"] == ["openrouter/openai/gpt-oss-20b"]
    assert api.post("/agents", json={**body, "name": "bad", "fallbacks": ["nope"]}).status_code == 422
    assert api.post("/agents", json={**body, "name": "bad2", "fallbacks": ["custom/x/y"]}).status_code == 422
    assert api.post("/agents", json={**body, "name": "none", "fallbacks": None}).json()["fallbacks"] == []


# ------------------------------------------------------------------------------------ chaos endpoint
def test_chaos_endpoint_only_answers_bifrost_and_fails_at_its_rate(client, monkeypatch):
    body = {"model": "r99/groq/openai/gpt-oss-20b", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/chaos/v1/chat/completions", json=body).status_code == 401
    auth = {"Authorization": f"Bearer {get_settings().chaos_token}"}
    import app.routers.chaos as chaos_router

    monkeypatch.setattr(chaos_router.random, "random", lambda: 0.0)
    res = client.post("/chaos/v1/chat/completions", json=body, headers=auth)
    assert res.status_code == 503 and res.json()["error"]["message"] == OUTAGE_MESSAGE
    assert (
        client.post("/chaos/v1/chat/completions", json={"model": "groq/x"}, headers=auth).status_code == 400
    )

    forwarded = {}

    class FakeAsync:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            forwarded.update(url=url, body=json)
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr(chaos_router.httpx, "AsyncClient", FakeAsync)
    monkeypatch.setattr(chaos_router.random, "random", lambda: 0.999)
    res = client.post("/chaos/v1/chat/completions", json={**body, "fallbacks": ["x/y"]}, headers=auth)
    assert res.status_code == 200 and forwarded["url"].endswith("/v1/chat/completions")
    assert forwarded["body"]["model"] == "groq/openai/gpt-oss-20b" and "fallbacks" not in forwarded["body"]


# ------------------------------------------------------------------------------------ Tavily via Bifrost MCP
MCP_BODY = {
    "query": "q",
    "answer": None,
    "results": [
        {
            "title": "Spain win",
            "url": "https://x.test/a",
            "content": "Spain won.",
            "published_date": "Mon, 20 Jul 2026 19:00:50 GMT",
            "score": 0.9,
        }
    ],
}


def test_mcp_search_parses_results_and_sends_the_tool_call():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"role": "tool", "content": json.dumps(MCP_BODY)})

    client = BifrostMCPSearchClient("http://bifrost:8080", transport=httpx.MockTransport(handler))
    res = client.search("latest World Cup", topic="news", start_date="2026-07-01")
    assert res.route == "bifrost-mcp" and res.results[0].published_date == "2026-07-20"
    args = json.loads(seen["function"]["arguments"])
    assert seen["function"]["name"] == "tavily-tavily_search"
    assert (
        args["include_published_date"] and args["start_date"] == "2026-07-01" and "include_answer" not in args
    )


def test_mcp_search_falls_back_to_rest_with_the_reason():
    def mcp_down(request):
        return httpx.Response(
            400, json={"error": {"message": "tool 'tavily-tavily_search' is not available"}}
        )

    def rest(request):
        return httpx.Response(200, json=MCP_BODY)

    rest_client = TavilySearchClient("key", transport=httpx.MockTransport(rest))
    client = BifrostMCPSearchClient(
        "http://bifrost:8080", rest=rest_client, transport=httpx.MockTransport(mcp_down)
    )
    res = client.search("q")
    assert res.route.startswith("rest (Bifrost MCP unavailable: ValueError: HTTP 400: tool")
    assert res.results[0].title == "Spain win"
    with pytest.raises(SearchError, match="via Bifrost MCP failed"):
        BifrostMCPSearchClient("http://bifrost:8080", transport=httpx.MockTransport(mcp_down)).search("q")


def test_live_search_client_follows_the_setting(monkeypatch):
    from app import clients

    for via, kind in (("bifrost-mcp", BifrostMCPSearchClient), ("rest", TavilySearchClient)):
        monkeypatch.setattr(clients, "get_settings", lambda via=via: Settings(web_search_via=via))
        clients.get_live_search.cache_clear()  # cached per process
        assert isinstance(clients.get_live_search(), kind)
    clients.get_live_search.cache_clear()


def test_web_search_tool_reports_its_route():
    from app.agents.runtime import RuntimeConfig, run_agent

    spec = AgentSpec(name="s", model="groq/openai/gpt-oss-120b", tools=["web_search"])
    result = run_agent(
        spec,
        "Who won the latest World Cup?",
        MockLLMClient(latency_scale=0),
        MockSearchClient(latency_scale=0),
        RuntimeConfig(sleep_scale=0),
    )
    results = [s.payload for s in result.steps if s.type == "tool_result"]
    assert results and all(p.get("via") == "mock" for p in results)
