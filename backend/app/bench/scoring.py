"""Scoring an agent's output against a task's expected answer.

Scorers
-------
``contains``  expected text appears in the output (case/punctuation-insensitive, word boundaries).
              ``|`` separates acceptable alternatives: ``"Burnham|Andy Burnham"``.
``exact``     normalised output equals the expected text, after stripping filler such as
              "The answer is".
``numeric``   a number in the answer's final sentence is within ``tolerance`` of expected
              (absolute; ``tolerance=0`` means equal up to 1e-6 relative).
``regex``     ``re.search(expected, output, IGNORECASE)``.
``judge``     an LLM (fixed judge model via Bifrost) grades the answer 0–1 against the reference.

Expected answers may contain date placeholders resolved at scoring time:
``{{current_year}}``, ``{{today_iso}}``, ``{{today_weekday}}``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.clients.llm import LLMClient, LLMError

SCORERS = ("contains", "exact", "numeric", "regex", "judge")


@dataclass(frozen=True)
class ScoreResult:
    score: float
    passed: bool
    detail: str


def render_expected(expected: str, today: date | None = None) -> str:
    """Substitute ``{{current_year}}`` / ``{{today_iso}}`` / ``{{today_weekday}}``."""
    today = today or datetime.now(UTC).date()
    return (
        expected.replace("{{current_year}}", str(today.year))
        .replace("{{today_iso}}", today.isoformat())
        .replace("{{today_weekday}}", today.strftime("%A"))
    )


def normalize(text: str) -> str:
    """NFKC, lowercase, strip markdown/punctuation (keeping decimal points), collapse spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[*_`#>]", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # drop periods that aren't decimal points
    text = re.sub(r"[^\w\s.%-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_FILLER = re.compile(r"^(the )?(final )?(answer|result)( is|:)?\s*", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    """Numbers in ``text``; handles thousands separators (``1,234`` / ``1 234``) and unicode minus."""
    t = unicodedata.normalize("NFKC", text).replace("−", "-")
    t = re.sub(r"(?<=\d)[,   ](?=\d{3}(?!\d))", "", t)
    return [float(n) for n in _NUMBER.findall(t)]


def _final_sentence_with_number(text: str) -> str:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.strip()]
    for s in reversed(sentences):
        if re.search(r"\d", s):
            return s
    return text


def score_contains(output: str, expected: str) -> ScoreResult:
    out = f" {normalize(output)} "
    alternatives = [a.strip() for a in expected.split("|") if a.strip()]
    for alt in alternatives:
        if f" {normalize(alt)} " in out:
            return ScoreResult(1.0, True, f"found '{alt}'")
    return ScoreResult(0.0, False, f"none of {alternatives} found")


def score_exact(output: str, expected: str) -> ScoreResult:
    out = _FILLER.sub("", normalize(output)).strip()
    ok = out == normalize(expected)
    return ScoreResult(float(ok), ok, "exact match" if ok else f"expected '{expected}'")


def score_numeric(output: str, expected: str, tolerance: float = 0.0) -> ScoreResult:
    try:
        target = _numbers(expected)[0]
    except IndexError:
        return ScoreResult(0.0, False, f"expected value '{expected}' is not a number")
    candidates = _numbers(_final_sentence_with_number(output))
    tol = tolerance if tolerance > 0 else max(1e-6 * abs(target), 1e-9)
    for n in candidates:
        if abs(n - target) <= tol:
            return ScoreResult(1.0, True, f"{n:g} ≈ {target:g} (±{tol:g})")
    shown = ", ".join(f"{n:g}" for n in candidates[:6]) or "no numbers"
    return ScoreResult(0.0, False, f"expected {target:g} (±{tol:g}), found {shown}")


def score_regex(output: str, expected: str) -> ScoreResult:
    try:
        ok = re.search(expected, output, re.IGNORECASE) is not None
    except re.error as exc:
        return ScoreResult(0.0, False, f"invalid regex: {exc}")
    return ScoreResult(float(ok), ok, "regex matched" if ok else f"/{expected}/ not matched")


JUDGE_PROMPT = """You are grading an AI assistant's answer against a reference answer.
Question: {question}
Reference answer: {expected}
Assistant answer: {output}

Is the assistant's answer correct and consistent with the reference? Minor wording differences are fine.
Reply with ONLY a JSON object: {{"score": <number 0 to 1>, "reason": "<one short sentence>"}}"""


def score_judge(
    output: str, expected: str, question: str, llm: LLMClient | None, judge_model: str
) -> ScoreResult:
    if llm is None:
        result = score_contains(output, expected)
        return ScoreResult(
            result.score, result.passed, f"judge unavailable, fell back to contains: {result.detail}"
        )
    prompt = JUDGE_PROMPT.format(question=question, expected=expected, output=output[:4000])
    try:
        res = llm.chat(judge_model, [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=300)
    except LLMError as exc:
        fallback = score_contains(output, expected)
        return ScoreResult(
            fallback.score, fallback.passed, f"judge error ({exc}); contains: {fallback.detail}"
        )
    match = re.search(r"\{.*\}", res.content, re.DOTALL)
    try:
        data = json.loads(match.group()) if match else None
        if not isinstance(data, dict) or "score" not in data:
            raise ValueError("no score in judge reply")
        score = max(0.0, min(1.0, float(data["score"])))
        reason = str(data.get("reason", ""))[:200]
    except (ValueError, TypeError):
        fallback = score_contains(output, expected)
        return ScoreResult(
            fallback.score, fallback.passed, f"unparseable judge reply; contains: {fallback.detail}"
        )
    return ScoreResult(score, score >= 0.5, f"judge {judge_model}: {score:.2f} · {reason}")


def score_output(
    output: str,
    *,
    scorer: str,
    expected: str,
    tolerance: float = 0.0,
    question: str = "",
    judge: Callable[[str, str, str], ScoreResult] | None = None,
    today: date | None = None,
) -> ScoreResult:
    """Score ``output`` with the named scorer. Empty output always fails."""
    if not output or not output.strip():
        return ScoreResult(0.0, False, "no answer")
    exp = render_expected(expected, today)
    if scorer == "contains":
        return score_contains(output, exp)
    if scorer == "exact":
        return score_exact(output, exp)
    if scorer == "numeric":
        return score_numeric(output, exp, tolerance)
    if scorer == "regex":
        return score_regex(output, exp)
    if scorer == "judge":
        if judge is None:
            r = score_contains(output, exp)
            return ScoreResult(r.score, r.passed, f"no judge configured, contains: {r.detail}")
        return judge(output, exp, question)
    return ScoreResult(0.0, False, f"unknown scorer '{scorer}'")
