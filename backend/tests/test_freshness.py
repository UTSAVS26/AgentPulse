"""Stale-answer detection: heuristics, parsing, clock checks, the checker (mock + failure paths),
the /freshness API and the benchmark integration."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, InlineExecutor, get_bench_engine
from app.bench.metrics import ExecRecord, agent_metrics
from app.bench.pricing import PriceBook
from app.clients.llm import LLMError, LLMResponse
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchResponse, SearchResult
from app.config import Settings
from app.db import get_sessionmaker
from app.freshness import heuristics
from app.freshness.checker import (
    ClaimCheck,
    FreshnessChecker,
    _cooldowns,
    clock_check,
    freshness_score,
    overall_verdict,
    parse_json_reply,
)
from app.freshness.prompts import EXTRACT_SYSTEM, VERIFY_SYSTEM
from app.main import app
from app.models import Agent, FreshnessCheck, TaskExecution, TaskSuite
from app.seed import seed_agents, seed_suites

TODAY = date(2026, 11, 5)  # a Thursday
MODEL = "groq/openai/gpt-oss-120b"
DEMO = (
    "The current UK Prime Minister is Rishi Sunak. Water boils at 100 °C at sea level. "
    "Argentina won the latest FIFA World Cup."
)


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    _cooldowns.clear()  # rate-limit memory is process-wide
    yield
    _cooldowns.clear()


class CountingSearch(MockSearchClient):
    def __init__(self, **kw):
        super().__init__(latency_scale=0, **kw)
        self.queries: list[str] = []

    def search(self, query, *args, **kwargs):
        self.queries.append(query)
        return super().search(query, *args, **kwargs)


def checker(llm=None, search=None, **kw) -> FreshnessChecker:
    return FreshnessChecker(
        llm or MockLLMClient(latency_scale=0),
        search or CountingSearch(),
        MODEL,
        today=TODAY,
        retry_backoff_s=0,
        **kw,
    )


class ScriptedLLM:
    """Replies per system prompt: a string, or an exception to raise."""

    def __init__(self, extract, verify=None):
        self.replies = {EXTRACT_SYSTEM: extract, VERIFY_SYSTEM: verify}
        self.calls = 0

    def chat(self, model, messages, tools=None, temperature=0.0, max_tokens=None):
        self.calls += 1
        reply = self.replies[messages[0]["content"]]
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(content=reply, tool_calls=[], prompt_tokens=1, completion_tokens=1, latency_ms=1)


# ------------------------------------------------------------------------------------ heuristics
@pytest.mark.parametrize(
    ("sentence", "sensitive"),
    [
        ("The current UK Prime Minister is Rishi Sunak.", True),
        ("Argentina won the latest FIFA World Cup.", True),
        ("The tallest building in the world is the Burj Khalifa.", True),
        ("Python 3.13 is the newest release.", True),
        ("The CEO of Twitter is Elon Musk.", True),
        ("Water boils at 100 °C at sea level.", False),
        ("The Berlin Wall fell in 1989.", False),
        ("Pi is approximately 3.14159.", False),
        ("Paris is a beautiful city with great food.", False),
    ],
)
def test_time_sensitivity_rules(sentence, sensitive):
    assert heuristics.is_time_sensitive(sentence) is sensitive


def test_signals_and_sentence_splitting():
    assert "word:current" in heuristics.temporal_signals("The current PM")
    assert "year:1989" in heuristics.temporal_signals("in 1989")
    parts = heuristics.split_sentences("Dr. Smith uses Python 3.13 daily. It is great!\n- Bullet two here")
    assert parts == ["Dr. Smith uses Python 3.13 daily.", "It is great!", "Bullet two here"]


def test_heuristic_claims_skip_hedges_and_use_question_context():
    assert heuristics.heuristic_claims("I'm not certain about this.") == []
    assert heuristics.heuristic_claims("The answer is I'm not certain about this.", question="Who won?") == []
    [c] = heuristics.heuristic_claims("The answer is Keir Starmer.", question="Who is the current UK PM?")
    assert c.text.startswith("Keir Starmer") and c.time_sensitive and c.query == "Who is the current UK PM?"


def test_parse_json_reply_tolerates_fences_and_prose():
    assert parse_json_reply('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_reply('Sure! Here you go: {"verdict": "current"} hope it helps') == {
        "verdict": "current"
    }
    assert parse_json_reply("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        parse_json_reply("no json at all")
    with pytest.raises(ValueError):
        parse_json_reply("")


@pytest.mark.parametrize(
    ("claim", "verdict"),
    [
        ("The current year is 2024.", "outdated"),
        ("It is 2026 (answer to: What year is it right now?)", "current"),
        ("The current year is 2031.", "contradicted"),
        ("Thursday (answer to: What day of the week is it today?)", "current"),
        ("Monday (answer to: What day of the week is it today?)", "contradicted"),
    ],
)
def test_clock_check(claim, verdict):
    assert clock_check(claim, TODAY)[0] == verdict


def test_clock_check_ignores_other_claims():
    assert clock_check("Spain won the 2026 World Cup.", TODAY) is None


def test_score_and_overall_verdict():
    def c(v, ts=True):
        return ClaimCheck(text=v, time_sensitive=ts, verdict=v)

    assert freshness_score([c("current"), c("outdated"), c("unverifiable")]) == 0.5
    assert freshness_score([c("unverifiable"), c("not_time_sensitive", ts=False)]) is None
    assert overall_verdict([c("current"), c("outdated")]) == "stale"
    assert overall_verdict([c("current"), c("contradicted")]) == "wrong"
    assert overall_verdict([c("current")]) == "fresh"
    assert overall_verdict([c("unverifiable")]) == "unverified"
    assert overall_verdict([c("not_time_sensitive", ts=False)]) == "timeless"
    assert overall_verdict([]) == "no_claims"


# ------------------------------------------------------------------------------------ checker (mock)
def test_mock_check_flags_stale_claims_and_skips_timeless_ones():
    search = CountingSearch()
    r = checker(search=search).check(DEMO)
    assert r.extraction == "llm"  # the mock LLM answers the extraction prompt
    by_text = {c.text: c for c in r.claims}
    sunak = by_text["The current UK Prime Minister is Rishi Sunak."]
    assert sunak.verdict == "outdated" and "Burnham" in sunak.correction
    assert sunak.sources and all(s["url"].startswith("https://") for s in sunak.sources)
    dates = [s["published_date"] for s in sunak.sources]
    assert dates == sorted(dates, reverse=True)  # newest first
    water = by_text["Water boils at 100 °C at sea level."]
    assert water.verdict == "not_time_sensitive" and not water.time_sensitive and water.sources == []
    assert by_text["Argentina won the latest FIFA World Cup."].verdict == "outdated"
    assert len(search.queries) == 2  # the timeless claim was not searched
    assert r.verdict == "stale" and r.freshness_score == 0.0
    assert r.counts["outdated"] == 2 and r.counts["not_time_sensitive"] == 1


def test_mock_check_current_answer_is_fresh():
    r = checker().check("The answer is Burnham.", question="Who is the current Prime Minister of the UK?")
    assert [c.verdict for c in r.claims] == ["current"] and r.verdict == "fresh" and r.freshness_score == 1


def test_tavily_failure_gives_unverifiable_not_a_crash():
    r = checker(search=CountingSearch(failure_rate=1.0)).check(DEMO)
    ts = [c for c in r.claims if c.time_sensitive]
    assert ts and all(c.verdict == "unverifiable" and c.method == "search-error" for c in ts)
    assert "Search failed" in ts[0].explanation
    assert r.verdict == "unverified" and r.freshness_score is None


def test_unparseable_extraction_falls_back_to_heuristics():
    llm = ScriptedLLM(extract="I think there are some claims here.", verify=LLMError("down", retryable=False))
    r = checker(llm=llm).check(DEMO)
    assert r.extraction == "heuristic (LLM unparseable reply)"
    assert [c.time_sensitive for c in r.claims] == [True, False, True]


def test_extraction_llm_error_falls_back_to_heuristics():
    llm = ScriptedLLM(extract=LLMError("boom", retryable=False, kind="provider_error"), verify="{}")
    assert checker(llm=llm).check(DEMO).extraction == "heuristic (LLM provider_error)"


def test_verify_llm_failure_keeps_sources_and_search_summary():
    extract = json.dumps({"claims": [{"claim": "The current UK PM is Rishi Sunak.", "time_sensitive": True}]})
    llm = ScriptedLLM(extract=extract, verify=LLMError("judge down", retryable=True))
    [c] = checker(llm=llm).check("whatever").claims
    assert c.verdict == "unverifiable" and c.method == "llm-error"
    assert "Burnham" in c.explanation and c.sources  # Tavily's answer and sources are still shown
    assert llm.calls == 1 + 3  # extraction + 3 attempts at the retryable verify error


def test_strong_words_override_llm_time_sensitivity_and_bad_verdicts_are_rejected():
    extract = json.dumps(
        {"claims": [{"claim": "The latest Python is 3.13.", "time_sensitive": False, "topic": "weird"}]}
    )
    llm = ScriptedLLM(extract=extract, verify='{"verdict": "probably", "confidence": 2}')
    [c] = checker(llm=llm).check("x").claims
    assert c.time_sensitive and c.topic == "general"
    assert c.verdict == "unverifiable" and c.method == "llm-error"


def test_no_sources_is_unverifiable():
    class Empty(CountingSearch):
        def search(self, query, *a, **k):
            return SearchResponse(query=query, answer=None, results=[])

    r = checker(search=Empty()).check("The current UK Prime Minister is Rishi Sunak.")
    assert r.claims[0].verdict == "unverifiable" and r.claims[0].method == "no-sources"


def test_cache_verifies_identical_claims_once():
    search = CountingSearch()
    ch = checker(search=search, cache=True)
    q = "Who is the current Prime Minister of the United Kingdom?"
    for _ in range(4):
        assert ch.check("The answer is Keir Starmer.", question=q).verdict == "stale"
    assert len(search.queries) == 1


def test_real_shaped_search_results_are_used_by_the_judge():
    class Live(CountingSearch):
        def search(self, query, *a, **k):
            return SearchResponse(
                query=query,
                answer="Andy Burnham is the UK Prime Minister.",
                results=[
                    SearchResult("Old", "https://ex.com/old", "Sunak is PM", 0.5, "2024-05-01"),
                    SearchResult("New", "https://ex.com/new", "Burnham is PM", 0.9, "2026-07-24"),
                    SearchResult("Undated", "https://ex.com/u", "PM bio", 0.1, None),
                ],
            )

    extract = json.dumps({"claims": [{"claim": "Rishi Sunak is the current UK PM.", "time_sensitive": True}]})
    verify = json.dumps(
        {"verdict": "outdated", "confidence": 0.95, "explanation": "2026-07-24", "correction": "Burnham"}
    )
    [c] = checker(llm=ScriptedLLM(extract, verify), search=Live()).check("x").claims
    assert (c.verdict, c.confidence, c.correction, c.method) == ("outdated", 0.95, "Burnham", "llm")
    assert [s["title"] for s in c.sources] == ["New", "Old", "Undated"]


# ------------------------------------------------------------------------------------ metrics
def test_agent_metrics_freshness_fields():
    def rec(i, score, verdict):
        return ExecRecord(1, i, 0, True, 1.0, "ok", 1.0, 1, 1, 0.0, 1, 0, 0, 0, 0, False, score, verdict)

    m = agent_metrics(
        [rec(1, 1.0, "fresh"), rec(2, 0.0, "stale"), rec(3, None, "no_claims"), rec(4, None, None)]
    )
    assert m["freshness_checked"] == 3 and m["freshness"] == 0.5
    assert m["stale_answers"] == 1 and m["stale_rate"] == pytest.approx(1 / 3)
    assert m["freshness_verdicts"]["fresh"] == 1 and m["freshness_verdicts"]["no_claims"] == 1
    assert agent_metrics([])["freshness"] is None


# ------------------------------------------------------------------------------------ API
def test_api_check_text(client):
    res = client.post("/freshness/check", json={"text": DEMO})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["verdict"] == "stale" and body["mock_mode"] is True and body["source"] == "text"
    assert body["counts"]["outdated"] == 2 and len(body["claims"]) == 3
    assert body["claims"][0]["sources"][0]["published_date"]

    got = client.get(f"/freshness/checks/{body['id']}").json()
    assert got["claims"] == body["claims"]
    listed = client.get("/freshness/checks").json()
    assert [c["id"] for c in listed] == [body["id"]] and "claims" not in listed[0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": "  "},
        {"text": "x", "execution_id": 1},
        {"text": "x" * 6001},
        {"text": "x", "search_depth": "deep"},
    ],
)
def test_api_check_validation(client, payload):
    assert client.post("/freshness/check", json=payload).status_code == 422


def test_api_check_execution_updates_it(client):
    with _db() as db:
        seed_agents(db)
        e = TaskExecution(
            agent_id=1,
            prompt="Who is the current Prime Minister of the United Kingdom?",
            output="Keir Starmer.",
        )
        empty = TaskExecution(agent_id=1, prompt="q", output="")
        db.add_all([e, empty])
        db.commit()
        eid, empty_id = e.id, empty.id

    body = client.post("/freshness/check", json={"execution_id": eid}).json()
    assert body["execution_id"] == eid and body["verdict"] == "stale" and body["source"] == "execution"
    assert body["question"].startswith("Who is the current Prime Minister")
    ex = client.get(f"/executions/{eid}").json()
    assert ex["freshness_verdict"] == "stale" and ex["freshness_score"] == 0.0
    assert ex["freshness_check_id"] == body["id"]
    assert client.get("/freshness/checks", params={"execution_id": eid}).json()[0]["id"] == body["id"]

    assert client.post("/freshness/check", json={"execution_id": 9999}).status_code == 404
    assert client.post("/freshness/check", json={"execution_id": empty_id}).status_code == 422
    assert client.get("/freshness/checks/9999").status_code == 404


def test_api_delete_check(client):
    cid = client.post("/freshness/check", json={"text": "The current UK PM is Rishi Sunak."}).json()["id"]
    assert client.delete(f"/freshness/checks/{cid}").status_code == 204
    assert client.get(f"/freshness/checks/{cid}").status_code == 404


def _db() -> Session:
    return get_sessionmaker()()


# ------------------------------------------------------------------------------------ benchmark integration
@pytest.fixture()
def bench(db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    mock = (MockLLMClient(seed=7, latency_scale=0), CountingSearch())
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: mock,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    return engine, mock[1]


def _run(engine, suite_name, agent_count=2, repeats=2, agent_ids=None, **kw):
    with _db() as db:
        sid = db.query(TaskSuite).filter_by(name=suite_name).one().id
        ids = agent_ids or [a.id for a in db.query(Agent).order_by(Agent.id).limit(agent_count)]
        run = engine.create_run(db, suite_id=sid, agent_ids=ids, repeats=repeats, fault_rate=0, **kw)
    engine.start(run.id)
    return run.id


def test_benchmark_checks_time_sensitive_answers(bench):
    engine, _ = bench
    run_id = _run(engine, "Current Events (time-sensitive)")
    with _db() as db:
        execs = db.query(TaskExecution).filter_by(run_id=run_id).all()
        ok = [e for e in execs if e.status == "ok" and e.output.strip()]
        assert ok and all(e.freshness_verdict for e in ok)
        assert {e.freshness_verdict for e in ok} >= {"fresh", "stale"}
        checks = db.query(FreshnessCheck).filter(FreshnessCheck.source == "benchmark").all()
        assert len(checks) == len(ok) and all(c.execution_id for c in checks)
        assert all(e.freshness_verdict is None for e in execs if e.status != "ok")


def test_benchmark_freshness_cache_searches_each_distinct_claim_once(bench):
    engine, search = bench
    with _db() as db:
        baseline = db.query(Agent).filter(Agent.tools == []).first().id  # no tools: every search is ours
    run_id = _run(engine, "Current Events (time-sensitive)", agent_ids=[baseline], repeats=3)
    with _db() as db:
        claims = {
            (c.query, c.text)
            for chk in db.query(FreshnessCheck).all()
            for c in chk.claims
            if c.method == "llm"
        }
        n_checked = (
            db.query(TaskExecution)
            .filter(TaskExecution.run_id == run_id, TaskExecution.freshness_verdict.is_not(None))
            .count()
        )
    assert search.queries and len(search.queries) == len(claims) < n_checked


def test_benchmark_metrics_include_freshness(bench, client):
    engine, _ = bench
    app.dependency_overrides[get_bench_engine] = lambda: engine
    run_id = _run(engine, "Current Events (time-sensitive)")
    board = client.get(f"/runs/{run_id}/metrics").json()["leaderboard"]
    assert all(r["freshness_checked"] > 0 for r in board)
    assert all(0 <= r["freshness"] <= 1 for r in board if r["freshness"] is not None)
    run = client.get(f"/runs/{run_id}").json()
    assert run["check_freshness"] is True
    manual = client.get("/freshness/checks", params={"manual_only": True}).json()
    assert manual == []  # benchmark checks are automatic


def test_benchmark_can_skip_freshness_and_ignores_timeless_suites(bench):
    engine, _ = bench
    skipped = _run(engine, "Current Events (time-sensitive)", agent_count=1, repeats=1, check_freshness=False)
    timeless = _run(engine, "Math & Reasoning", agent_count=1, repeats=1)
    with _db() as db:
        for rid in (skipped, timeless):
            assert all(e.freshness_verdict is None for e in db.query(TaskExecution).filter_by(run_id=rid))
        assert db.query(FreshnessCheck).count() == 0


def test_rate_limited_model_falls_back_to_the_next_one():
    class Quota(ScriptedLLM):
        def chat(self, model, messages, **kw):
            self.models_tried = [*getattr(self, "models_tried", []), model]
            if model == "groq/big":
                raise LLMError("tokens per day", retryable=True, status_code=429, kind="rate_limited")
            return super().chat(model, messages, **kw)

    extract = json.dumps({"claims": [{"claim": "The current UK PM is Rishi Sunak.", "time_sensitive": True}]})
    llm = Quota(extract=extract, verify='{"verdict": "outdated", "confidence": 0.9, "explanation": "x"}')
    ch = FreshnessChecker(llm, CountingSearch(), ["groq/big", "groq/small"], today=TODAY, retry_backoff_s=0)
    [c] = ch.check("x").claims
    assert c.verdict == "outdated"
    # extraction: big hits its daily quota -> small; verify: big is benched, small goes first
    assert llm.models_tried == ["groq/big", "groq/small", "groq/small"]


def test_per_minute_limit_on_the_last_model_is_waited_out():
    class Busy(ScriptedLLM):
        def chat(self, model, messages, **kw):
            self.n = getattr(self, "n", 0) + 1
            if self.n == 1:
                raise LLMError(
                    "tokens per minute (TPM)", retryable=True, status_code=429, kind="rate_limited"
                )
            return super().chat(model, messages, **kw)

    llm = Busy(extract='{"claims": []}')
    ch = FreshnessChecker(
        llm, CountingSearch(), MODEL, today=TODAY, retry_backoff_s=0, rate_limit_backoff_s=0
    )
    assert ch.check("Some text here.").extraction == "llm" and llm.n == 2


def test_date_questions_go_straight_to_the_clock():
    llm = ScriptedLLM(extract=AssertionError("no LLM call expected"))
    r = checker(llm=llm).check("Thursday.", question="What day of the week is it today?")
    assert r.extraction == "clock (date question)" and r.claims[0].verdict == "current" and llm.calls == 0
    r = checker().check("The answer is 2024.", question="What year is it right now?")
    assert r.verdict == "stale" and r.claims[0].method == "clock"
    # a historical question is not a date-of-today question
    r = checker().check("1945.", question="Which year did World War II end?")
    assert r.extraction != "clock (date question)" and r.verdict in ("timeless", "no_claims")


def test_settings_freshness_models_dedupes_and_keeps_order():
    s = Settings(freshness_model="a", freshness_fallback_models=" b, a ,,c")
    assert s.freshness_models == ["a", "b", "c"]
    with pytest.raises(ValueError):
        FreshnessChecker(MockLLMClient(), CountingSearch(), [])


def test_all_models_rate_limited_waits_for_a_per_minute_one_and_skips_daily_ones():
    class Crowded(ScriptedLLM):
        def chat(self, model, messages, **kw):
            self.tried = [*getattr(self, "tried", []), model]
            if model == "m/daily":
                raise LLMError("tokens per day (TPD)", retryable=True, status_code=429, kind="rate_limited")
            if model == "m/minute" and self.tried.count("m/minute") < 3:
                raise LLMError("tokens per minute", retryable=True, status_code=429, kind="rate_limited")
            return super().chat(model, messages, **kw)

    llm = Crowded(extract='{"claims": []}')
    ch = FreshnessChecker(
        llm, CountingSearch(), ["m/minute", "m/daily"], today=TODAY, retry_backoff_s=0, rate_limit_backoff_s=0
    )
    assert ch.check("Some text here.").extraction == "llm"
    assert llm.tried == ["m/minute", "m/daily", "m/minute", "m/minute"]  # daily one skipped after round 1


@pytest.mark.parametrize(("was_true_before", "expected"), [(True, "outdated"), (False, "contradicted")])
def test_contradicted_but_true_before_is_outdated(was_true_before, expected):
    extract = json.dumps(
        {"claims": [{"claim": "Argentina won the latest World Cup.", "time_sensitive": True}]}
    )
    verify = json.dumps({"verdict": "contradicted", "was_true_before": was_true_before, "confidence": 0.9})
    [c] = checker(llm=ScriptedLLM(extract, verify)).check("x").claims
    assert c.verdict == expected


def test_parse_json_reply_picks_the_answer_object_after_reasoning():
    reply = (
        '<think>maybe {"draft": 1}</think> I considered {"x": 2}. '
        'Final: {"verdict": "outdated", "confidence": 0.9}'
    )
    assert parse_json_reply(reply, require="verdict") == {"verdict": "outdated", "confidence": 0.9}
    assert parse_json_reply('{"a": 1} {"claims": []}', require="claims") == {"claims": []}
    assert parse_json_reply('{"a": 1}', require="claims") == {"a": 1}  # caller validates the shape


def test_truncated_empty_reply_moves_on_to_the_next_model():
    class Thinker(ScriptedLLM):
        def chat(self, model, messages, **kw):
            self.tried = [*getattr(self, "tried", []), model]
            if model == "m/thinker":
                return LLMResponse(
                    content="",
                    tool_calls=[],
                    prompt_tokens=1,
                    completion_tokens=4096,
                    latency_ms=1,
                    finish_reason="length",
                )
            return super().chat(model, messages, **kw)

    llm = Thinker(extract='{"claims": []}')
    ch = FreshnessChecker(llm, CountingSearch(), ["m/thinker", "m/ok"], today=TODAY, retry_backoff_s=0)
    assert ch.check("Some text here.").extraction == "llm" and llm.tried == ["m/thinker", "m/ok"]
