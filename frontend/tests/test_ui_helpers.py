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
