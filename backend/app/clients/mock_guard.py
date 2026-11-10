"""Mock reply to the Freshness Guard's correction prompt (``MOCK_MODE``): a rewritten answer that states what
is true today, cites the numbered sources and lists them, as a real model would."""

from __future__ import annotations

import json
import random
from typing import Any

from app.guard.prompts import CORRECT_MARKER


def guard_responder(model: str, messages: list[dict[str, Any]], rng: random.Random) -> str | None:
    system = str(messages[0].get("content") or "") if messages else ""
    if not system.startswith(CORRECT_MARKER):
        return None
    user = next((m for m in messages if m.get("role") == "user"), {})
    try:
        payload = json.loads(str(user.get("content") or ""))
    except ValueError:
        return None
    lines = []
    for f in payload.get("findings") or []:
        refs = "".join(f"[{n}]" for n in f.get("sources") or [])
        lines.append(f"{f.get('true_today') or 'This has changed.'} {refs}".strip())
    sources = "\n".join(
        f"[{s['n']}] {s.get('title') or s.get('url')} ({s.get('published_date') or 'undated'}) {s.get('url')}"
        for s in payload.get("sources") or []
    )
    text = f"As of {payload.get('today')}: " + " ".join(lines)
    return text + (f"\n\nSources:\n{sources}" if sources else "")
