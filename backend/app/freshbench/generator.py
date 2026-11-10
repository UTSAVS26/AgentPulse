"""Generate a Freshness Bench snapshot: Tavily news per category × age bucket → a model writes one dated
question per article → a second call rejects unsupported, ambiguous or guessable questions.

The output is a list of :class:`Question` that the caller freezes into a read-only suite. Nothing here
touches the database, so it is easy to test with the mock clients.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from app.clients.llm import LLMClient, LLMError
from app.clients.search import SearchClient, SearchError, SearchResult
from app.freshbench.buckets import BUCKETS, CATEGORIES, Bucket, age_days, bucket_for
from app.freshbench.prompts import VALIDATOR_SYSTEM, WRITER_SYSTEM
from app.freshness.checker import chat_with_fallback, parse_json_reply

log = logging.getLogger(__name__)

MIN_CONTENT_CHARS = 60
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_DATE_ANSWER = re.compile(
    rf"^\s*((({_MONTHS})\.?\s*\d{{0,2}},?\s*)?(19|20)\d\d|\d{{4}}-\d{{2}}(-\d{{2}})?|({_MONTHS})"
    rf"(\s+\d{{1,2}})?|(mon|tues|wednes|thurs|fri|satur|sun)day)\s*$",
    re.IGNORECASE,
)


def is_date_answer(answer: str) -> bool:
    """'November 2026', '2025', '2026-11-04', 'March 3', 'Tuesday': every question is dated already."""
    return bool(_DATE_ANSWER.match(answer.strip()))


@dataclass
class Question:
    key: str
    prompt: str
    expected: str  # "answer|alias|…"
    category: str
    bucket: str
    event_date: str
    source_url: str
    source_title: str

    def task(self, scorer: str = "contains") -> dict[str, Any]:
        """As a suite task (``TaskIn`` fields)."""
        return {
            "key": self.key,
            "prompt": self.prompt,
            "expected": self.expected,
            "scorer": scorer,
            "category": self.category,
            "event_date": self.event_date,
            "source_url": self.source_url,
            "source_title": self.source_title[:500],
        }


@dataclass
class GenerationResult:
    today: str
    questions: list[Question] = field(default_factory=list)
    searches: int = 0
    articles: int = 0
    written: int = 0
    rejected: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        per_bucket = Counter(q.bucket for q in self.questions)
        return {
            "today": self.today,
            "questions": len(self.questions),
            "searches": self.searches,
            "articles": self.articles,
            "written": self.written,
            "rejected": dict(self.rejected),
            "per_bucket": {b.key: per_bucket.get(b.key, 0) for b in BUCKETS},
            "per_category": dict(Counter(q.category for q in self.questions)),
            "errors": self.errors[:10],
        }


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


class Generator:
    def __init__(
        self,
        llm: LLMClient,
        search: SearchClient,
        models: list[str],
        *,
        today: date,
        categories: list[str] | None = None,
        buckets: list[str] | None = None,
        per_cell: int = 2,
        workers: int = 4,
        retry_backoff_s: float = 0.5,
        progress: Callable[[int, int, str], None] | None = None,
    ):
        self.llm, self.search, self.models = llm, search, models
        self.today = today
        self.categories = [c for c in (categories or list(CATEGORIES)) if c in CATEGORIES]
        self.buckets = [b for b in BUCKETS if not buckets or b.key in buckets]
        if not self.categories or not self.buckets:
            raise ValueError("pick at least one known category and age bucket")
        self.per_cell = max(1, min(per_cell, 5))
        self.workers = workers
        self.retry_backoff_s = retry_backoff_s
        self.progress = progress or (lambda done, total, note: None)
        self._lock = threading.Lock()
        self._seen_urls: set[str] = set()
        self._seen_questions: set[str] = set()

    # ------------------------------------------------------------------------------ public
    def run(self) -> GenerationResult:
        result = GenerationResult(today=self.today.isoformat())
        cells = [(c, b) for b in self.buckets for c in self.categories]
        done = 0
        self.progress(0, len(cells), "searching the news")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for cell_questions in pool.map(lambda cell: self._cell(*cell, result), cells):
                done += 1
                with self._lock:
                    result.questions.extend(cell_questions)
                self.progress(done, len(cells), f"{len(result.questions)} questions so far")
        result.questions.sort(key=lambda q: (q.event_date, q.category), reverse=True)
        counters: Counter = Counter()
        for q in result.questions:  # stable, readable keys: fb-<bucket>-<category>-<n>
            counters[(q.bucket, q.category)] += 1
            q.key = f"fb-{q.bucket}-{q.category}-{counters[(q.bucket, q.category)]}"
        return result

    # ------------------------------------------------------------------------------ one cell
    def _cell(self, category: str, bucket: Bucket, result: GenerationResult) -> list[Question]:
        start, end = bucket.window(self.today)
        try:
            res = self.search.search(
                CATEGORIES[category],
                max_results=min(10, self.per_cell * 3 + 2),
                topic="news",
                search_depth="basic",
                include_answer=False,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        except SearchError as exc:
            with self._lock:
                result.errors.append(f"{category}/{bucket.key}: {exc}")
            return []
        with self._lock:
            result.searches += 1
        out: list[Question] = []
        for article in self._candidates(res.results, start, end):
            if len(out) >= self.per_cell:
                break
            with self._lock:
                result.articles += 1
            q = self._question(article, category, result)
            if q:
                out.append(q)
        return out

    def _candidates(self, results: list[SearchResult], start: date, end: date) -> list[SearchResult]:
        """Dated articles inside the window (Tavily is occasionally loose), each URL used once overall."""
        picked = []
        for r in results:
            if not r.published_date or not (start.isoformat() <= r.published_date <= end.isoformat()):
                continue
            if len(r.content or "") < MIN_CONTENT_CHARS:
                continue
            with self._lock:
                if r.url in self._seen_urls:
                    continue
                self._seen_urls.add(r.url)
            picked.append(r)
        return picked

    def _ask(self, system: str, payload: dict[str, Any], require: str | None = None) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        reply = chat_with_fallback(
            self.llm, self.models, messages, max_tokens=900, retry_backoff_s=self.retry_backoff_s
        )
        data = parse_json_reply(reply, require=require)
        if not isinstance(data, dict):
            raise ValueError("reply is not a JSON object")
        return data

    def _question(self, article: SearchResult, category: str, result: GenerationResult) -> Question | None:
        doc = {
            "title": article.title,
            "url": article.url,
            "published_date": article.published_date,
            "text": (article.content or "")[:3000],
        }
        reject = lambda why: (self._count(result, why), None)[1]  # noqa: E731
        try:
            written = self._ask(
                WRITER_SYSTEM, {"today": self.today.isoformat(), "category": category, "article": doc}
            )
        except (LLMError, ValueError) as exc:
            with self._lock:
                result.errors.append(f"writer: {exc}"[:200])
            return reject("writer failed")
        if written.get("skip") or not written.get("question") or not written.get("answer"):
            return reject("no clear fact")
        question = str(written["question"]).strip()[:500]
        answer = str(written["answer"]).strip()[:200]
        aliases = [str(a).strip() for a in written.get("aliases") or [] if str(a).strip()][:5]
        with self._lock:
            result.written += 1
        if _norm(answer) and _norm(answer) in _norm(question):
            return reject("answer in question")
        if is_date_answer(answer):
            return reject("answer is a date")
        try:
            verdict = self._ask(
                VALIDATOR_SYSTEM,
                {"article": doc, "question": question, "proposed_answer": answer},
                require="supported",
            )
        except (LLMError, ValueError) as exc:
            with self._lock:
                result.errors.append(f"validator: {exc}"[:200])
            return reject("validator failed")
        if not verdict.get("supported"):
            return reject("not supported by the article")
        if not verdict.get("unambiguous", False):
            return reject("ambiguous")
        if verdict.get("guessable"):
            return reject("guessable")
        event_date = self._event_date(written.get("event_date"), article.published_date or "")
        bucket = bucket_for(age_days(event_date, self.today))
        if bucket is None:
            return reject("event outside the age buckets")
        with self._lock:
            key = _norm(question)
            if key in self._seen_questions:
                self._count(result, "duplicate", locked=True)
                return None
            self._seen_questions.add(key)
        expected = "|".join(dict.fromkeys([answer, *aliases]))
        return Question(
            key="",
            prompt=question,
            expected=expected,
            category=category,
            bucket=bucket.key,
            event_date=event_date,
            source_url=article.url,
            source_title=article.title,
        )

    def _event_date(self, proposed: Any, published: str) -> str:
        """The writer's event date if plausible (not in the future, at most 30 days before publication)."""
        try:
            d = date.fromisoformat(str(proposed))
            pub = date.fromisoformat(published)
            if pub - timedelta(days=30) <= d <= min(pub, self.today):
                return d.isoformat()
        except ValueError:
            pass
        return published

    def _count(self, result: GenerationResult, why: str, *, locked: bool = False) -> None:
        if locked:
            result.rejected[why] += 1
            return
        with self._lock:
            result.rejected[why] += 1


def to_dict(q: Question) -> dict[str, Any]:
    return asdict(q)
