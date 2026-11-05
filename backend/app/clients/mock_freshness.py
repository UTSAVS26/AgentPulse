"""Mock replies to the stale-answer detector's prompts (``MOCK_MODE=true``).

Registered on every :class:`~app.clients.mock_llm.MockLLMClient`, so mock mode runs the *same*
extraction → search → verdict pipeline as live mode, including the JSON parsing:

* extraction prompt → rule-based claims from :mod:`app.freshness.heuristics`, as JSON;
* verification prompt → a verdict from the mock search corpus: a claim naming the topic's
  current answer is ``current``, one naming an older answer is ``outdated``, any other answer
  on a known topic is ``contradicted``, and unknown topics are ``unverifiable``.
"""

from __future__ import annotations

import json
import random
from typing import Any

from app.clients.mock_search import find_entry
from app.freshness.heuristics import heuristic_claims
from app.freshness.prompts import EXTRACT_SYSTEM, VERIFY_SYSTEM


def freshness_responder(model: str, messages: list[dict[str, Any]], rng: random.Random) -> str | None:
    if len(messages) < 2 or messages[0].get("role") != "system":
        return None
    system = messages[0].get("content")
    if system not in (EXTRACT_SYSTEM, VERIFY_SYSTEM):
        return None
    try:
        payload = json.loads(str(messages[-1].get("content") or "{}"))
    except ValueError:
        return "{}"
    if system == EXTRACT_SYSTEM:
        return _extract(payload)
    return _verify(payload)


def _extract(payload: dict[str, Any]) -> str:
    claims = heuristic_claims(str(payload.get("text") or ""), payload.get("question"))
    out = []
    for c in claims:
        entry = find_entry(f"{c.text} {c.query}")
        out.append(
            {
                "claim": c.text,
                "time_sensitive": c.time_sensitive,
                "query": c.query,
                "topic": "news" if entry and entry.stale_terms else "general",
            }
        )
    return json.dumps({"claims": out})


def _verify(payload: dict[str, Any]) -> str:
    claim = str(payload.get("claim") or "")
    sources = payload.get("sources") or []
    entry = find_entry(claim)
    if entry is None or not entry.current_terms or not sources:
        return json.dumps(
            {
                "verdict": "unverifiable",
                "confidence": 0.3,
                "explanation": "The search results do not clearly address this claim (mock corpus).",
                "correction": "",
            }
        )
    dated = sorted((s for s in sources if s.get("published_date")), key=lambda s: s["published_date"])
    newest = dated[-1] if dated else sources[0]
    cite = f"The most recent source ({newest.get('published_date') or 'undated'}, \"{newest.get('title')}\")"
    lower = claim.lower()
    if any(t in lower for t in entry.current_terms):
        verdict, confidence = "current", 0.92
        explanation = f"{cite} confirms it: {entry.answer}"
    elif any(t in lower for t in entry.stale_terms):
        verdict, confidence = "outdated", 0.9
        explanation = f"This was true earlier, but {cite[0].lower()}{cite[1:]} reports: {entry.answer}"
    else:
        verdict, confidence = "contradicted", 0.6
        explanation = f"{cite} does not support it: {entry.answer}"
    return json.dumps(
        {
            "verdict": verdict,
            "was_true_before": verdict == "outdated",
            "confidence": confidence,
            "explanation": explanation,
            "correction": "" if verdict == "current" else entry.answer,
        }
    )
