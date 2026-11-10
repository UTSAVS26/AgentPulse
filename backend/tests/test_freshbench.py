"""Phase 3.2: Live Freshness Bench (generator, curve, API, pulse) with the mock clients."""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, InlineExecutor, get_bench_engine
from app.bench.pricing import PriceBook
from app.clients.llm import LLMError
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.search import SearchError, TavilySearchClient
from app.config import Settings
from app.db import get_sessionmaker
from app.freshbench import jobs, pulse
from app.freshbench.buckets import BUCKETS, age_days, bucket_for
from app.freshbench.curve import agent_curve
from app.freshbench.generator import Generator
from app.main import app
from app.seed import seed_agents

TODAY = datetime.now(UTC).date()


# ------------------------------------------------------------------------------------ buckets
def test_buckets_are_contiguous_and_windows_match():
    assert BUCKETS[0].min_days == 0 and BUCKETS[-1].max_days == 730
    for a, b in zip(BUCKETS, BUCKETS[1:], strict=False):
        assert b.min_days == a.max_days + 1
    start, end = BUCKETS[1].window(date(2026, 11, 10))
    assert (start, end) == (date(2026, 10, 11), date(2026, 11, 2))
    assert bucket_for(0).key == "week" and bucket_for(400).key == "older" and bucket_for(800) is None
    assert age_days("2026-11-01", "2026-11-10") == 9


def test_tavily_client_sends_the_publication_window():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    client = TavilySearchClient("key", transport=httpx.MockTransport(handler))
    client.search("q", topic="news", start_date="2026-01-01", end_date="2026-01-31")
    assert seen["start_date"] == "2026-01-01" and seen["end_date"] == "2026-01-31" and seen["topic"] == "news"
    client.search("q")
    assert "start_date" not in seen or seen.get("query") == "q"


# ------------------------------------------------------------------------------------ generator
def _generator(**kw):
    defaults = dict(today=TODAY, per_cell=2, workers=2)
    defaults.update(kw)
    return Generator(
        defaults.pop("llm", MockLLMClient(latency_scale=0)),
        defaults.pop("search", MockSearchClient(latency_scale=0)),
        ["groq/openai/gpt-oss-120b"],
        **defaults,
    )


def test_mock_generation_covers_every_bucket_and_rejects_flawed_questions():
    result = _generator().run()
    summary = result.summary()
    assert summary["questions"] == 72 and set(summary["per_bucket"].values()) == {12}
    assert summary["rejected"] == {"not supported by the article": 10, "ambiguous": 10}
    keys = [q.key for q in result.questions]
    assert len(set(keys)) == len(keys) and all(k.startswith("fb-") for k in keys)
    for q in result.questions:
        answer = q.expected.split("|")[0].lower()
        assert answer not in q.prompt.lower()
        assert bucket_for(age_days(q.event_date, TODAY)).key == q.bucket
        assert q.source_url.startswith("https://mock.agentpulse.local/news/")


def test_generation_subset_and_failures_are_reported():
    result = _generator(categories=["sports"], buckets=["week", "older"], per_cell=1).run()
    assert {q.bucket for q in result.questions} == {"week", "older"} and result.searches == 2

    class DownSearch:
        def search(self, *a, **k):
            raise SearchError("simulated outage", retryable=False)

    out = _generator(search=DownSearch(), categories=["sports"], buckets=["week"]).run()
    assert out.questions == [] and "simulated outage" in out.errors[0]

    class DeadLLM(MockLLMClient):
        def chat(self, *a, **k):
            raise LLMError("no credit", retryable=False, status_code=402)

    out = _generator(llm=DeadLLM(latency_scale=0), categories=["sports"], buckets=["week"]).run()
    assert out.questions == [] and out.rejected["writer failed"] >= 1
    with pytest.raises(ValueError):
        _generator(categories=["astrology"])


# ------------------------------------------------------------------------------------ curve
def _passes(ages, cutoff, plateau=0.9, repeats=3, seed=1):
    rng = random.Random(seed)
    out = {}
    for t, a in ages.items():
        p = plateau if cutoff is None else plateau / (1 + math.exp(-(a - cutoff) / 20))
        out[t] = [rng.random() < p for _ in range(repeats)]
    return out


def test_curve_recovers_a_known_cutoff_and_flags_flat_or_weak_agents():
    rng = random.Random(7)
    ages = {i: rng.randint(0, 730) for i in range(80)}
    ref = date(2026, 11, 10)
    est = agent_curve(_passes(ages, 200), ages, ref, seed=1)["cutoff"]
    assert est["status"] == "estimated" and abs(est["days_before_snapshot"] - 200) <= 40
    lo, hi = (date.fromisoformat(d) for d in est["ci"])
    assert lo <= ref - timedelta(days=est["days_before_snapshot"]) <= hi
    assert agent_curve(_passes(ages, None), ages, ref)["cutoff"]["status"] == "no cutoff detected"
    assert agent_curve(_passes(ages, None, plateau=0.1), ages, ref)["cutoff"]["status"] == "too little signal"
    few = {t: ages[t] for t in list(ages)[:3]}
    assert agent_curve(_passes(few, 200), few, ref)["cutoff"]["status"] == "too few questions"


# ------------------------------------------------------------------------------------ API end to end
@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: (MockLLMClient(seed=3, latency_scale=0), MockSearchClient(latency_scale=0)),
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    app.dependency_overrides[get_bench_engine] = lambda: engine
    return client


def _wait(api, job_id):
    for _ in range(200):
        job = api.get(f"/freshbench/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("generation did not finish")


def test_generate_run_and_read_the_knowledge_curve(api):
    cfg = api.get("/freshbench/config").json()
    assert len(cfg["buckets"]) == 6 and "sports" in cfg["categories"] and cfg["pulse"]["hour_utc"] is None
    started = api.post("/freshbench/generate", json={}).json()
    job = _wait(api, started["job"]["id"])
    assert job["status"] == "done" and job["summary"]["questions"] == 72, job
    sid = job["suite_id"]

    again = api.post("/freshbench/generate", json={}).json()  # today's snapshot is reused
    assert again["job"] is None and again["existing_suite_id"] == sid
    snaps = api.get("/freshbench/snapshots").json()
    assert snaps[0]["suite_id"] == sid and snaps[0]["questions"] == 72 and snaps[0]["name"].endswith("(mock)")

    suite = api.get(f"/suites/{sid}").json()
    assert suite["kind"] == "freshbench" and suite["snapshot_date"] == TODAY.isoformat()
    task = suite["tasks"][0]
    assert task["event_date"] and task["source_url"]
    edit = {**{k: task[k] for k in ("key", "prompt", "expected", "scorer", "category")}}
    assert api.put(f"/suites/{sid}/tasks/{task['id']}", json=edit).status_code == 403  # read-only snapshot

    run = api.post("/runs", json={"suite_id": sid, "agent_ids": [2, 4], "repeats": 2, "fault_rate": 0}).json()
    curve = api.get(f"/runs/{run['id']}/knowledge-curve").json()
    by_name = {a["agent_name"]: a for a in curve["agents"]}
    no_tools = by_name["GPT-OSS 120B · no tools (baseline)"]["cutoff"]
    tools = by_name["GPT-OSS 120B · tools"]["cutoff"]
    assert no_tools["status"] == "estimated" and 120 <= no_tools["days_before_snapshot"] <= 300, no_tools
    assert tools["status"] == "no cutoff detected", tools
    assert api.get(f"/runs/{run['id']}/metrics").json()["knowledge_curve"]["questions"] == 72

    hist = api.get("/freshbench/history").json()
    assert {a["agent_name"] for a in hist["agents"]} >= {"GPT-OSS 120B · tools"} and hist["alerts"] == []

    # a copy is editable, and editing it from the dashboard table keeps the event date
    copy = api.post(f"/suites/{sid}/duplicate").json()
    ctask = copy["tasks"][0]
    res = api.put(f"/suites/{copy['id']}/tasks/{ctask['id']}", json={**edit, "key": ctask["key"]})
    assert res.status_code == 200 and res.json()["event_date"] == ctask["event_date"]
    assert api.delete(f"/runs/{run['id']}").status_code == 204
    assert api.delete(f"/suites/{sid}").status_code == 204  # snapshots can be deleted


def test_generate_rejects_unknown_options_and_concurrent_jobs(api, monkeypatch):
    assert api.post("/freshbench/generate", json={"categories": ["astrology"]}).status_code == 422
    assert api.post("/freshbench/generate", json={"buckets": ["decade"]}).status_code == 422
    monkeypatch.setattr(jobs, "running_job", lambda: True)
    busy = jobs.Job(id=999, live=False, params={})
    monkeypatch.setitem(jobs._jobs, 999, busy)
    assert api.post("/freshbench/generate", json={"force": True}).status_code == 409
    jobs._jobs.pop(999, None)
    assert api.get("/runs/1/knowledge-curve").status_code == 404


def test_pulse_tick_and_history_drift(api, db_engine):
    sessions = get_sessionmaker()
    off = Settings(mock_mode=True)
    assert pulse.tick(off, sessions, get_bench_engine) == "off"
    early = Settings(mock_mode=True, freshbench_pulse_hour_utc=23)
    assert pulse.tick(early, sessions, get_bench_engine, now=datetime(2026, 1, 1, 1, tzinfo=UTC)) == (
        "waiting for the hour"
    )
    on = Settings(mock_mode=True, freshbench_pulse_hour_utc=0)
    assert pulse.tick(on, sessions, get_bench_engine) == "started"
    job = jobs.jobs()[0]
    _wait(api, job.id)
    assert job.source == "pulse" and job.status == "done"
    assert pulse.tick(on, sessions, get_bench_engine) == "today's snapshot exists"

    from app.freshbench import pulse as p

    h = p.history(Session(db_engine), drift_points=10)
    assert "alerts" in h and "pulse" in h


@pytest.mark.parametrize(
    ("answer", "is_date"),
    [
        ("November 2026", True),
        ("Nov 2026", True),
        ("2025", True),
        ("2026-11-04", True),
        ("March 3, 2026", True),
        ("Tuesday", True),
        ("Darren Fichardt", False),
        ("May Robotics", False),
        ("Jan Novak", False),
        ("$0.73", False),
        ("1.34 billion", False),
    ],
)
def test_date_answers_are_rejected(answer, is_date):
    """Every question already names its month: an answer that is just a date is a weak question."""
    from app.freshbench.generator import is_date_answer

    assert is_date_answer(answer) is is_date


@pytest.mark.parametrize(("raw", "hour"), [("", None), ("off", None), ("6", 6), (" 23 ", 23)])
def test_pulse_hour_setting_accepts_empty_as_off(raw, hour):
    """docker compose passes an empty string when FRESHBENCH_PULSE_HOUR_UTC is unset: that must mean off."""
    assert Settings(freshbench_pulse_hour_utc=raw).freshbench_pulse_hour_utc == hour


def test_pulse_hour_setting_rejects_bad_hours():
    with pytest.raises(ValueError, match="0 to 23"):
        Settings(freshbench_pulse_hour_utc="25")
