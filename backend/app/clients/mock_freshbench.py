"""Mock replies to the Freshness Bench writer and validator prompts (``MOCK_MODE``).

The writer returns the question stored with the (fictional) article; the validator rejects the corpus items
that carry a deliberate flaw, so the offline demo shows validation at work.
"""

from __future__ import annotations

import json
import random
from typing import Any

from app.clients import mock_news
from app.freshbench.prompts import VALIDATOR_MARKER, WRITER_MARKER


def _payload(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    user = next((m for m in messages if m.get("role") == "user"), None)
    try:
        return json.loads(str(user.get("content"))) if user else None
    except ValueError:
        return None


def freshbench_responder(model: str, messages: list[dict[str, Any]], rng: random.Random) -> str | None:
    system = str(messages[0].get("content") or "") if messages else ""
    if not system.startswith((WRITER_MARKER, VALIDATOR_MARKER)):
        return None
    payload = _payload(messages) or {}
    article = payload.get("article") or {}
    item = mock_news.find_by_url(str(article.get("url") or ""), mock_news.today())
    if system.startswith(WRITER_MARKER):
        if item is None:
            return json.dumps({"skip": "not a simulated news article"})
        answer, *aliases = item.answer.split("|")
        return json.dumps(
            {"question": item.question, "answer": answer, "aliases": aliases, "event_date": item.published}
        )
    # validator
    flaw = item.flaw if item else "unsupported"
    return json.dumps(
        {
            "supported": flaw != "unsupported",
            "unambiguous": flaw != "ambiguous",
            "guessable": False,
            "reason": {
                None: "stated in the article",
                "unsupported": "the article doesn't say this",
                "ambiguous": "more than one event matches",
            }[flaw],
        }
    )
