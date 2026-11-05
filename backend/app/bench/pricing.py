"""Token pricing for cost metrics.

Prices come from Bifrost's model catalogue (``GET /v1/models`` → ``pricing.prompt`` /
``pricing.completion`` in USD per token), so they track each provider's published list price.
Local Ollama models cost $0. When the catalogue is unavailable (mock mode, gateway down) a
dated snapshot of the same catalogue is used.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Snapshot of Bifrost's /v1/models pricing on 2026-11-05.
SNAPSHOT_PER_MILLION: dict[str, tuple[float, float]] = {
    "groq/openai/gpt-oss-20b": (0.075, 0.30),
    "groq/openai/gpt-oss-120b": (0.15, 0.60),
    "groq/qwen/qwen3.8-27b": (0.80, 4.00),
    "openrouter/nvidia/nemotron-3.5-lightning:free": (0.0, 0.0),
}
SNAPSHOT_DATE = "2026-11-05"


@dataclass(frozen=True)
class Price:
    input_per_token: float
    output_per_token: float
    source: str  # "bifrost" | "snapshot" | "local" | "free" | "unknown"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return prompt_tokens * self.input_per_token + completion_tokens * self.output_per_token


ZERO_LOCAL = Price(0.0, 0.0, "local")
UNKNOWN = Price(0.0, 0.0, "unknown")


class PriceBook:
    """Thread-safe price lookup with a cached copy of Bifrost's catalogue."""

    def __init__(self, bifrost_url: str | None, ttl_s: float = 3600, timeout_s: float = 30):
        self.bifrost_url = bifrost_url.rstrip("/") if bifrost_url else None
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self._catalogue: dict[str, Price] = {}
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        if not self.bifrost_url or time.monotonic() - self._loaded_at < self.ttl_s:
            return
        try:
            res = httpx.get(f"{self.bifrost_url}/v1/models", timeout=self.timeout_s)
            res.raise_for_status()
            catalogue = {}
            for m in res.json().get("data", []):
                p = m.get("pricing") or {}
                if "prompt" in p and "completion" in p:
                    catalogue[m["id"]] = Price(float(p["prompt"]), float(p["completion"]), "bifrost")
            self._catalogue = catalogue
            log.info("Loaded pricing for %d models from Bifrost", len(catalogue))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("Could not load pricing from Bifrost (%s); using snapshot", exc)
        self._loaded_at = time.monotonic()  # don't hammer the gateway on failure either

    def price(self, model: str) -> Price:
        with self._lock:
            self._refresh()
            if model in self._catalogue:
                return self._catalogue[model]
        if model.startswith("ollama/"):
            return ZERO_LOCAL
        if model in SNAPSHOT_PER_MILLION:
            i, o = SNAPSHOT_PER_MILLION[model]
            return Price(i / 1e6, o / 1e6, "snapshot")
        if model.endswith(":free"):
            return Price(0.0, 0.0, "free")
        return UNKNOWN

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, str]:
        p = self.price(model)
        return p.cost(prompt_tokens, completion_tokens), p.source
