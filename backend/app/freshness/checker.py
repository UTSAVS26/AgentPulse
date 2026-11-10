"""Stale-answer detection: extract claims, verify the time-sensitive ones against live search.

Pipeline (``FreshnessChecker.check``)
------------------------------------
1. **Extract** atomic claims with an LLM (``EXTRACT_SYSTEM``), each flagged time-sensitive or not
   and given a neutral search query. A claim that literally says "current"/"latest"... is always
   treated as time-sensitive (heuristic override). If the LLM fails or replies with something
   unparseable, rule-based extraction (:mod:`app.freshness.heuristics`) takes over.
2. **Verify** each time-sensitive claim (in parallel):
   * claims about the current date/year/weekday are checked against the clock (no search);
   * otherwise Tavily is searched with the claim's query, and an LLM judge (``VERIFY_SYSTEM``)
     compares the claim with the dated sources → ``current | outdated | contradicted | unverifiable``.
   Timeless claims are not searched (``not_time_sensitive``), which saves search credits.
3. **Score**: ``freshness = current / (current + outdated + contradicted)`` over time-sensitive
   claims with a decisive verdict (``None`` when there are none).

Every failure degrades to ``unverifiable`` with the reason in ``explanation``; ``check`` never
raises. LLM calls go to a list of models: a rate limit (Groq's free tier has a daily token quota) or a
persistent error on one model moves straight on to the next. In mock mode the same code runs against
the mock LLM and mock search.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from app.clients.llm import LLMClient, LLMError
from app.clients.search import SearchClient, SearchError
from app.freshness import heuristics
from app.freshness.prompts import EXTRACT_SYSTEM, VERIFY_SYSTEM

VERDICTS = ("current", "outdated", "contradicted", "unverifiable", "not_time_sensitive")
DECISIVE = ("current", "outdated", "contradicted")
CHECK_VERDICTS = ("fresh", "stale", "wrong", "unverified", "timeless", "no_claims")
"""Overall verdict for a whole text: any outdated claim → ``stale``; else any contradicted →
``wrong``; else any current → ``fresh``; time-sensitive but nothing decisive → ``unverified``;
only timeless claims → ``timeless``; nothing factual → ``no_claims``."""

MAX_TEXT_CHARS = 6000


@dataclass
class ClaimCheck:
    text: str
    time_sensitive: bool
    query: str | None = None
    topic: str = "general"
    signals: list[str] = field(default_factory=list)
    verdict: str = "not_time_sensitive"
    confidence: float = 0.0
    explanation: str = ""
    correction: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    method: str = "skipped"  # skipped | llm | clock | search-error | no-sources | llm-error

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "time_sensitive": self.time_sensitive,
            "query": self.query,
            "topic": self.topic,
            "signals": self.signals,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "correction": self.correction,
            "sources": self.sources,
            "method": self.method,
        }


@dataclass
class FreshnessResult:
    claims: list[ClaimCheck]
    extraction: str  # "llm" or "heuristic (...)"
    freshness_score: float | None
    verdict: str
    latency_ms: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        c = Counter(cl.verdict for cl in self.claims)
        return {v: c.get(v, 0) for v in VERDICTS}


# ------------------------------------------------------------------------------------ scoring
def freshness_score(claims: list[ClaimCheck]) -> float | None:
    decisive = [c for c in claims if c.time_sensitive and c.verdict in DECISIVE]
    if not decisive:
        return None
    return sum(c.verdict == "current" for c in decisive) / len(decisive)


def overall_verdict(claims: list[ClaimCheck]) -> str:
    verdicts = {c.verdict for c in claims}
    if not claims:
        return "no_claims"
    if "outdated" in verdicts:
        return "stale"
    if "contradicted" in verdicts:
        return "wrong"
    if "current" in verdicts:
        return "fresh"
    if any(c.time_sensitive for c in claims):
        return "unverified"
    return "timeless"


# ------------------------------------------------------------------------------------ parsing
def parse_json_reply(text: str, require: str | None = None) -> Any:
    """Parse a model's JSON reply, tolerating code fences, prose and ``<think>`` blocks around it.

    With ``require``, the last JSON object containing that key wins (reasoning models may write other
    braces before their final answer)."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
        if require is None or (isinstance(data, dict) and require in data):
            return data
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    found: list[Any] = []
    i = 0
    while i < len(cleaned):
        if cleaned[i] in "{[":
            try:
                obj, length = decoder.raw_decode(cleaned[i:])
                found.append(obj)
                i += length
                continue
            except ValueError:
                pass
        i += 1
    if require is not None:
        matches = [o for o in found if isinstance(o, dict) and require in o]
        if matches:
            return matches[-1]
    if found:
        return found[0]
    raise ValueError("no JSON found in reply")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# ------------------------------------------------------------------------------------ clock check
# Claims/questions about *now* (not "Which year did WW2 end?").
_DATE_CLAIM = re.compile(
    r"\b(what|which) (year|day|date) is it\b|\bwhat is the (current )?(date|year)\b|"
    r"\bcurrent (year|date|day)\b|\bday of the week\b.{0,30}\btoday\b|\btoday'?s date\b|"
    r"\bthe (current )?year is\b|\bit is (now )?(the year )?(19|20)\d{2}\b|\bwe are in (19|20)\d{2}\b|"
    r"\btoday is\b",
    re.IGNORECASE,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def clock_check(claim: str, today: date) -> tuple[str, str, str | None] | None:
    """Verdict for claims about the current date/year/weekday, or None if not such a claim."""
    if not _DATE_CLAIM.search(claim):
        return None
    lower = claim.lower()
    years = {int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", lower)}
    days = {d for d in _WEEKDAYS if re.search(rf"\b{d}\b", lower)}
    today_str = f"{today:%A} {today.isoformat()} (UTC)"
    if years:
        if today.year in years:
            return "current", f"The clock says today is {today_str}.", None
        verdict = "outdated" if max(years) < today.year else "contradicted"
        return verdict, f"Claims {sorted(years)}, but today is {today_str}.", f"It is {today.year}."
    if days:
        weekday = _WEEKDAYS[today.weekday()]
        if weekday in days:
            return "current", f"The clock says today is {today_str}.", None
        return "contradicted", f"Claims {sorted(days)}, but today is {today_str}.", f"Today is {today:%A}."
    return None


# ------------------------------------------------------------------------------------ model cooldowns
class _Cooldowns:
    """Process-wide memory of rate-limited models, per LLM client (mock and live never mix).

    A daily quota ("tokens per day") benches a model for an hour, a per-minute limit for 20 s. Benched
    models are tried last instead of first, so one exhausted model doesn't add a failed call to every
    request. (Benchmarked agents share these quotas.)"""

    DAILY_S = 3600.0
    MINUTE_S = 20.0

    def __init__(self) -> None:
        self._until: dict[tuple[int, str], tuple[float, bool]] = {}  # -> (benched until, daily quota)
        self._lock = threading.Lock()

    def mark(self, llm: object, model: str, exc: LLMError) -> bool:
        """Bench ``model``; returns True for a daily quota."""
        text = exc.message.lower()
        daily = any(k in text for k in ("per day", "tpd", "rpd", "daily"))
        with self._lock:
            self._until[(id(llm), model)] = (
                time.monotonic() + (self.DAILY_S if daily else self.MINUTE_S),
                daily,
            )
        return daily

    def out_for_the_day(self, llm: object, model: str) -> bool:
        with self._lock:
            until, daily = self._until.get((id(llm), model), (0.0, False))
        return daily and until > time.monotonic()

    def order(self, llm: object, models: list[str]) -> list[str]:
        """Models not benched first (configured order), then benched ones, soonest-free first."""
        now = time.monotonic()
        with self._lock:
            until = {m: self._until.get((id(llm), m), (0.0, False))[0] for m in models}
        ready = [m for m in models if until[m] <= now]
        benched = sorted((m for m in models if until[m] > now), key=lambda m: until[m])
        return ready + benched

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


_cooldowns = _Cooldowns()


# ------------------------------------------------------------------------------------ checker
def chat_with_fallback(
    llm: LLMClient,
    models: list[str],
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    retry_backoff_s: float = 0.5,
    rate_limit_backoff_s: float = 2.0,
) -> str:
    """Ask ``models`` in order (rate-limited ones benched, see ``_cooldowns``) and return the first reply.

    Transient errors are retried; a model that spends its whole budget thinking (empty reply) hands over to
    the next; if every model is per-minute limited, wait the limit out (up to a full minute) and try again.
    """
    last: LLMError | None = None
    for round_ in range(6):  # waits 2+4+8+16+30 s: covers a full per-minute window
        minute_limited = False
        for model in _cooldowns.order(llm, models):
            if round_ and _cooldowns.out_for_the_day(llm, model):
                continue
            for attempt in range(3):
                try:
                    res = llm.chat(model, messages, temperature=0.0, max_tokens=max_tokens)
                    if not (res.content or "").strip() and res.finish_reason == "length":
                        # a reasoning model spent the whole budget thinking: try the next model
                        raise LLMError(
                            "empty reply (token budget used up)", retryable=False, kind="truncated"
                        )
                    return res.content
                except LLMError as exc:
                    last = exc
                    if exc.kind == "rate_limited":
                        minute_limited |= not _cooldowns.mark(llm, model, exc)
                        break  # next model: don't hammer a rate-limited one
                    if not exc.retryable or attempt == 2:
                        break
                    time.sleep(retry_backoff_s * (2**attempt))
        if not minute_limited:
            break
        time.sleep(min(30.0, rate_limit_backoff_s * (2**round_)))  # wait out a per-minute limit
    if last is None:
        raise LLMError("no model available", retryable=False, kind="unavailable")
    raise last


class FreshnessChecker:
    """Checks texts for stale claims. Thread-safe; optionally caches results (use one checker per
    benchmark run so identical answers are verified once)."""

    def __init__(
        self,
        llm: LLMClient,
        search: SearchClient,
        model: str | list[str],
        *,
        search_depth: str = "advanced",
        max_results: int = 5,
        max_claims: int = 8,
        today: date | None = None,
        cache: bool = False,
        workers: int = 4,
        retry_backoff_s: float = 0.5,
        rate_limit_backoff_s: float = 2.0,
    ):
        self.llm = llm
        self.search = search
        self.models = [model] if isinstance(model, str) else [m for m in model if m]
        if not self.models:
            raise ValueError("at least one model is required")
        self.search_depth = search_depth
        self.max_results = max_results
        self.max_claims = max_claims
        self._today = today
        self.cache_enabled = cache
        self.workers = workers
        self.retry_backoff_s = retry_backoff_s
        self.rate_limit_backoff_s = rate_limit_backoff_s
        self._extract_cache: dict[str, tuple[list[ClaimCheck], str]] = {}
        self._verify_cache: dict[str, ClaimCheck] = {}
        self._lock = threading.Lock()

    @property
    def today(self) -> date:
        return self._today or datetime.now(UTC).date()

    # -------------------------------------------------------------------------- public API
    def check(self, text: str, question: str | None = None) -> FreshnessResult:
        started = time.perf_counter()
        text = (text or "").strip()[:MAX_TEXT_CHARS]
        clock_claim = self._date_question_claim(text, question)
        if clock_claim is not None:
            claims, extraction = [clock_claim], "clock (date question)"
        else:
            claims, extraction = self.extract(text, question) if text else ([], "none")
        to_verify = [c for c in claims if c.time_sensitive][: self.max_claims]
        chosen = {id(c) for c in to_verify}
        for c in claims:
            if c.time_sensitive and id(c) not in chosen:
                c.verdict, c.method = "unverifiable", "skipped"
                c.explanation = f"Not checked: only the first {self.max_claims} time-sensitive claims are."
        if len(to_verify) > 1 and self.workers > 1:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(to_verify))) as pool:
                verified = list(pool.map(self.verify, to_verify))
        else:
            verified = [self.verify(c) for c in to_verify]
        by_id = {id(c): v for c, v in zip(to_verify, verified, strict=True)}
        final = [by_id.get(id(c), c) for c in claims]
        for c in final:
            if not c.time_sensitive:
                c.verdict, c.method = "not_time_sensitive", "skipped"
                c.explanation = c.explanation or "Timeless claim: not searched."
        return FreshnessResult(
            claims=final,
            extraction=extraction,
            freshness_score=freshness_score(final),
            verdict=overall_verdict(final),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _date_question_claim(text: str, question: str | None) -> ClaimCheck | None:
        """A short answer to "what year/day is it?" is checked against the clock directly."""
        if not (text and question and _DATE_CLAIM.search(question)) or len(text) > 300:
            return None
        answer = heuristics.strip_filler(" ".join(heuristics.split_sentences(text)))
        if not answer or heuristics.HEDGE.match(answer):
            return None
        return ClaimCheck(
            text=f"{answer} (answer to: {question.strip()})",
            time_sensitive=True,
            query=question.strip(),
            signals=heuristics.temporal_signals(question),
        )

    def extract(self, text: str, question: str | None = None) -> tuple[list[ClaimCheck], str]:
        key = _norm(f"{question or ''}||{text}")
        if self.cache_enabled:
            with self._lock:
                if key in self._extract_cache:
                    cached, how = self._extract_cache[key]
                    return [replace(c, signals=list(c.signals)) for c in cached], how
        try:
            claims, how = self._extract_llm(text, question), "llm"
        except (LLMError, ValueError, TypeError, KeyError) as exc:
            reason = exc.kind if isinstance(exc, LLMError) else "unparseable reply"
            claims, how = self._extract_heuristic(text, question), f"heuristic (LLM {reason})"
        if self.cache_enabled:
            with self._lock:
                self._extract_cache[key] = (claims, how)
            claims = [replace(c, signals=list(c.signals)) for c in claims]
        return claims, how

    def verify(self, claim: ClaimCheck) -> ClaimCheck:
        """Verify one time-sensitive claim (returns a new object)."""
        claim = replace(claim)
        clock = clock_check(f"{claim.text} {claim.query or ''}", self.today)
        if clock:
            claim.verdict, claim.explanation, claim.correction = clock
            claim.confidence, claim.method = 0.99, "clock"
            claim.sources = [
                {"title": "System clock (UTC)", "url": "", "published_date": self.today.isoformat()}
            ]
            return claim

        key = _norm(f"{claim.query}||{claim.text}")
        if self.cache_enabled:
            with self._lock:
                if key in self._verify_cache:
                    hit = self._verify_cache[key]
                    return replace(hit, text=claim.text, signals=claim.signals, sources=list(hit.sources))
        result = self._verify_uncached(claim)
        if self.cache_enabled and result.method in ("llm", "no-sources"):
            with self._lock:
                self._verify_cache[key] = result
        return result

    # -------------------------------------------------------------------------- internals
    def _chat(self, system: str, payload: dict[str, Any], max_tokens: int) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        return chat_with_fallback(
            self.llm,
            self.models,
            messages,
            max_tokens,
            retry_backoff_s=self.retry_backoff_s,
            rate_limit_backoff_s=self.rate_limit_backoff_s,
        )

    def _extract_llm(self, text: str, question: str | None) -> list[ClaimCheck]:
        payload: dict[str, Any] = {"today": self.today.isoformat(), "text": text}
        if question:
            payload["question"] = question
        data = parse_json_reply(self._chat(EXTRACT_SYSTEM, payload, max_tokens=4096), require="claims")
        items = data.get("claims") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("no 'claims' list in reply")
        claims: list[ClaimCheck] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            text_ = str(item.get("claim") or item.get("text") or "").strip()
            if not text_ or _norm(text_) in seen:
                continue
            seen.add(_norm(text_))
            signals = heuristics.temporal_signals(text_)
            ts = _as_bool(item.get("time_sensitive")) or heuristics.strong_signal(text_)
            topic = str(item.get("topic") or "general").lower()
            claims.append(
                ClaimCheck(
                    text=text_[:1000],
                    time_sensitive=ts,
                    query=(str(item.get("query") or "").strip() or text_)[:400],
                    topic=topic if topic in ("news", "general") else "general",
                    signals=signals,
                )
            )
        return claims

    def _extract_heuristic(self, text: str, question: str | None) -> list[ClaimCheck]:
        return [
            ClaimCheck(text=h.text, time_sensitive=h.time_sensitive, query=h.query, signals=h.signals)
            for h in heuristics.heuristic_claims(text, question, self.max_claims * 2)
        ]

    def _search(self, claim: ClaimCheck):
        for attempt in range(2):
            try:
                return self.search.search(
                    claim.query or claim.text,
                    max_results=self.max_results,
                    topic=claim.topic if claim.topic in ("news", "general") else "general",
                    search_depth=self.search_depth,
                    include_answer=True,
                )
            except SearchError as exc:
                if not exc.retryable or attempt == 1:
                    raise
                time.sleep(self.retry_backoff_s)
        raise AssertionError("unreachable")  # pragma: no cover

    def _verify_uncached(self, claim: ClaimCheck) -> ClaimCheck:
        try:
            res = self._search(claim)
        except SearchError as exc:
            claim.verdict, claim.method, claim.confidence = "unverifiable", "search-error", 0.0
            claim.explanation = f"Search failed, so the claim could not be checked: {exc.message}"
            return claim

        claim.sources = sorted(
            (
                {
                    "title": r.title,
                    "url": r.url,
                    "published_date": r.published_date,
                    "snippet": r.content[:400],
                }
                for r in res.results
            ),
            key=lambda s: s["published_date"] or "",
            reverse=True,
        )
        if not res.results and not res.answer:
            claim.verdict, claim.method, claim.confidence = "unverifiable", "no-sources", 0.0
            claim.explanation = "The search returned no sources for this claim."
            return claim

        payload = {
            "today": self.today.isoformat(),
            "claim": claim.text,
            "search_answer": res.answer,
            "sources": [
                {
                    "n": i + 1,
                    "title": r.title,
                    "url": r.url,
                    "published_date": r.published_date,
                    "content": r.content[:700],
                }
                for i, r in enumerate(res.results)
            ],
        }
        try:
            data = parse_json_reply(self._chat(VERIFY_SYSTEM, payload, max_tokens=4096), require="verdict")
            if not isinstance(data, dict):
                raise ValueError("reply is not an object")
            verdict = str(data.get("verdict", "")).strip().lower()
            if verdict not in DECISIVE + ("unverifiable",):
                raise ValueError(f"unknown verdict {verdict!r}")
            if verdict == "contradicted" and _as_bool(data.get("was_true_before")):
                verdict = "outdated"  # was true once: that is a stale answer, not a never-true one
            claim.verdict = verdict
            claim.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
            claim.explanation = str(data.get("explanation") or "")[:1000]
            correction = str(data.get("correction") or "").strip()
            claim.correction = correction[:500] if correction and verdict != "current" else None
            claim.method = "llm"
        except (LLMError, ValueError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, LLMError) else "unparseable verdict"
            claim.verdict, claim.method, claim.confidence = "unverifiable", "llm-error", 0.0
            summary = f" Search summary: {res.answer}" if res.answer else ""
            claim.explanation = f"Could not get a verdict ({reason[:200]}).{summary}"
        return claim
