"""Plotly chart builders for the dashboard (pure functions: data in, figure out).

Design rules (see docs/dashboard.md):
* Colors follow the **agent**, assigned in the run's fixed agent order, never by rank; eight
  validated slots, then a neutral "other". The palette was validated (CVD + contrast) against
  Streamlit's dark surface #0e1117: all 8 slots for adjacent marks (bars), only the first 3 for
  all-pairs forms (radar), so the radar is capped at 3 agents and the scatter uses one color with
  direct labels.
* Identity is never color-alone: agent names are on the axis or directly labelled.
* One y-axis per chart; latency, tokens and cost are separate charts.
* Status colors (good / warning / critical) are reserved for outcomes (trace steps, freshness
  verdicts) and always come with an icon + label.
"""

from __future__ import annotations

import math
from typing import Any

import plotly.graph_objects as go

# ------------------------------------------------------------------------------------ tokens
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER = "#898781"
INK = "#f0efec"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
# Sequential blue for magnitude on a dark surface: low values recede toward the surface.
SEQ_BLUE = [
    [0.0, "#104281"],
    [0.25, "#1c5cab"],
    [0.5, "#2a78d6"],
    [0.75, "#6da7ec"],
    [1.0, "#cde2fb"],
]
HEAT_INK_DARK = "#0b0b0b"  # on the light (high) end of the sequential ramp
MAX_ALL_PAIRS_SERIES = 3
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def agent_colors(agent_ids: list[int]) -> dict[int, str]:
    """Stable color per agent: slot by position in the run's agent list (not by rank)."""
    return {a: (SERIES[i] if i < len(SERIES) else OTHER) for i, a in enumerate(agent_ids)}


def _layout(fig: go.Figure, title: str, height: int = 360, **kw: Any) -> go.Figure:
    fig.update_layout(
        title={"text": title, "font": {"size": 15, "color": INK}, "x": 0, "xanchor": "left"},
        height=height,
        margin={"l": 8, "r": 16, "t": 48, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": FONT, "color": INK_2, "size": 12},
        hoverlabel={"font": {"family": FONT}},
        barcornerradius=4,
        bargap=0.35,
        bargroupgap=0.08,
        legend={"orientation": "h", "y": -0.18, "x": 0, "font": {"color": INK_2}},
        **kw,
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=AXIS, linecolor=AXIS, tickfont={"color": MUTED})
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=AXIS, linecolor=AXIS, tickfont={"color": MUTED})
    return fig


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.0f}%"


def _safe(v: float | None) -> float:
    return 0.0 if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


# ------------------------------------------------------------------------------------ leaderboard
def reliability_bars(board: list[dict], colors: dict[int, str]) -> go.Figure:
    """Horizontal bars of the reliability score, best at the top, value labelled at the bar end."""
    rows = [r for r in board if r["reliability_score"] is not None][::-1]  # plotly draws bottom-up
    fig = go.Figure(
        go.Bar(
            x=[r["reliability_score"] for r in rows],
            y=[r["agent_name"] for r in rows],
            orientation="h",
            marker={"color": [colors.get(r["agent_id"], OTHER) for r in rows], "line": {"width": 0}},
            text=[_pct(r["reliability_score"]) for r in rows],
            textposition="none",  # labels are annotations past the error bar (below)
            cliponaxis=False,
            error_x=_ci_error(rows),
            customdata=[
                [_pct(r["accuracy"]), _pct(r["consistency"]), _pct(r["recovery_rate"]), _ci_text(r)]
                for r in rows
            ],
            hovertemplate="<b>%{y}</b><br>Reliability %{x:.0%} (95% CI %{customdata[3]})"
            "<br>Accuracy %{customdata[0]}<br>Consistency %{customdata[1]}<br>Recovery %{customdata[2]}"
            "<extra></extra>",
        )
    )
    for r in rows:
        ci = r.get("reliability_ci")
        end = max(r["reliability_score"], ci[1] if ci else 0)
        fig.add_annotation(
            x=end + 0.015,
            y=r["agent_name"],
            text=_pct(r["reliability_score"]),
            showarrow=False,
            xanchor="left",
            font={"color": INK, "size": 13},
        )
    _layout(fig, "Reliability (± 95% CI)", height=max(220, 56 * len(rows) + 80))
    fig.update_xaxes(range=[0, 1.16], tickformat=".0%", showgrid=True)
    fig.update_yaxes(showgrid=False)
    return fig


def _ci_text(row: dict) -> str:
    ci = row.get("reliability_ci")
    return f"{ci[0] * 100:.0f}–{ci[1] * 100:.0f}%" if ci else "n/a"


def _ci_error(rows: list[dict]) -> dict:
    """Asymmetric error bars from each row's ``reliability_ci`` (none where undefined)."""
    plus, minus = [], []
    for r in rows:
        ci, x = r.get("reliability_ci"), r["reliability_score"]
        plus.append(max(0.0, ci[1] - x) if ci else 0.0)
        minus.append(max(0.0, x - ci[0]) if ci else 0.0)
    return {
        "type": "data",
        "symmetric": False,
        "array": plus,
        "arrayminus": minus,
        "color": INK_2,
        "thickness": 1.5,
        "width": 6,
        "visible": any(plus) or any(minus),
    }


# ------------------------------------------------------------------------------------ radar
RADAR_AXES = ["Accuracy", "Consistency", "Recovery", "Stability", "Speed", "Cost efficiency"]


def normalized_profile(board: list[dict]) -> dict[int, dict[str, float]]:
    """Per-agent 0–1 scores for the radar.

    Accuracy, consistency, recovery: as measured (undefined → 0).
    Stability = 1 − error rate.
    Speed = fastest p50 latency in the run ÷ this agent's p50 (fastest agent = 1).
    Cost efficiency = free/local agents 1; paid agents: cheapest *paid* mean cost per task ÷ this
    agent's (so paid agents stay comparable even when a free agent is in the run).
    """
    p50s = [r["latency_ms_p50"] for r in board if r["latency_ms_p50"]]
    paid = [r["cost_usd_mean"] for r in board if r["cost_usd_mean"]]
    fastest = min(p50s) if p50s else None
    cheapest = min(paid) if paid else None
    out = {}
    for r in board:
        p50, cost = r["latency_ms_p50"], r["cost_usd_mean"]
        speed = (fastest / p50) if fastest and p50 else 0.0
        if cost is None:
            cost_eff = 0.0
        elif cost == 0:
            cost_eff = 1.0
        else:
            cost_eff = cheapest / cost
        out[r["agent_id"]] = {
            "Accuracy": _safe(r["accuracy"]),
            "Consistency": _safe(r["consistency"]),
            "Recovery": _safe(r["recovery_rate"]),
            "Stability": 1 - _safe(r["error_rate"]),
            "Speed": speed,
            "Cost efficiency": cost_eff,
        }
    return out


def radar(board: list[dict], agent_ids: list[int], colors: dict[int, str]) -> go.Figure:
    """Profile comparison for up to 3 agents (the palette is only all-pairs safe for 3)."""
    agent_ids = agent_ids[:MAX_ALL_PAIRS_SERIES]
    profile = normalized_profile(board)
    names = {r["agent_id"]: r["agent_name"] for r in board}
    by_id = {r["agent_id"]: r for r in board}
    undefined = {"Accuracy": "accuracy", "Consistency": "consistency", "Recovery": "recovery_rate"}
    fig = go.Figure()
    for aid in agent_ids:
        vals = [profile[aid][a] for a in RADAR_AXES]
        hover = [
            "n/a (no failures to recover from)"
            if a == "Recovery" and by_id[aid]["recovery_rate"] is None
            else ("n/a" if a in undefined and by_id[aid][undefined[a]] is None else f"{profile[aid][a]:.0%}")
            for a in RADAR_AXES
        ]
        fig.add_trace(
            go.Scatterpolar(
                r=vals + vals[:1],
                theta=RADAR_AXES + RADAR_AXES[:1],
                name=names[aid],
                mode="lines+markers",
                line={"color": colors.get(aid, OTHER), "width": 2},
                marker={"size": 8, "line": {"color": "#0e1117", "width": 2}},
                fill="toself",
                fillcolor=_alpha(colors.get(aid, OTHER), 0.12),
                customdata=hover + hover[:1],
                hovertemplate=f"<b>{names[aid]}</b><br>%{{theta}}: %{{customdata}}<extra></extra>",
            )
        )
    _layout(fig, "Agent profile (0–100%, higher is better)", height=420)
    fig.update_layout(
        polar={
            "bgcolor": "rgba(0,0,0,0)",
            "radialaxis": {
                "range": [0, 1],
                "angle": 90,  # sits in the gap between "Consistency" and "Recovery": no label collisions
                "tickangle": 90,
                "tickvals": [0.5, 1.0],
                "ticktext": ["50%", "100%"],
                "gridcolor": GRID,
                "linecolor": AXIS,
                "tickfont": {"color": MUTED, "size": 10},
            },
            "angularaxis": {"gridcolor": GRID, "linecolor": AXIS, "tickfont": {"color": INK_2}},
        },
        showlegend=True,
    )
    return fig


def _alpha(hex_color: str, a: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{a})"


# ------------------------------------------------------------------------------------ speed & cost
def latency_chart(board: list[dict]) -> go.Figure:
    """p50 vs p95 per agent: two series in one blue hue (light = p50, dark = p95)."""
    rows = [r for r in board if r["latency_ms_p50"] is not None]
    names = [r["agent_name"] for r in rows]
    fig = go.Figure()
    for label, key, color in (
        ("p50 (typical)", "latency_ms_p50", "#6da7ec"),
        ("p95 (slow tail)", "latency_ms_p95", "#1c5cab"),
    ):
        fig.add_trace(
            go.Bar(
                y=names,
                x=[r[key] / 1000 for r in rows],
                name=label,
                orientation="h",
                marker={"color": color, "line": {"width": 0}},
                hovertemplate="<b>%{y}</b><br>" + label + ": %{x:.2f} s<extra></extra>",
            )
        )
    _layout(fig, "Latency per task (seconds)", height=max(240, 64 * len(rows) + 100), barmode="group")
    fig.update_xaxes(ticksuffix=" s", rangemode="tozero")
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig


def metric_bars(board: list[dict], colors: dict[int, str], key: str, title: str, fmt: str) -> go.Figure:
    """One measure per agent (tokens, cost…), agent-colored, name on the axis, value labelled."""
    rows = [r for r in board if r[key] is not None]
    values = [r[key] for r in rows]
    labels = [_fmt(v, fmt) for v in values]
    fig = go.Figure(
        go.Bar(
            y=[r["agent_name"] for r in rows],
            x=values,
            orientation="h",
            marker={"color": [colors.get(r["agent_id"], OTHER) for r in rows], "line": {"width": 0}},
            text=labels,
            textposition="outside",
            textfont={"color": INK},
            cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>" + title + ": %{text}<extra></extra>",
        )
    )
    _layout(fig, title, height=max(220, 56 * len(rows) + 80))
    top = max(values) if values else 0
    fig.update_xaxes(range=[0, top * 1.25 if top else 1], showticklabels=False, showgrid=False)
    fig.update_yaxes(autorange="reversed", showgrid=False)
    return fig


def _fmt(v: float, fmt: str) -> str:
    if fmt == "usd":
        return "$0" if v == 0 else (f"${v:.4f}" if v >= 0.0001 else f"${v:.6f}")
    if fmt == "int":
        return f"{v:,.0f}"
    if fmt == "pct":
        return _pct(v)
    return f"{v:.2f}"


def _label_positions(xs: list[float]) -> list[str]:
    """Alternate labels above/below in x order; edge points point inward so nothing clips."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    pos = [""] * len(xs)
    for rank, i in enumerate(order):
        vertical = "top" if rank % 2 == 0 else "bottom"
        horizontal = (
            "right"
            if rank == 0 and len(xs) > 1
            else ("left" if rank == len(xs) - 1 and len(xs) > 1 else "center")
        )
        pos[i] = f"{vertical} {horizontal}"
    return pos


def accuracy_vs_cost(board: list[dict]) -> go.Figure:
    """Trade-off scatter. One color + direct name labels (identity never relies on color).

    Cost is shown per 1,000 tasks so the axis reads in normal dollar amounts.
    """
    rows = [r for r in board if r["accuracy"] is not None and r["cost_usd_mean"] is not None]
    xs = [r["cost_usd_mean"] * 1000 for r in rows]
    top = max(xs) if xs else 0
    pad = top * 0.12 if top else 0.05
    fig = go.Figure(
        go.Scatter(
            x=xs,
            y=[r["accuracy"] for r in rows],
            mode="markers+text",
            text=[r["agent_name"] for r in rows],
            textposition=_label_positions(xs),
            textfont={"color": INK_2, "size": 11},
            marker={"size": 12, "color": SERIES[0], "line": {"color": "#0e1117", "width": 2}},
            customdata=[[_fmt(r["cost_usd_mean"], "usd"), _pct(r["accuracy"]), r["model"]] for r in rows],
            hovertemplate="<b>%{text}</b><br>%{customdata[2]}<br>Accuracy %{customdata[1]}"
            "<br>Cost per task %{customdata[0]}<br>Per 1,000 tasks $%{x:,.2f}<extra></extra>",
            cliponaxis=False,
        )
    )
    _layout(fig, "Accuracy vs cost (top-left is best)", height=400)
    fig.update_yaxes(
        range=[-0.08, 1.15], tickformat=".0%", title={"text": "Accuracy", "font": {"color": MUTED}}
    )
    fig.update_xaxes(
        range=[-pad, top + pad],
        tickformat="$,.2f",
        title={"text": "Cost per 1,000 tasks (USD)", "font": {"color": MUTED}},
    )
    return fig


# ------------------------------------------------------------------------------------ per task
def task_heatmap(matrix: list[dict], board: list[dict]) -> go.Figure:
    """Task × agent pass rate, sequential blue, every cell labelled (– = not attempted)."""
    agents = [(str(r["agent_id"]), r["agent_name"]) for r in board]
    tasks = [m["key"] + (" ⏰" if m.get("time_sensitive") else "") for m in matrix]
    z, text = [], []
    for m in matrix:
        row_z, row_t = [], []
        for aid, _ in agents:
            v = m["pass_rate_by_agent"].get(aid)
            row_z.append(None if v is None else v)
            row_t.append(_pct(v))
        z.append(row_z)
        text.append(row_t)
    xs = [name for _, name in agents]
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=xs,
            y=tasks,
            text=text,
            colorscale=SEQ_BLUE,
            zmin=0,
            zmax=1,
            xgap=2,
            ygap=2,
            colorbar={
                "title": {"text": "pass rate", "font": {"color": MUTED}},
                "tickformat": ".0%",
                "thickness": 10,
            },
            hovertemplate="<b>%{y}</b><br>%{x}<br>pass rate %{text}<extra></extra>",
        )
    )
    # Cell labels as annotations so each picks the ink that contrasts with its own fill.
    for yi, row_z in enumerate(z):
        for xi, v in enumerate(row_z):
            fig.add_annotation(
                x=xs[xi],
                y=tasks[yi],
                text=text[yi][xi],
                showarrow=False,
                font={"size": 11, "color": MUTED if v is None else (HEAT_INK_DARK if v >= 0.55 else INK)},
            )
    _layout(fig, "Pass rate per task", height=max(260, 30 * len(tasks) + 120))
    fig.update_xaxes(side="top", showgrid=False, tickfont={"color": INK_2})
    fig.update_yaxes(autorange="reversed", showgrid=False, tickfont={"color": INK_2})
    return fig


def category_bars(board: list[dict], colors: dict[int, str]) -> go.Figure:
    """Accuracy by task category, grouped by agent (adjacent marks: all 8 slots are valid)."""
    categories = sorted({c for r in board for c in (r.get("accuracy_by_category") or {})})
    fig = go.Figure()
    for r in board:
        acc = r.get("accuracy_by_category") or {}
        fig.add_trace(
            go.Bar(
                x=categories,
                y=[acc.get(c) for c in categories],
                name=r["agent_name"],
                marker={"color": colors.get(r["agent_id"], OTHER), "line": {"width": 0}},
                hovertemplate=f"<b>{r['agent_name']}</b><br>%{{x}}: %{{y:.0%}}<extra></extra>",
            )
        )
    _layout(fig, "Accuracy by category", height=360, barmode="group")
    fig.update_yaxes(range=[0, 1.05], tickformat=".0%")
    return fig


# ------------------------------------------------------------------------------------ freshness
SURFACE = "#0e1117"
FRESHNESS_STYLE: dict[str, tuple[str, str, str]] = {
    # answer verdict -> (fill, legend label, segment-text ink). Outcomes, so status colors + icons.
    "fresh": (STATUS["good"], "✅ fresh", HEAT_INK_DARK),
    "stale": (STATUS["warning"], "⚠️ stale", HEAT_INK_DARK),
    "wrong": (STATUS["critical"], "❌ wrong", INK),
    "unverified": (OTHER, "❔ unverified", HEAT_INK_DARK),
    "no_claims": (AXIS, "∅ no claim / timeless", INK),
}


def freshness_bars(board: list[dict]) -> go.Figure:
    """100%-stacked bar per agent: share of its checked answers by freshness verdict.

    ``timeless`` folds into ``no_claims`` (neither had anything time-sensitive to verify). Segments are
    separated by a 2px surface gap; counts are labelled inside, and hover gives count and share."""
    rows = [r for r in board if r.get("freshness_checked")][::-1]  # plotly draws bottom-up
    names = [r["agent_name"] for r in rows]
    fig = go.Figure()
    for verdict, (fill, label, ink) in FRESHNESS_STYLE.items():
        counts = []
        for r in rows:
            v = r.get("freshness_verdicts") or {}
            counts.append(v.get(verdict, 0) + (v.get("timeless", 0) if verdict == "no_claims" else 0))
        shares = [c / r["freshness_checked"] for c, r in zip(counts, rows, strict=True)]
        fig.add_trace(
            go.Bar(
                y=names,
                x=shares,
                orientation="h",
                name=label,
                marker={"color": fill, "line": {"color": SURFACE, "width": 2}},
                text=[str(c) if c else "" for c in counts],
                textposition="inside",
                insidetextanchor="middle",
                insidetextfont={"color": ink},
                customdata=counts,
                hovertemplate=f"<b>%{{y}}</b><br>{label}: %{{customdata}} (%{{x:.0%}})<extra></extra>",
            )
        )
    _layout(fig, "Freshness of answers to time-sensitive tasks", height=max(220, 70 + 52 * len(rows)))
    fig.update_layout(barmode="stack", bargap=0.3, legend_traceorder="normal")
    fig.update_xaxes(range=[0, 1], tickformat=".0%", showgrid=False)
    fig.update_yaxes(showgrid=False, tickfont={"color": INK_2})
    return fig


# ------------------------------------------------------------------------------------ traces
STEP_STYLE: dict[str, tuple[str, str]] = {
    # type -> (color, label). Status colors only for outcomes; always paired with an icon label.
    "llm_call": (SERIES[0], "🧠 model call"),
    "tool_call": (SERIES[2], "🛠️ tool call"),
    "tool_result": (SERIES[2], "📦 tool result"),
    "fault": (STATUS["critical"], "💥 injected fault"),
    "tool_error": (STATUS["serious"], "⚠️ tool error"),
    "error": (STATUS["critical"], "❌ error"),
    "retry": (STATUS["warning"], "🔁 retry backoff"),
    "final": (STATUS["good"], "✅ final answer"),
}


def _step_kind(step: dict) -> str:
    p = step.get("payload") or {}
    if step["type"] == "tool_result":
        if p.get("fault"):
            return "fault"
        if not p.get("ok", True):
            return "tool_error"
    return step["type"] if step["type"] in STEP_STYLE else "llm_call"


def trace_timeline(trace: list[dict]) -> go.Figure:
    """Gantt-style timeline: each step as a bar starting where the previous one ended."""
    total = sum(s.get("latency_ms") or 0 for s in trace) or 1.0
    min_w = total * 0.004  # zero-latency steps stay visible
    fig = go.Figure()
    seen: set[str] = set()
    t = 0.0
    for s in trace:
        kind = _step_kind(s)
        color, label = STEP_STYLE[kind]
        dur = float(s.get("latency_ms") or 0)
        name = (s.get("payload") or {}).get("name")
        y = f"{s['idx']:>2}. {label.split(' ', 1)[1]}" + (f" · {name}" if name else "")
        fig.add_trace(
            go.Bar(
                x=[max(dur, min_w) / 1000],
                y=[y],
                base=[t / 1000],
                orientation="h",
                name=label,
                legendgroup=kind,
                showlegend=kind not in seen,
                marker={"color": color, "line": {"width": 0}},
                hovertemplate=f"<b>{label}</b>{' · ' + name if name else ''}<br>start %{{base:.2f}} s"
                f"<br>duration {dur / 1000:.2f} s<extra></extra>",
            )
        )
        seen.add(kind)
        t += dur
    _layout(fig, f"Execution timeline · {total / 1000:.2f} s total", height=max(220, 26 * len(trace) + 120))
    fig.update_layout(barmode="overlay", bargap=0.25)
    fig.update_xaxes(ticksuffix=" s", rangemode="tozero")
    fig.update_yaxes(autorange="reversed", showgrid=False, tickfont={"color": INK_2, "size": 11})
    return fig


# ------------------------------------------------------------------------------------ freshness bench
MAX_CURVE_AGENTS = 4


def _age_position(age: float, mids: list[float]) -> float:
    """Map an event age (days) to the evenly spaced bucket axis: oldest bucket at 0, newest at len-1.

    ``mids`` are the bucket midpoints from newest to oldest; ages between them interpolate linearly."""
    n = len(mids)
    if age <= mids[0]:
        return n - 1.0
    if age >= mids[-1]:
        return 0.0
    for i in range(n - 1):
        lo, hi = mids[i], mids[i + 1]
        if lo <= age <= hi:
            return (n - 1) - (i + (age - lo) / (hi - lo))
    return 0.0


def knowledge_curve(curve: dict, colors: dict[int, str]) -> go.Figure:
    """Accuracy per event-age bucket (95% Wilson bars) with the fitted logistic and the estimated cutoff.

    Buckets are evenly spaced, oldest on the left and *today on the right*, so a model's knowledge visibly
    fades toward the present. The fitted curve and the cutoff line are mapped onto the same axis.
    """
    fig = go.Figure()
    buckets = curve["buckets"]
    mids = [(b["min_days"] + b["max_days"]) / 2 for b in buckets]
    n = len(buckets)
    estimated = 0
    for row in curve["agents"][:MAX_CURVE_AGENTS]:
        color = colors.get(row["agent_id"], OTHER)
        pts = [(i, b) for i, b in enumerate(row["buckets"]) if b["accuracy"] is not None]
        cutoff = row.get("cutoff") or {}
        fitted = cutoff.get("status") == "estimated" and row.get("fit")
        fig.add_trace(
            go.Scatter(
                x=[n - 1 - i for i, _ in pts],
                y=[b["accuracy"] for _, b in pts],
                mode="markers" if fitted else "lines+markers",
                name=row["agent_name"],
                legendgroup=str(row["agent_id"]),
                marker={"size": 9, "color": color, "line": {"width": 2, "color": "#0e1117"}},
                line={"width": 2, "color": color},
                error_y={
                    "type": "data",
                    "symmetric": False,
                    "array": [b["ci"][1] - b["accuracy"] if b["ci"] else 0 for _, b in pts],
                    "arrayminus": [b["accuracy"] - b["ci"][0] if b["ci"] else 0 for _, b in pts],
                    "color": color,
                    "thickness": 1.5,
                    "width": 4,
                },
                customdata=[[b["label"], b["attempts"], b["tasks"]] for _, b in pts],
                hovertemplate="<b>%{fullData.name}</b><br>Events %{customdata[0]} old<br>"
                "Accuracy %{y:.0%} (%{customdata[1]} answers, %{customdata[2]} questions)<extra></extra>",
            )
        )
        if fitted:
            fig.add_trace(
                go.Scatter(
                    x=[_age_position(p["age_days"], mids) for p in row["fit"]],
                    y=[p["accuracy"] for p in row["fit"]],
                    mode="lines",
                    line={"width": 2, "color": color, "dash": "dot"},
                    legendgroup=str(row["agent_id"]),
                    showlegend=False,
                    hoverinfo="skip",
                )
            )
            x = _age_position(cutoff["days_before_snapshot"], mids)
            fig.add_vline(x=x, line={"width": 1, "dash": "dash", "color": color})
            fig.add_annotation(
                x=x,
                y=1.0 + 0.08 * (estimated + 1),
                yref="paper",
                text=f"cutoff ≈ {cutoff['date'][:7]}",
                showarrow=False,
                font={"size": 11, "color": color},
                xanchor="center",
            )
            estimated += 1
    _layout(fig, "Accuracy by event age (today on the right)", height=420)
    fig.update_layout(margin={"l": 8, "r": 16, "t": 48 + 22 * max(estimated, 1), "b": 8})
    fig.update_xaxes(
        range=[-0.4, n - 0.6],
        tickvals=list(range(n)),
        ticktext=[b["label"] for b in reversed(buckets)],
        title={"text": "How long ago the event happened", "font": {"color": MUTED, "size": 12}},
        showgrid=False,
    )
    fig.update_yaxes(range=[-0.03, 1.05], tickformat=".0%", title=None)
    return fig


def freshbench_history(history: dict, colors: dict[int, str]) -> go.Figure:
    """Accuracy per agent across Freshness Bench snapshots (one point per run)."""
    fig = go.Figure()
    for row in history["agents"][: len(SERIES)]:
        pts = [p for p in row["points"] if p["accuracy"] is not None]
        if not pts:
            continue
        fig.add_trace(
            go.Scatter(
                x=[p["snapshot_date"] for p in pts],
                y=[p["accuracy"] for p in pts],
                mode="lines+markers",
                name=row["agent_name"],
                marker={"size": 8, "color": colors.get(row["agent_id"], OTHER)},
                line={"width": 2, "color": colors.get(row["agent_id"], OTHER)},
                customdata=[[p["suite_name"], p["run_id"]] for p in pts],
                hovertemplate="<b>%{fullData.name}</b><br>%{customdata[0]} (run #%{customdata[1]})<br>"
                "Accuracy %{y:.0%}<extra></extra>",
            )
        )
    _layout(fig, "Freshness Bench accuracy over time", height=320)
    fig.update_yaxes(range=[0, 1.05], tickformat=".0%")
    fig.update_xaxes(type="category", showgrid=False)
    return fig
