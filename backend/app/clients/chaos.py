"""Outage drills: the ``chaos`` model id that makes a share of calls fail on purpose.

``chaos/r40/groq/openai/gpt-oss-20b`` means "call groq/openai/gpt-oss-20b, but fail 40% of the calls with a
503 first". Live, Bifrost routes ``chaos/…`` to AgentPulse's chaos endpoint (a custom provider in
``bifrost/config.json``), which fails or forwards the call back through Bifrost; with a request-level
``fallbacks`` list, **Bifrost's own fallback machinery** rescues the failed calls. The mock LLM simulates the
same behaviour offline.
"""

from __future__ import annotations

import re

PREFIX = "chaos/"
_RATE = re.compile(r"^r(\d{1,2})/(.+)$")
OUTAGE_MESSAGE = "chaos: simulated provider outage (AgentPulse outage drill)"


def chaos_model(model: str, rate: float) -> str:
    """The chaos route for ``model`` with outage ``rate`` (0–0.99)."""
    return f"{PREFIX}r{max(0, min(99, round(rate * 100)))}/{model}"


def parse_chaos(model: str) -> tuple[float, str] | None:
    """``(rate, real model)`` for a ``chaos/r<NN>/<model>`` id (or Bifrost's ``r<NN>/<model>``), else None."""
    rest = model[len(PREFIX) :] if model.startswith(PREFIX) else model
    m = _RATE.match(rest)
    if not m:
        return None
    return int(m.group(1)) / 100, m.group(2)
