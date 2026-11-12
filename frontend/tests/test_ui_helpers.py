"""Pure UI helpers (no backend needed)."""

from ui import preview_markdown


def test_preview_markdown_shifts_headings_but_not_code_or_hashtags():
    md = "# Title\n\n## Section\n\n```\n# not a heading\n```\n#hashtag\n###### deep"
    out = preview_markdown(md).splitlines()
    assert out[0] == "### Title" and out[2] == "#### Section"
    assert out[5] == "# not a heading"  # inside a code fence
    assert out[7] == "#hashtag"  # no space: not a heading
    assert out[8] == "###### deep"  # capped at h6


def test_route_badge():
    from ui import route_badge

    assert route_badge("bifrost") == "🛰️ Bifrost"
    assert route_badge("direct", "My Ollama") == "↗️ direct · My Ollama"
    assert route_badge(None) == "–"


def test_suites_page_offers_every_backend_scorer():
    """The task table and 'Test a scorer' must know every scorer, or a suite using one crashes the page."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    def scorers(path: Path) -> list[str]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SCORERS":
                return list(ast.literal_eval(node.value))
        raise AssertionError(f"no SCORERS in {path}")

    backend = scorers(root / "backend/app/bench/scoring.py")
    frontend = scorers(root / "frontend/pages/3_Suites.py")
    assert frontend == backend


def test_statistics_caption_explains_missing_interval():
    from ui import statistics_caption

    data = {
        "statistics": {"power_note": "With 5 tasks × 3 repeats, …", "method": "paired task-level bootstrap"},
        "leaderboard": [
            {"reliability_score": 1.0, "reliability_ci": None},
            {"reliability_score": 0.8, "reliability_ci": [0.6, 0.95]},
        ],
    }
    assert "A **95% CI** of – means" in statistics_caption(data)
    data["leaderboard"] = data["leaderboard"][1:]
    assert "of – means" not in statistics_caption(data)


def test_gateway_labels_on_trace_steps():
    from ui import gateway_note, search_route_label

    rescued = {
        "provider": "groq",
        "fallback": True,
        "primary_provider": "chaos",
        "gateway_ms": 14.7,
        "route_model": "chaos/r50/groq/openai/gpt-oss-20b",
    }
    assert gateway_note(rescued) == " · 🛟 rescued by a Bifrost fallback (chaos → groq) · gateway 15 ms"
    drill_ok = {"provider": "groq", "route_model": "chaos/r50/groq/x", "gateway_ms": 4.0}
    assert gateway_note(drill_ok) == " · 🌩️ outage drill route, answered by groq · gateway 4 ms"
    assert gateway_note({"provider": "groq"}) == ""
    assert search_route_label("bifrost-mcp") == "🛰️ Bifrost MCP (Tavily)"
    assert search_route_label("rest (Bifrost MCP unavailable: timeout)").startswith(
        "Tavily REST ⚠️ (Bifrost MCP"
    )
    assert search_route_label("mock") == "mock search"


def test_leaderboard_shows_rescues_only_for_drills():
    from ui import leaderboard_frame

    base = {
        "rank": 1,
        "agent_name": "A",
        "model": "m",
        "route": "bifrost",
        "connection": None,
        "reliability_score": 0.9,
        "reliability_ci": None,
        "tier": 1,
        "tied_with": [],
        "accuracy": 0.9,
        "consistency": None,
        "recovery_rate": None,
        "error_rate": 0.0,
        "latency_ms_p50": 500,
        "latency_ms_p95": 900,
        "tokens_mean": 100,
        "cost_usd_total": 0.001,
        "executions": 4,
        "gateway_overhead_ms_p50": 5.2,
        "fallback_calls": 0,
        "outage_failures": 0,
    }
    plain = leaderboard_frame([base])
    assert "🛟 Rescued by Bifrost" not in plain.columns and plain["Gateway p50"][0] == "5 ms"
    drilled = leaderboard_frame([{**base, "fallback_calls": 3, "rescued_by_gateway": 2}])
    assert drilled["🛟 Rescued by Bifrost"][0] == 2
