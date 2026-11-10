"""Mock replies to the judge prompt (``judge`` / ``jury`` scorers) in ``MOCK_MODE``.

A simulated juror checks whether the reference answer appears in the assistant's answer, then errs with a
small, model-specific probability. Jurors therefore sometimes disagree, as real ones do, which makes the jury
agreement statistics (Fleiss' κ) meaningful in the offline demo.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any

JUDGE_MARKER = "You are grading an AI assistant's answer against a reference answer."


def _field(prompt: str, label: str) -> str:
    match = re.search(rf"^{label}: (.*)$", prompt, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _norm(text: str) -> str:
    """Same idea as ``bench.scoring.normalize``: drop punctuation but keep decimal points ("4." → "4")."""
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text.lower())
    return " " + re.sub(r"[^\w.%-]+", " ", text).strip() + " "


def error_rate(model: str) -> float:
    """Stable per-model juror error rate between 2% and 8% (strong judges rarely disagree; live, the
    frontier jury was unanimous on clear-cut answers)."""
    h = int(hashlib.sha256(model.encode()).hexdigest()[:8], 16)
    return 0.02 + (h % 1000) / 1000 * 0.06


def judge_responder(model: str, messages: list[dict[str, Any]], rng: random.Random) -> str | None:
    if not messages or messages[0].get("role") != "user":
        return None
    prompt = str(messages[0].get("content") or "")
    if not prompt.startswith(JUDGE_MARKER):
        return None
    expected = _field(prompt, "Reference answer")
    answer = _norm(_field(prompt, "Assistant answer"))
    correct = any(_norm(alt) in answer for alt in expected.split("|") if alt.strip())
    if rng.random() < error_rate(model):
        correct = not correct
    score = round(rng.uniform(0.75, 1.0) if correct else rng.uniform(0.0, 0.25), 2)
    reason = "matches the reference" if correct else "does not match the reference"
    return json.dumps({"score": score, "reason": reason})
