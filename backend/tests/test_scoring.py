from datetime import date

import pytest

from app.bench.scoring import (
    ScoreResult,
    normalize,
    render_expected,
    score_judge,
    score_numeric,
    score_output,
)
from app.clients.llm import LLMError, LLMResponse

TODAY = date(2026, 11, 5)  # a Thursday


def s(output, scorer, expected, tolerance=0.0, **kw):
    return score_output(output, scorer=scorer, expected=expected, tolerance=tolerance, today=TODAY, **kw)


# ------------------------------------------------------------------------------------ helpers
def test_render_expected_placeholders():
    assert render_expected("{{current_year}}", TODAY) == "2026"
    assert render_expected("{{today_weekday}} {{today_iso}}", TODAY) == "Thursday 2026-11-05"


def test_normalize_handles_unicode_markdown_and_punctuation():
    assert normalize("**Andy Burnham**.") == "andy burnham"
    assert normalize("It costs 1628.89 dollars.") == "it costs 1628.89 dollars"


# ------------------------------------------------------------------------------------ contains
@pytest.mark.parametrize(
    ("output", "expected", "passed"),
    [
        ("The current Prime Minister is Andy Burnham.", "Burnham", True),
        ("It's **Canberra**.", "Canberra", True),
        ("Sydney", "Canberra", False),
        ("The symbol is Au.", "Au", True),
        ("Gold is beautiful", "Au", False),  # word boundary: no match inside other words
        ("Starmer", "Burnham|Andy Burnham", False),
        ("Andy Burnham", "Burnham|Andy Burnham", True),
        ("Today is Thursday, November 5.", "{{today_weekday}}", True),
    ],
)
def test_contains(output, expected, passed):
    assert s(output, "contains", expected).passed is passed


# ------------------------------------------------------------------------------------ numeric
@pytest.mark.parametrize(
    ("output", "expected", "tol", "passed"),
    [
        ("17 × 23 + 4 equals 395.", "395", 0, True),
        ("The answer is 7,006,652.", "7006652", 0, True),
        ("1 234 567", "1234567", 0, True),
        ("After 10 years: **$1,628.89**", "1628.89", 0.01, True),
        ("About $1,628.90", "1628.89", 0.01, True),
        ("About $1,629", "1628.89", 0.01, False),
        ("The result is 396.", "395", 0, False),
        ("Temperature is −40 degrees.", "-40", 0, True),
        # only the final sentence containing a number is considered
        ("Earlier I thought 395. Actually it is 396.", "395", 0, False),
        ("I can't compute that.", "395", 0, False),
    ],
)
def test_numeric(output, expected, tol, passed):
    assert s(output, "numeric", expected, tol).passed is passed


def test_numeric_detail_explains_failure():
    r = score_numeric("It is 396.", "395")
    assert "expected 395" in r.detail and "396" in r.detail


# ---------------------------------------------------------------- exact / regex / empty / unknown
def test_exact_strips_filler():
    assert s("The answer is: Paris.", "exact", "Paris").passed
    assert not s("Paris, France", "exact", "Paris").passed


def test_regex_and_invalid_regex():
    assert s("Symbol: Au", "regex", r"\bAu\b").passed
    r = s("x", "regex", "([")
    assert not r.passed and "invalid regex" in r.detail


def test_empty_output_always_fails():
    for scorer in ("contains", "exact", "numeric", "regex", "judge"):
        r = s("   ", scorer, "x")
        assert (r.score, r.passed, r.detail) == (0.0, False, "no answer")


def test_unknown_scorer_fails_cleanly():
    assert s("x", "telepathy", "x") == ScoreResult(0.0, False, "unknown scorer 'telepathy'")


# ------------------------------------------------------------------------------------ judge
class FakeJudge:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def chat(self, *a, **k):
        if self.error:
            raise self.error
        return LLMResponse(content=self.reply)


def test_judge_parses_json_score():
    r = score_judge(
        "Rayleigh scattering.",
        "Rayleigh scattering",
        "why?",
        FakeJudge('{"score": 0.9, "reason": "ok"}'),
        "m",
    )
    assert r.passed and r.score == 0.9 and "ok" in r.detail


def test_judge_with_extra_text_and_clamping():
    r = score_judge("x", "y", "q", FakeJudge('Sure! {"score": 7, "reason": "great"} done'), "m")
    assert r.score == 1.0


def test_judge_failures_fall_back_to_contains():
    for judge in (FakeJudge("not json"), FakeJudge(error=LLMError("down", retryable=True))):
        r = score_judge("Rayleigh scattering of light", "Rayleigh", "q", judge, "m")
        assert r.passed and "contains" in r.detail


def test_judge_scorer_without_judge_uses_contains():
    r = s("Rayleigh scattering", "judge", "Rayleigh scattering")
    assert r.passed and "no judge configured" in r.detail


@pytest.mark.parametrize(
    "output", ["It is **Renuvis Enzara™**.", "Enzara® ships now", "© Enzara 2026", "Enzara℠"]
)
def test_contains_ignores_trademark_symbols(output):
    """NFKC turns ™ into the letters TM; the symbol must not glue onto the name."""
    assert score_output(output, scorer="contains", expected="Enzara").passed
