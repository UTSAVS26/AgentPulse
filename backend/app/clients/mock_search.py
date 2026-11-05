"""Fake Tavily search for ``MOCK_MODE=true``.

Returns results from a small built-in corpus of dated "news" snippets (chosen so that the
stale-answer demo has clear current-vs-outdated cases), falling back to generic results for
anything else. All content is simulated and labelled as such in the URL host
(``mock.agentpulse.local``).
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass

from app.clients.search import Depth, SearchError, SearchResponse, SearchResult, Topic


@dataclass(frozen=True)
class CorpusEntry:
    keywords: tuple[str, ...]
    answer: str
    results: tuple[tuple[str, str, str], ...]  # (title, content, published_date)


CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry(
        keywords=("uk prime minister", "prime minister of the uk", "british prime minister", "uk pm"),
        answer="Andy Burnham became UK Prime Minister in July 2026, succeeding Keir Starmer.",
        results=(
            (
                "New UK Prime Minister Andy Burnham takes office",
                "Andy Burnham has become Prime Minister of the United Kingdom after Keir Starmer stepped "
                "down in July 2026.",
                "2026-07-24",
            ),
            (
                "Keir Starmer's final Prime Minister's Questions",
                "Keir Starmer took his last PMQs at the House of Commons before handing over to his "
                "successor.",
                "2026-07-20",
            ),
        ),
    ),
    CorpusEntry(
        keywords=("capital of france",),
        answer="Paris is the capital of France.",
        results=(("Paris - capital of France", "Paris is the capital and largest city of France.", ""),),
    ),
    CorpusEntry(
        keywords=("tallest building",),
        answer="The Burj Khalifa in Dubai is the tallest building in the world at 828 m.",
        results=(
            (
                "Burj Khalifa remains world's tallest building",
                "At 828 metres, Dubai's Burj Khalifa is still the tallest building in the world.",
                "2026-03-02",
            ),
        ),
    ),
    CorpusEntry(
        keywords=("boiling point", "water boil"),
        answer="Water boils at 100 °C at sea level.",
        results=(("Boiling point of water", "At standard atmospheric pressure water boils at 100 °C.", ""),),
    ),
)


class MockSearchClient:
    """Fake :class:`~app.clients.search.SearchClient` with seeded latency and optional failures."""

    def __init__(self, seed: int = 0, latency_scale: float = 1.0, failure_rate: float = 0.0):
        self.seed = seed
        self.latency_scale = latency_scale
        self.failure_rate = failure_rate

    def ping(self) -> tuple[bool, str]:
        return True, "mock"

    def search(
        self,
        query: str,
        max_results: int = 5,
        topic: Topic = "general",
        search_depth: Depth = "basic",
        include_answer: bool = True,
    ) -> SearchResponse:
        rng = random.Random(hashlib.sha256(f"{self.seed}|{query}".encode()).hexdigest())
        latency_ms = rng.uniform(300, 1200) * (2 if search_depth == "advanced" else 1)
        if self.latency_scale > 0:
            time.sleep(latency_ms * self.latency_scale / 1000)
        if rng.random() < self.failure_rate:
            raise SearchError("simulated Tavily outage (mock)", retryable=True, status_code=503)

        lower = query.lower()
        entry = next((e for e in CORPUS if any(k in lower for k in e.keywords)), None)
        if entry:
            results = [
                SearchResult(
                    title=title,
                    url=f"https://mock.agentpulse.local/{_slug(title)}",
                    content=content,
                    score=round(0.9 - i * 0.1, 2),
                    published_date=date or None,
                )
                for i, (title, content, date) in enumerate(entry.results)
            ]
            answer = entry.answer
        else:
            results = [
                SearchResult(
                    title=f"Result {i + 1} for '{query[:60]}'",
                    url=f"https://mock.agentpulse.local/search/{_slug(query)}/{i + 1}",
                    content=f"Simulated snippet {i + 1} discussing {query[:120]}.",
                    score=round(0.8 - i * 0.1, 2),
                    published_date=None,
                )
                for i in range(min(max_results, 3))
            ]
            answer = None
        return SearchResponse(
            query=query,
            answer=answer if include_answer else None,
            results=results[:max_results],
            latency_ms=latency_ms,
        )


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())[:60]
