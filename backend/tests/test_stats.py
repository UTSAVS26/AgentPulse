"""Phase 3.1: statistics (hand-checked values) and the jury scorer."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session

from app.bench import stats
from app.bench.engine import BenchmarkEngine, InlineExecutor, get_bench_engine
from app.bench.metrics import ExecRecord, run_metrics
from app.bench.pricing import PriceBook
from app.bench.scoring import score_jury, score_output
from app.clients.llm import LLMError, LLMResponse
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.db import get_sessionmaker
from app.main import app
from app.seed import seed_agents


def rec(agent, task, passed, repeat=0, **kw):
    base = ExecRecord(
        agent, task, repeat, passed, float(passed), "ok", 100.0, 10, 5, 0.0, 1, 0, 0, 0, 0, False
    )
    return replace(base, **kw)


# ------------------------------------------------------------------------------------ intervals & tests
def test_wilson_known_values():
    lo, hi = stats.wilson(8, 10)
    assert lo == pytest.approx(0.4902, abs=1e-4) and hi == pytest.approx(0.9433, abs=1e-4)
    lo, hi = stats.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-4)
    assert stats.wilson(0, 0) is None


def test_fleiss_kappa_hand_computed():
    items = [[True, True, True], [False, False, False], [True, True, False], [True, False, False]]
    assert stats.fleiss_kappa(items) == pytest.approx(1 / 3)
    assert stats.fleiss_kappa([[True, True], [False, False]]) == pytest.approx(1.0)
    assert (
        stats.fleiss_kappa([[True, True, True], [True, True, True]]) is None
    )  # no variation: not measurable
    assert stats.fleiss_kappa([[True, True]]) is None  # one item: undefined
    assert stats.kappa_label(0.7) == "substantial agreement" and stats.kappa_label(None).startswith(
        "not measurable"
    )


def test_paired_p_value_extremes():
    same = [0.5] * 100
    assert stats.paired_p_value(same, same) == 1.0
    assert stats.paired_p_value([0.9] * 100, [0.1] * 100) == 0.0
    assert stats.paired_p_value([0.5] * 5, [0.5] * 5) is None  # too few draws


def test_bootstrap_is_seeded_and_separates_clear_winner_from_tie():
    records = {
        1: [rec(1, t, True) for t in range(10)],
        2: [rec(2, t, False) for t in range(10)],
        3: [rec(3, t, True) for t in range(10)],
    }

    def score(rs):
        return sum(r.passed for r in rs) / len(rs)

    a = stats.paired_bootstrap(records, score, seed=7, resamples=300)
    b = stats.paired_bootstrap(records, score, seed=7, resamples=300)
    assert a == b  # reproducible
    tiers, comparisons = stats.rank_tiers([1, 3, 2], a)
    assert tiers == {1: 1, 3: 1, 2: 2}  # 1 and 3 tie; 2 is clearly worse
    assert comparisons[0]["p"] == 1.0 and comparisons[1]["p"] == 0.0


def test_duplicate_task_draws_stay_separate_tasks():
    # consistency needs per-task grouping: a task drawn twice must not merge its repeats
    records = {1: [rec(1, 0, True, 0), rec(1, 0, False, 1)]}
    draws = stats.paired_bootstrap(
        records, lambda rs: len({r.task_id for r in rs}) / len(rs), seed=1, resamples=50
    )
    assert all(v == 0.5 for v in draws[1])


def test_power_note():
    note = stats.power_note(6, 2, [0.2, 0.25, 0.3])
    assert "6 tasks × 2 repeats" in note and "±25 points" in note and "More tasks or repeats" in note
    assert "More tasks" not in stats.power_note(30, 3, [0.05])


# ------------------------------------------------------------------------------------ run metrics
def test_run_metrics_adds_intervals_tiers_and_statistics():
    records = [rec(1, t, True, r) for t in range(8) for r in range(2)]
    records += [rec(2, t, t < 2, r) for t in range(8) for r in range(2)]
    records += [rec(3, t, True, r) for t in range(8) for r in range(2)]
    agents = {i: {"name": f"A{i}", "model": "m"} for i in (1, 2, 3)}
    out = run_metrics(records, agents, {t: {"key": f"t{t}"} for t in range(8)}, seed=3)
    rows = {r["agent_name"]: r for r in out["leaderboard"]}
    assert rows["A1"]["tier"] == rows["A3"]["tier"] == 1 and rows["A2"]["tier"] == 2
    assert rows["A1"]["tied_with"] == ["A3"]
    lo, hi = rows["A2"]["reliability_ci"]
    assert lo <= rows["A2"]["reliability_score"] <= hi
    assert rows["A2"]["accuracy_ci"][0] < 0.25 < rows["A2"]["accuracy_ci"][1]
    st = out["statistics"]
    assert st["tasks"] == 8 and st["repeats"] == 2 and st["resamples"] >= 200
    assert any(c["significant"] for c in st["comparisons"]) and "±" in st["power_note"]
    assert out["jury"] is None
    again = run_metrics(records, agents, {t: {"key": f"t{t}"} for t in range(8)}, seed=3)
    assert again["leaderboard"] == out["leaderboard"]  # deterministic per seed


def test_run_metrics_handles_empty_and_single_agent():
    empty = run_metrics([], {1: {"name": "A", "model": "m"}}, {})
    assert empty["leaderboard"][0]["tier"] is None and empty["statistics"]["resamples"] == 0
    one = run_metrics([rec(1, 0, True)], {1: {"name": "A", "model": "m"}}, {0: {"key": "t"}})
    assert one["leaderboard"][0]["tier"] == 1 and one["leaderboard"][0]["tied_with"] == []


# ------------------------------------------------------------------------------------ jury
class Panel:
    """Juror model → reply (a JSON string) or an exception."""

    def __init__(self, replies):
        self.replies = replies

    def chat(self, model, messages, **kw):
        reply = self.replies[model]
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(content=reply, tool_calls=[], prompt_tokens=1, completion_tokens=1, latency_ms=1)


def test_jury_majority_and_failures():
    panel = Panel(
        {
            "a/x": '{"score": 0.9, "reason": "ok"}',
            "b/y": '{"score": 0.8, "reason": "fine"}',
            "c/z": '{"score": 0.1, "reason": "wrong"}',
            "d/w": LLMError("down", retryable=False, kind="provider_error"),
        }
    )
    r = score_jury("Lima", "Lima", "Capital of Peru?", panel, ["a/x", "b/y", "c/z", "d/w"])
    assert r.passed and r.score == pytest.approx(0.6) and "jury 2/3 pass" in r.detail
    assert "w failed (provider_error)" in r.detail
    assert [v.get("passed") for v in r.votes] == [True, True, False, None]


def test_jury_tie_uses_mean_and_all_failed_falls_back():
    tie = Panel({"a/x": '{"score": 0.9}', "b/y": '{"score": 0.2}'})
    assert score_jury("x", "x", "q", tie, ["a/x", "b/y"]).passed is True  # mean 0.55
    dead = Panel({"a/x": LLMError("no", retryable=False), "b/y": "no json here"})
    r = score_jury("Lima", "Lima", "q", dead, ["a/x", "b/y"])
    assert "every juror failed" in r.detail and r.passed and len(r.votes) == 2
    assert score_output("Lima", scorer="jury", expected="Lima").detail.startswith("no jury configured")


def test_mock_jury_end_to_end(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
    mock = (MockLLMClient(seed=2, latency_scale=0), MockSearchClient(latency_scale=0))
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: mock,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    app.dependency_overrides[get_bench_engine] = lambda: engine
    tasks = [
        {"key": f"q{i}", "prompt": f"What is {i} + {i}?", "expected": str(2 * i), "scorer": "jury"}
        for i in range(1, 9)
    ]
    sid = client.post("/suites", json={"name": "Jury suite", "tasks": tasks}).json()["id"]
    rid = client.post("/runs", json={"suite_id": sid, "agent_ids": [1, 4], "repeats": 2}).json()["id"]
    data = client.get(f"/runs/{rid}/metrics").json()
    jury = data["jury"]
    assert jury["jurors"] == 3 and jury["verdicts"] == 32
    assert jury["kappa"] is not None and 0 <= jury["unanimous_share"] <= 1
    ex = client.get(f"/runs/{rid}/executions").json()[0]
    assert ex["score_detail"].startswith("jury ")
    assert all(r["reliability_ci"] for r in data["leaderboard"])
    # every juror's vote is exposed on the execution (API, JSON report, executions CSV)
    detail = client.get(f"/executions/{ex['id']}").json()
    assert len(detail["jury"]) == 3 and {"model", "passed", "score"} <= set(detail["jury"][0])
    report = client.get(f"/reports/{rid}", params={"format": "json"}).json()
    assert len(report["executions"][0]["jury"]) == 3
    csv_text = client.get(f"/reports/{rid}", params={"format": "csv", "table": "executions"}).text
    assert "jury_votes" in csv_text.splitlines()[0] and "claude-opus-5.5=" in csv_text
    json.dumps(data)


def test_all_pass_is_not_reported_as_certain():
    records = [rec(a, t, True) for a in (1, 2) for t in range(5)]
    out = run_metrics(records, {1: {"name": "A", "model": "m"}, 2: {"name": "B", "model": "m"}}, {}, seed=1)
    rows = out["leaderboard"]
    assert all(r["reliability_ci"] is None for r in rows)  # degenerate bootstrap interval hidden
    assert rows[0]["accuracy_ci"][0] == pytest.approx(0.5655, abs=1e-3)  # Wilson for 5/5
    assert "±22 points" in out["statistics"]["power_note"] and rows[0]["tier"] == rows[1]["tier"] == 1


def test_mock_juror_matches_answers_with_trailing_periods():
    import random

    from app.bench.scoring import JUDGE_PROMPT
    from app.clients.mock_judge import judge_responder

    prompt = JUDGE_PROMPT.format(question="2+2?", expected="4", output="The answer is 4.")
    rng = random.Random(0)
    scores = [
        json.loads(judge_responder("x/judge", [{"role": "user", "content": prompt}], rng))["score"]
        for _ in range(50)
    ]
    assert sum(s >= 0.5 for s in scores) >= 45  # correct answer passes (bar a few simulated juror errors)
    wrong = JUDGE_PROMPT.format(question="2+2?", expected="4", output="The answer is 5.")
    assert (
        json.loads(judge_responder("x/judge", [{"role": "user", "content": wrong}], random.Random(1)))[
            "score"
        ]
        < 0.5
    )
