"""Metrics against hand-calculated expectations."""

import math

import pytest

from app.bench.metrics import ExecRecord, agent_metrics, percentile, reliability_score, run_metrics


def rec(
    task,
    rep,
    passed,
    *,
    agent=1,
    status="ok",
    latency=100.0,
    pt=10,
    ct=5,
    cost=0.001,
    faults=0,
    retries=0,
    tool_errors=0,
    recovered=False,
    steps=1,
    tools=0,
    score=None,
):
    return ExecRecord(
        agent,
        task,
        rep,
        passed,
        float(passed) if score is None else score,
        status,
        latency,
        pt,
        ct,
        cost,
        steps,
        tools,
        faults,
        retries,
        tool_errors,
        recovered,
    )


def test_percentile_linear_interpolation():
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10, 20, 30, 40, 50], 95) == pytest.approx(48.0)


def test_agent_metrics_hand_calculated():
    # 2 tasks × 3 repeats
    # task 1: pass, pass, pass  -> consistent, pass@k, pass-all
    # task 2: pass, fail(error), fail -> inconsistent, pass@k, not pass-all
    rs = [
        rec(1, 0, True, latency=100),
        rec(1, 1, True, latency=200, faults=1, recovered=True),
        rec(1, 2, True, latency=300),
        rec(2, 0, True, latency=400, tool_errors=1, recovered=True),
        rec(2, 1, False, status="error", latency=500, retries=2),
        rec(2, 2, False, latency=600, faults=2),
    ]
    m = agent_metrics(rs)
    assert m["executions"] == 6 and m["passed"] == 4
    assert m["accuracy"] == pytest.approx(4 / 6)
    assert m["consistency"] == pytest.approx(1 / 2)
    assert m["pass_at_k"] == 1.0 and m["pass_all_k"] == 0.5
    assert m["error_rate"] == pytest.approx(1 / 6)
    assert m["executions_with_failures"] == 4  # faults, tool error, retries, faults
    assert m["recovered"] == 2 and m["recovery_rate"] == 0.5
    assert m["latency_ms_p50"] == 350 and m["latency_ms_mean"] == 350
    assert m["tokens_total"] == 90 and m["tokens_mean"] == 15
    assert m["cost_usd_total"] == pytest.approx(0.006)
    assert m["cost_per_correct_usd"] == pytest.approx(0.006 / 4)

    expected = 0.40 * (4 / 6) + 0.25 * 0.5 + 0.20 * 0.5 + 0.15 * (5 / 6)
    assert m["reliability_score"] == pytest.approx(expected)
    assert m["reliability_weights"] == {
        "accuracy": 0.4,
        "consistency": 0.25,
        "recovery_rate": 0.2,
        "stability": 0.15,
    }


def test_undefined_components_are_dropped_and_weights_renormalised():
    # one repeat (no consistency) and no failures (no recovery)
    m = agent_metrics([rec(1, 0, True), rec(2, 0, False)])
    assert m["consistency"] is None and m["recovery_rate"] is None
    assert set(m["reliability_weights"]) == {"accuracy", "stability"}
    assert math.isclose(sum(m["reliability_weights"].values()), 1.0)
    assert m["reliability_score"] == pytest.approx((0.40 * 0.5 + 0.15 * 1.0) / 0.55)


def test_empty_agent_metrics_have_no_nan_or_division_errors():
    m = agent_metrics([])
    assert m["executions"] == 0 and m["accuracy"] is None and m["reliability_score"] is None
    assert m["latency_ms_p95"] is None and m["cost_per_correct_usd"] is None
    for v in m.values():
        assert not (isinstance(v, float) and math.isnan(v))


def test_zero_passes_gives_no_cost_per_correct():
    assert agent_metrics([rec(1, 0, False)])["cost_per_correct_usd"] is None


def test_reliability_score_all_undefined():
    assert reliability_score({"accuracy": None}) == (None, {})


def test_run_metrics_leaderboard_ranking_and_matrix():
    agents = {
        1: {"name": "A", "model": "m/a"},
        2: {"name": "B", "model": "m/b"},
        3: {"name": "C", "model": "m/c"},
    }
    tasks = {10: {"key": "t1", "category": "math"}, 20: {"key": "t2", "category": "facts"}}
    records = [
        rec(10, 0, True, agent=1),
        rec(20, 0, True, agent=1),
        rec(10, 0, True, agent=2),
        rec(20, 0, False, agent=2),
        # agent 3 has no executions yet (run in progress)
    ]
    m = run_metrics(records, agents, tasks)
    board = m["leaderboard"]
    assert [r["agent_name"] for r in board] == ["A", "B", "C"]
    assert [r["rank"] for r in board] == [1, 2, 3]
    assert board[1]["accuracy_by_category"] == {"facts": 0.0, "math": 1.0}
    assert board[2]["executions"] == 0 and board[2]["reliability_score"] is None
    t2 = next(r for r in m["task_matrix"] if r["key"] == "t2")
    assert t2["pass_rate_by_agent"] == {"1": 1.0, "2": 0.0, "3": None}
    assert m["total_executions"] == 4
