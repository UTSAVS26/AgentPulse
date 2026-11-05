from datetime import UTC, datetime

import pytest

from app.agents.tools import TOOLS, ToolContext, ToolError, run_tool, safe_eval, tool_definitions
from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchError


@pytest.fixture()
def ctx():
    return ToolContext(
        search=MockSearchClient(latency_scale=0), now=lambda: datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("17 * 23 + 4", 395),
        ("2 ^ 10", 1024),
        ("(1 + 2) * 3 / 4", 2.25),
        ("10 // 3", 3),
        ("-5 + 2", -3),
        ("sqrt(144)", 12),
        ("round(pi, 2)", 3.14),
        ("1,000 * 3", 3000),
        ("1,234,567 + 1", 1234568),
        ("max(3, 9, 4)", 9),
        ("6 × 7", 42),
    ],
)
def test_safe_eval_arithmetic(expr, expected):
    assert safe_eval(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd')",
        "(1).__class__",
        "[1, 2, 3]",
        "x + 1",
        "2 ** 100000",
        "1 / 0",
        "sqrt(-1)",
        "True + 1",
        "",
    ],
)
def test_safe_eval_rejects_unsafe_or_invalid(expr):
    with pytest.raises(ToolError):
        safe_eval(expr)


def test_calculator_tool_output(ctx):
    assert run_tool("calculator", {"expression": "17 * 23"}, ctx) == "17 * 23 = 391"


def test_current_date_tool_uses_clock(ctx):
    assert run_tool("current_date", {}, ctx) == "Today is Thursday, 2026-11-05 (UTC time 09:30)."


def test_web_search_tool_formats_sources_with_dates(ctx):
    out = run_tool("web_search", {"query": "current UK prime minister"}, ctx)
    assert out.startswith("Summary: Andy Burnham")
    assert "(2026-07-24)" in out and "https://mock.agentpulse.local/" in out


def test_web_search_failure_becomes_tool_error():
    class Down:
        def search(self, *a, **k):
            raise SearchError("service unavailable", retryable=True, status_code=503)

    with pytest.raises(ToolError, match="search failed"):
        run_tool("web_search", {"query": "x"}, ToolContext(search=Down()))


@pytest.mark.parametrize(
    ("name", "args", "match"),
    [
        ("nope", {}, "unknown tool"),
        ("calculator", {}, "missing required argument"),
        ("web_search", {"query": " "}, "missing required argument"),
        ("calculator", {"_raw": "{bad"}, "not valid JSON"),
    ],
)
def test_tool_argument_errors(ctx, name, args, match):
    with pytest.raises(ToolError, match=match):
        run_tool(name, args, ctx)


def test_tool_definitions_are_openai_format_and_skip_unknown():
    defs = tool_definitions(["calculator", "bogus", "web_search"])
    assert [d["function"]["name"] for d in defs] == ["calculator", "web_search"]
    assert all(d["type"] == "function" and "parameters" in d["function"] for d in defs)
    assert set(TOOLS) == {"web_search", "calculator", "current_date"}
