"""Chart builders: data correctness, color rules and edge cases (no backend needed)."""

import math

import pytest

import charts


def row(aid, name, **kw):
    base = {
        "agent_id": aid,
        "agent_name": name,
        "model": f"groq/{name}",
        "rank": aid,
        "executions": 6,
        "accuracy": 0.5,
        "consistency": 0.5,
        "recovery_rate": None,
        "error_rate": 0.0,
        "reliability_score": 0.6,
        "latency_ms_p50": 1000.0,
        "latency_ms_p95": 2000.0,
        "tokens_mean": 500.0,
        "cost_usd_mean": 0.001,
        "cost_usd_total": 0.006,
        "accuracy_by_category": {"math": 0.5},
    }
    return {**base, **kw}


BOARD = [
    row(1, "A", reliability_score=0.9, accuracy=1.0, latency_ms_p50=500.0, cost_usd_mean=0.002),
    row(2, "B", reliability_score=0.7, latency_ms_p50=1000.0, cost_usd_mean=0.0),
    row(
        3,
        "C",
        reliability_score=0.5,
        accuracy=None,
        consistency=None,
        latency_ms_p50=2000.0,
        cost_usd_mean=0.004,
    ),
    row(4, "D", reliability_score=None),
]


def test_agent_colors_follow_agent_order_not_rank_and_never_cycle():
    ids = list(range(10, 20))
    colors = charts.agent_colors(ids)
    assert [colors[i] for i in ids[:8]] == charts.SERIES
    assert colors[18] == colors[19] == charts.OTHER  # 9th+ fold to neutral "other", no generated hues
    assert charts.agent_colors([3, 1])[3] == charts.SERIES[0]  # position in run, not id or rank


def test_normalized_profile_math():
    p = charts.normalized_profile(BOARD)
    assert p[1]["Speed"] == 1.0 and p[2]["Speed"] == 0.5 and p[3]["Speed"] == 0.25
    assert p[2]["Cost efficiency"] == 1.0  # free
    assert p[4]["Cost efficiency"] == 1.0  # D: cheapest PAID agent ($0.001)
    assert p[1]["Cost efficiency"] == 0.5  # A: $0.002 = twice the cheapest paid
    assert p[3]["Cost efficiency"] == 0.25  # C: $0.004
    assert p[3]["Accuracy"] == 0.0 and p[3]["Consistency"] == 0.0  # undefined → 0
    assert all(0.0 <= v <= 1.0 for prof in p.values() for v in prof.values())


def test_cost_efficiency_relative_to_cheapest_paid_agent():
    board = [row(1, "A", cost_usd_mean=0.001), row(2, "B", cost_usd_mean=0.004)]
    p = charts.normalized_profile(board)
    assert p[1]["Cost efficiency"] == 1.0 and p[2]["Cost efficiency"] == 0.25


def test_radar_is_capped_at_three_agents():
    fig = charts.radar(BOARD, [1, 2, 3, 4], charts.agent_colors([1, 2, 3, 4]))
    assert len(fig.data) == charts.MAX_ALL_PAIRS_SERIES == 3
    for trace in fig.data:
        assert list(trace.theta)[0] == list(trace.theta)[-1]  # closed polygon


def test_reliability_bars_skip_undefined_and_put_best_on_top():
    fig = charts.reliability_bars(BOARD, charts.agent_colors([1, 2, 3, 4]))
    bar = fig.data[0]
    assert list(bar.y) == ["C", "B", "A"]  # plotly draws bottom-up: best (A) ends on top
    assert list(bar.text) == ["50%", "70%", "90%"]
    assert list(bar.marker.color) == [charts.SERIES[2], charts.SERIES[1], charts.SERIES[0]]


def test_latency_chart_two_series_same_agents():
    fig = charts.latency_chart(BOARD)
    assert [t.name for t in fig.data] == ["p50 (typical)", "p95 (slow tail)"]
    assert list(fig.data[0].x) == [0.5, 1.0, 2.0, 1.0]


@pytest.mark.parametrize(
    ("fmt", "value", "label"), [("usd", 0, "$0"), ("usd", 0.0042, "$0.0042"), ("int", 1234.4, "1,234")]
)
def test_metric_bar_labels(fmt, value, label):
    fig = charts.metric_bars([row(1, "A", tokens_mean=value)], {1: charts.SERIES[0]}, "tokens_mean", "t", fmt)
    assert list(fig.data[0].text) == [label]


def test_accuracy_vs_cost_single_color_with_direct_labels():
    fig = charts.accuracy_vs_cost(BOARD)
    sc = fig.data[0]
    assert sc.marker.color == charts.SERIES[0]
    assert "text" in sc.mode and list(sc.text) == ["A", "B", "D"]  # C has no accuracy


def test_task_heatmap_marks_unattempted_cells():
    matrix = [
        {"key": "t1", "time_sensitive": True, "pass_rate_by_agent": {"1": 1.0, "2": None}},
        {"key": "t2", "time_sensitive": False, "pass_rate_by_agent": {"1": 0.0, "2": 0.5}},
    ]
    fig = charts.task_heatmap(matrix, BOARD[:2])
    hm = fig.data[0]
    assert list(hm.y) == ["t1 ⏰", "t2"]
    assert [list(r) for r in hm.text] == [["100%", "–"], ["0%", "50%"]]
    assert hm.zmin == 0 and hm.zmax == 1


def test_trace_timeline_is_cumulative_and_flags_faults():
    trace = [
        {"idx": 0, "type": "llm_call", "payload": {}, "latency_ms": 400},
        {"idx": 1, "type": "tool_call", "payload": {"name": "calculator"}, "latency_ms": 0},
        {
            "idx": 2,
            "type": "tool_result",
            "payload": {"name": "calculator", "ok": False, "fault": "timeout"},
            "latency_ms": 5000,
        },
        {"idx": 3, "type": "retry", "payload": {}, "latency_ms": 500},
        {"idx": 4, "type": "final", "payload": {}, "latency_ms": 0},
    ]
    fig = charts.trace_timeline(trace)
    bases = [t.base[0] for t in fig.data]
    assert bases == pytest.approx([0.0, 0.4, 0.4, 5.4, 5.9])
    colors = [t.marker.color for t in fig.data]
    assert colors[2] == charts.STATUS["critical"] and colors[3] == charts.STATUS["warning"]
    assert colors[4] == charts.STATUS["good"]
    assert all(t.x[0] > 0 for t in fig.data)  # zero-latency steps stay visible
    assert "5.90 s total" in fig.layout.title.text


def test_empty_inputs_do_not_crash():
    assert charts.reliability_bars([], {}).data[0].y == ()
    assert charts.latency_chart([]).data[0].x == ()
    assert charts.accuracy_vs_cost([]).data[0].x == ()
    assert charts.trace_timeline([]).data == ()
    assert charts.normalized_profile([]) == {}
    assert not math.isnan(charts.normalized_profile([row(1, "A", latency_ms_p50=None)])[1]["Speed"])


def test_heatmap_cell_ink_contrasts_with_fill():
    matrix = [
        {"key": "t", "pass_rate_by_agent": {"1": 1.0, "2": 0.2}},
        {"key": "u", "pass_rate_by_agent": {"1": None, "2": 0.6}},
    ]
    fig = charts.task_heatmap(matrix, BOARD[:2])
    inks = {(a.x, a.y): (a.text, a.font.color) for a in fig.layout.annotations}
    assert inks[("A", "t")] == ("100%", charts.HEAT_INK_DARK)  # light cell -> dark ink
    assert inks[("B", "t")] == ("20%", charts.INK)  # dark cell -> light ink
    assert inks[("A", "u")] == ("–", charts.MUTED)


def test_scatter_labels_alternate_and_edges_point_inward():
    assert charts._label_positions([5.0, 1.0, 3.0]) == ["top left", "top right", "bottom center"]
    assert charts._label_positions([2.0]) == ["top center"]
    fig = charts.accuracy_vs_cost([row(1, "A", cost_usd_mean=0.0002)])
    assert fig.data[0].x[0] == pytest.approx(0.2)  # per 1,000 tasks
    assert fig.layout.xaxis.tickformat == "$,.2f"


def test_radar_ticks_avoid_axis_labels():
    fig = charts.radar(BOARD, [1], charts.agent_colors([1]))
    ra = fig.layout.polar.radialaxis
    assert ra.angle == 90 and list(ra.ticktext) == ["50%", "100%"]


def test_radar_hover_says_na_for_undefined_metrics():
    fig = charts.radar(BOARD, [3], charts.agent_colors([3]))  # C: accuracy/consistency/recovery undefined
    hover = dict(zip(fig.data[0].theta, fig.data[0].customdata, strict=False))
    assert hover["Recovery"].startswith("n/a") and hover["Accuracy"] == "n/a"
    assert hover["Stability"] == "100%"


def test_freshness_bars_are_shares_with_counts_and_skip_unchecked_agents():
    board = [
        row(1, "A", freshness_checked=4, freshness_verdicts={"fresh": 2, "stale": 1, "timeless": 1}),
        row(2, "B", freshness_checked=0, freshness_verdicts={}),
        row(3, "C", freshness_checked=2, freshness_verdicts={"wrong": 1, "no_claims": 1}),
    ]
    fig = charts.freshness_bars(board)
    by_name = {t.name: t for t in fig.data}
    assert list(by_name) == [label for _, label, _ in charts.FRESHNESS_STYLE.values()]
    assert list(by_name["✅ fresh"].y) == ["C", "A"]  # B has no checked answers; board order, top-down
    assert list(by_name["✅ fresh"].x) == [0.0, 0.5]
    assert list(by_name["∅ no claim / timeless"].customdata) == [1, 1]  # timeless folds into no-claims
    assert list(by_name["⚠️ stale"].text) == ["", "1"]  # zero segments are unlabelled
    for i in range(2):  # every agent's segments sum to 100%
        assert sum(t.x[i] for t in fig.data) == pytest.approx(1.0)
    assert fig.layout.barmode == "stack"
    assert by_name["⚠️ stale"].marker.color == charts.STATUS["warning"]
    assert charts.freshness_bars([]).data[0].y == ()
    assert (
        fig.layout.legend.traceorder == "normal"
    )  # fresh → stale → wrong, not plotly's reversed stack order
