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

from app.clients import mock_news
from app.clients.search import Depth, SearchError, SearchResponse, SearchResult, Topic


@dataclass(frozen=True)
class CorpusEntry:
    keywords: tuple[str, ...]
    answer: str
    results: tuple[tuple[str, str, str], ...]  # (title, content, published_date)
    current_terms: tuple[str, ...] = ()
    """Terms that make a claim on this topic *current* (used by the mock fact-checker)."""
    stale_terms: tuple[str, ...] = ()
    """Terms that make a claim *outdated*: what an older model would say."""
    stale_answer: str = ""
    """A typical answer from a model with old training data (mock chat answers, Freshness Guard demo)."""

    def matches(self, text: str) -> bool:
        lower = text.lower()
        return any(k in lower for k in self.keywords)


CORPUS: tuple[CorpusEntry, ...] = (
    CorpusEntry(
        keywords=(
            "uk prime minister",
            "prime minister of the uk",
            "prime minister of the united kingdom",
            "british prime minister",
            "uk pm",
        ),
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
        current_terms=("burnham",),
        stale_terms=("starmer", "sunak", "truss", "boris johnson", "theresa may"),
        stale_answer="The current UK Prime Minister is Rishi Sunak, leader of the Conservative Party.",
    ),
    CorpusEntry(
        keywords=("world cup",),
        answer="Spain won the 2026 FIFA World Cup, the most recent edition.",
        results=(
            (
                "Spain crowned 2026 FIFA World Cup champions",
                "Spain won the 2026 FIFA World Cup final on 19 July 2026, their second world title.",
                "2026-07-19",
            ),
            (
                "Argentina win the 2022 World Cup in Qatar",
                "Argentina beat France on penalties to win the 2022 FIFA World Cup in Qatar.",
                "2022-12-18",
            ),
        ),
        current_terms=("spain",),
        stale_terms=("argentina", "france", "germany"),
        stale_answer="Argentina won the latest FIFA World Cup, beating France on penalties in the final.",
    ),
    CorpusEntry(
        keywords=("python",),
        answer="Python 3.15 is the latest stable minor version, released in October 2026.",
        results=(
            (
                "Python 3.15.0 is now available",
                "The Python Software Foundation released Python 3.15.0, the newest stable version.",
                "2026-10-01",
            ),
            (
                "What's new in Python 3.13",
                "Python 3.13 was released in October 2024 with an improved interactive interpreter.",
                "2024-10-07",
            ),
        ),
        current_terms=("3.15",),
        stale_terms=("3.14", "3.13", "3.12", "3.11"),
        stale_answer="The latest stable version of Python is Python 3.13.",
    ),
    CorpusEntry(
        keywords=("secretary-general", "secretary general"),
        answer="António Guterres is the UN Secretary-General; his second term runs until 31 December 2026.",
        results=(
            (
                "UN Secretary-General António Guterres addresses the General Assembly",
                "Secretary-General António Guterres opened the general debate of the 81st session.",
                "2026-09-22",
            ),
        ),
        current_terms=("guterres",),
        stale_terms=("ban ki-moon", "ban ki moon", "kofi annan"),
    ),
    CorpusEntry(
        keywords=("capital of france",),
        answer="Paris is the capital of France.",
        results=(("Paris - capital of France", "Paris is the capital and largest city of France.", ""),),
        current_terms=("paris",),
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
        current_terms=("burj khalifa",),
        stale_terms=("taipei 101", "petronas", "willis tower", "sears tower"),
    ),
    CorpusEntry(
        keywords=("boiling point", "water boil", "water boils"),
        answer="Water boils at 100 °C at sea level.",
        results=(("Boiling point of water", "At standard atmospheric pressure water boils at 100 °C.", ""),),
    ),
)


def mock_fact_answer(prompt: str, stale_probability: float, rng: random.Random) -> str | None:
    """A mock model's answer to a question on a corpus topic: outdated with ``stale_probability`` (as from old
    training data), otherwise current. None for other prompts."""
    entry = find_entry(prompt)
    if entry is None:
        return None
    if entry.stale_answer and rng.random() < stale_probability:
        return entry.stale_answer
    return entry.answer


def find_entry(text: str) -> CorpusEntry | None:
    """The corpus entry whose keywords appear in ``text`` (first match), if any."""
    return next((e for e in CORPUS if e.matches(text)), None)


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
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> SearchResponse:
        rng = random.Random(hashlib.sha256(f"{self.seed}|{query}".encode()).hexdigest())
        latency_ms = rng.uniform(300, 1200) * (2 if search_depth == "advanced" else 1)
        if self.latency_scale > 0:
            time.sleep(latency_ms * self.latency_scale / 1000)
        if rng.random() < self.failure_rate:
            raise SearchError("simulated Tavily outage (mock)", retryable=True, status_code=503)

        today = mock_news.today()
        if start_date or end_date:  # Freshness Bench generation: dated news inside the window
            items = mock_news.search_window(query, start_date, end_date, today)
            rng.shuffle(items)
            return SearchResponse(
                query=query,
                results=[_news_result(it, 0.9 - i * 0.05) for i, it in enumerate(items[:max_results])],
                latency_ms=latency_ms,
            )
        news = mock_news.find_item(query, today)
        entry = None if news else find_entry(query)
        if news:  # an agent looking up a Freshness Bench question
            results = [_news_result(news, 0.92)]
            answer = f"{news.content} (simulated news, {news.published})"
        elif entry:
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


def _news_result(item: mock_news.NewsItem, score: float) -> SearchResult:
    return SearchResult(
        title=item.title,
        url=item.url,
        content=item.content,
        score=round(score, 2),
        published_date=item.published,
    )


def _slug(text: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())[:60]
