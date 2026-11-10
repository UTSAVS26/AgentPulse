"""The knowledge-cutoff curve: how an agent's accuracy depends on how old the event behind a question is.

A model that answers from its training data knows old events and not new ones, so its accuracy rises with
event age. We fit

    p(age) = plateau / (1 + exp(-(age - m) / s))

where ``plateau`` is the agent's accuracy on events clearly older than the cutoff (first guess: the oldest
third of the questions; then the questions older than ``m + 3s``, and the fit is redone) and ``m`` is the
age at which accuracy is half the plateau: the **effective knowledge cutoff** is ``m`` days before the
snapshot. ``m`` and ``s`` come from a grid search on the binomial log-likelihood (task level); the interval
is a seeded task-level bootstrap with the plateau held at its point estimate. An agent with web search knows
recent events too, so its curve is flat: "no cutoff detected".

Pure functions over plain data; the caller supplies records and event dates.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from app.bench.stats import wilson
from app.freshbench.buckets import BUCKETS, age_days, bucket_for

M_GRID = list(range(0, 741, 10))
S_GRID = (10.0, 20.0, 40.0, 80.0)
RESAMPLES = 150
MIN_TASKS = 6
LOW_SIGNAL_PLATEAU = 0.25
NO_CUTOFF_RATIO = 0.75  # newest-bucket accuracy at least this share of the plateau → no cutoff detected


def _accuracy(tasks: list[tuple[int, int, int]]) -> float:
    n = sum(t[1] for t in tasks)
    return sum(t[2] for t in tasks) / n if n else 0.0


def _plateau(tasks: list[tuple[int, int, int]]) -> float:
    """First guess: accuracy on the oldest third of the questions (by event age)."""
    ordered = sorted(tasks, key=lambda t: t[0])
    return _accuracy(ordered[-max(1, len(ordered) // 3) :])


MIN_PLATEAU_TASKS = 4


def _table(tasks: list[tuple[int, int, int]], plateau: float) -> list[tuple[float, float, list[float]]]:
    """Per grid point ``(m, s)``: each task's log-likelihood, so fits are weighted sums (fast bootstrap)."""
    table = []
    for m in M_GRID:
        for s in S_GRID:
            row = []
            for age, n, k in tasks:
                z = max(-50.0, min(50.0, (age - m) / s))
                p = min(1 - 1e-6, max(1e-6, plateau / (1 + math.exp(-z))))
                row.append(k * math.log(p) + (n - k) * math.log(1 - p))
            table.append((float(m), s, row))
    return table


def _argmax(table: list[tuple[float, float, list[float]]], weights: dict[int, int] | None = None) -> tuple:
    best = (-math.inf, 0.0, S_GRID[0])
    for m, s, row in table:
        ll = sum(row) if weights is None else sum(row[i] * w for i, w in weights.items())
        if ll > best[0]:
            best = (ll, m, s)
    return best[1], best[2]


def _quantile(values: list[float], q: float) -> float:
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def agent_curve(
    passes: dict[int, list[bool]], ages: dict[int, int], reference: date, seed: int = 0
) -> dict[str, Any]:
    """One agent: ``passes`` = task id → pass/fail per attempt, ``ages`` = task id → event age in days."""
    tasks = [(ages[t], len(v), sum(v)) for t, v in passes.items() if t in ages and v]
    buckets = []
    for b in BUCKETS:
        inside = [t for t in tasks if b.contains(t[0])]
        n, k = sum(t[1] for t in inside), sum(t[2] for t in inside)
        ci = wilson(k, n)
        buckets.append(
            {
                "key": b.key,
                "label": b.label,
                "mid_days": b.mid_days,
                "tasks": len(inside),
                "attempts": n,
                "accuracy": k / n if n else None,
                "ci": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
            }
        )
    out: dict[str, Any] = {"buckets": buckets, "cutoff": None}
    if len(tasks) < MIN_TASKS:
        out["cutoff"] = {
            "status": "too few questions",
            "detail": f"needs at least {MIN_TASKS} dated questions",
        }
        return out
    plateau = min(0.999, max(_plateau(tasks), 1e-3))
    m, s = _argmax(_table(tasks, plateau))
    # Refine: the plateau is the accuracy on events clearly older than the first cutoff estimate.
    settled = [t for t in tasks if t[0] >= m + 3 * s]
    if len(settled) >= MIN_PLATEAU_TASKS:
        plateau = min(0.999, max(_accuracy(settled), 1e-3))
    table = _table(tasks, plateau)
    m, s = _argmax(table)
    newest = next((b for b in buckets if b["accuracy"] is not None), None)
    if plateau < LOW_SIGNAL_PLATEAU:
        out["cutoff"] = {
            "status": "too little signal",
            "plateau": round(plateau, 3),
            "detail": "the agent rarely answers even old events correctly",
        }
        return out
    if newest and newest["accuracy"] >= NO_CUTOFF_RATIO * plateau:
        out["cutoff"] = {
            "status": "no cutoff detected",
            "plateau": round(plateau, 3),
            "detail": f"it knows the newest events ({newest['label']}) about as well as old ones, "
            "e.g. because it searches the web",
        }
        return out
    rng = random.Random(seed)
    draws = []
    for _ in range(RESAMPLES):  # resample tasks; the plateau is held at its point estimate
        weights: dict[int, int] = defaultdict(int)
        for _ in tasks:
            weights[rng.randrange(len(tasks))] += 1
        draws.append(_argmax(table, weights)[0])
    lo_days, hi_days = _quantile(draws, 0.975), _quantile(draws, 0.025)  # older date first
    out["cutoff"] = {
        "status": "estimated",
        "days_before_snapshot": int(m),
        "date": (reference - timedelta(days=int(m))).isoformat(),
        "ci": [
            (reference - timedelta(days=int(lo_days))).isoformat(),
            (reference - timedelta(days=int(hi_days))).isoformat(),
        ],
        "plateau": round(plateau, 3),
        "steepness_days": s,
    }
    out["fit"] = [
        {"age_days": a, "accuracy": round(plateau / (1 + math.exp(-max(-50, min(50, (a - m) / s)))), 4)}
        for a in range(0, 731, 15)
    ]
    return out


def knowledge_curve(
    records: Iterable[Any],
    event_dates: dict[int, str],
    reference: date,
    agents: dict[int, dict],
    seed: int = 0,
) -> dict[str, Any] | None:
    """The run-level curve, or None when no task has an event date.

    ``records`` need ``agent_id``, ``task_id``, ``passed`` and ``status``; provider errors are left out (they
    say nothing about what the model knows).
    """
    ages = {
        tid: age_days(d, reference)
        for tid, d in event_dates.items()
        if d and bucket_for(age_days(d, reference)) is not None
    }
    if not ages:
        return None
    by_agent: dict[int, dict[int, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r.task_id in ages and r.status != "error":
            by_agent[r.agent_id][r.task_id].append(bool(r.passed))
    rows = []
    for agent_id, info in agents.items():
        if agent_id not in by_agent:
            continue
        curve = agent_curve(by_agent[agent_id], ages, reference, seed=seed + agent_id)
        rows.append(
            {"agent_id": agent_id, "agent_name": info.get("name"), "model": info.get("model"), **curve}
        )
    return {
        "reference_date": reference.isoformat(),
        "questions": len(ages),
        "buckets": [
            {"key": b.key, "label": b.label, "min_days": b.min_days, "max_days": b.max_days} for b in BUCKETS
        ],
        "agents": rows,
        "method": "logistic fit of accuracy vs event age (grid search), task-level bootstrap interval",
    }
