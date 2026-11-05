import json

import httpx
import pytest

from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchError, TavilySearchClient, normalize_date, parse_search_body


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tue, 23 Jun 2026 10:47:49 GMT", "2026-06-23"),
        ("2026-07-24T14:21:37Z", "2026-07-24"),
        ("2026-07-24", "2026-07-24"),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_parse_search_body_skips_results_without_url():
    body = {
        "answer": "Andy Burnham",
        "results": [
            {
                "url": "https://a",
                "title": "t",
                "content": "c",
                "score": "0.8",
                "published_date": "2026-07-24",
            },
            {"title": "no url"},
        ],
    }
    r = parse_search_body(body, "q", 5.0)
    assert r.answer == "Andy Burnham"
    assert len(r.results) == 1
    assert r.results[0].score == 0.8 and r.results[0].published_date == "2026-07-24"


def _tavily(handler, key="tvly-test") -> TavilySearchClient:
    return TavilySearchClient(key, transport=httpx.MockTransport(handler))


def test_tavily_sends_bearer_key_and_parameters():
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [{"url": "https://x", "title": "x"}]})

    r = _tavily(handler).search("uk pm", topic="news", max_results=50)
    assert seen["auth"] == "Bearer tvly-test"
    assert seen["body"]["topic"] == "news"
    assert seen["body"]["max_results"] == 10  # clamped
    assert r.results[0].url == "https://x"


def test_tavily_auth_error_is_not_retryable():
    client = _tavily(lambda _: httpx.Response(401, json={"detail": {"error": "Unauthorized: invalid key"}}))
    with pytest.raises(SearchError) as exc:
        client.search("q")
    assert not exc.value.retryable
    assert exc.value.status_code == 401
    assert "Unauthorized" in exc.value.message


def test_tavily_rate_limit_and_timeout_are_retryable():
    with pytest.raises(SearchError) as exc:
        _tavily(lambda _: httpx.Response(429, json={"detail": "slow down"})).search("q")
    assert exc.value.retryable

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SearchError) as exc:
        _tavily(timeout).search("q")
    assert exc.value.retryable


def test_tavily_missing_key_fails_fast_and_ping_reports_it():
    client = _tavily(lambda _: httpx.Response(200, json={}), key="")
    assert client.ping() == (False, "TAVILY_API_KEY not set")
    with pytest.raises(SearchError):
        client.search("q")


def test_mock_search_corpus_hit_has_dated_sources():
    r = MockSearchClient(latency_scale=0).search("Who is the current UK prime minister?", topic="news")
    assert "Burnham" in r.answer
    assert all(res.url.startswith("https://mock.agentpulse.local/") for res in r.results)
    assert r.results[0].published_date == "2026-07-24"


def test_mock_search_generic_fallback_and_failure():
    r = MockSearchClient(latency_scale=0).search("quantum gravity breakthroughs", max_results=2)
    assert len(r.results) == 2 and r.answer is None
    with pytest.raises(SearchError):
        MockSearchClient(latency_scale=0, failure_rate=1.0).search("anything")
