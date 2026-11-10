"""Statistics for honest leaderboards: is a difference between agents real, or noise?

Pure functions, seeded, no third-party dependencies. Formulas: docs/statistics.md.

* **Wilson 95% interval** for accuracy (passes out of executions). Repeats of the same task are not
  independent, so treat it as a lower bound on the true uncertainty; the bootstrap below accounts for that.
* **Paired task-level bootstrap.** Tasks are the unit of sampling: each resample draws tasks with replacement
  and recomputes every agent's reliability score on those tasks, with the same definitions as the leaderboard
  (``metrics.reliability_components``). The same draws are used for every agent, so differences are *paired*:
  - reliability 95% interval = 2.5th–97.5th percentile of the resampled scores;
  - p-value for "A ≠ B" = 2 × the smaller tail of the resampled differences (``A − B ≤ 0`` vs ``≥ 0``).
* **Rank tiers.** Walking down the leaderboard, an agent joins the current tier when it is not
  significantly worse (p ≥ 0.05) than the tier's leader; otherwise it starts a new tier.
* **Fleiss' κ** for agreement between jurors (``scorer: jury``) on pass/fail.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

ALPHA = 0.05
Z95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion (None when n == 0)."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    pos = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def resample_count(total_records: int) -> int:
    """1,000 resamples for normal runs; fewer for very large ones (keeps the API fast)."""
    return max(200, min(1000, 400_000 // max(1, total_records)))


def paired_bootstrap(
    records_by_agent: dict[int, Sequence[Any]],
    score_fn,
    seed: int = 0,
    resamples: int | None = None,
) -> dict[int, list[float]]:
    """Resampled reliability scores per agent (same task draws for every agent).

    ``score_fn(records) -> float | None``. Agents whose score is undefined in a draw get no value for it.
    """
    tasks = sorted({r.task_id for rs in records_by_agent.values() for r in rs})
    if not tasks:
        return {a: [] for a in records_by_agent}
    by_agent_task: dict[int, dict[int, list[Any]]] = {}
    for agent, rs in records_by_agent.items():
        grouped: dict[int, list[Any]] = defaultdict(list)
        for r in rs:
            grouped[r.task_id].append(r)
        by_agent_task[agent] = grouped
    total = sum(len(rs) for rs in records_by_agent.values())
    b = resamples or resample_count(total)
    rng = random.Random(seed)
    draws: dict[int, list[float]] = {a: [] for a in records_by_agent}
    for _ in range(b):
        picked = [rng.choice(tasks) for _ in tasks]
        for agent, grouped in by_agent_task.items():
            sample = []
            for i, t in enumerate(picked):  # a task drawn twice counts as two distinct tasks
                sample.extend(_relabel(r, i) for r in grouped.get(t, []))
            if sample:
                value = score_fn(sample)
                if value is not None:
                    draws[agent].append(value)
    return draws


class _Relabelled:
    """A record viewed with a new task id (so duplicate draws stay separate tasks)."""

    __slots__ = ("_r", "task_id")

    def __init__(self, record: Any, task_id: int):
        self._r = record
        self.task_id = task_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._r, name)


def _relabel(record: Any, draw_index: int) -> _Relabelled:
    return _Relabelled(record, draw_index)


def interval(values: Sequence[float]) -> tuple[float, float] | None:
    if len(values) < 20:
        return None
    s = sorted(values)
    return _percentile(s, 0.025), _percentile(s, 0.975)


def paired_p_value(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Two-sided bootstrap p-value for a ≠ b from paired resampled scores."""
    n = min(len(a), len(b))
    if n < 20:
        return None
    diffs = [a[i] - b[i] for i in range(n)]
    le = sum(d <= 0 for d in diffs) / n
    ge = sum(d >= 0 for d in diffs) / n
    return min(1.0, 2 * min(le, ge))


def rank_tiers(order: Sequence[int], draws: dict[int, list[float]]) -> tuple[dict[int, int], list[dict]]:
    """Tier per agent (1 = top) and the pairwise comparisons used to build them."""
    tiers: dict[int, int] = {}
    comparisons: list[dict] = []
    tier, leader = 0, None
    for agent in order:
        if leader is None:
            tier, leader = 1, agent
        else:
            p = paired_p_value(draws.get(leader, []), draws.get(agent, []))
            comparisons.append({"a": leader, "b": agent, "p": p})
            if p is not None and p < ALPHA:
                tier, leader = tier + 1, agent
        tiers[agent] = tier
    return tiers, comparisons


def power_note(n_tasks: int, repeats: int, half_widths: Sequence[float]) -> str:
    """Plain-language note on how big a difference this run can detect."""
    if not half_widths:
        return "Not enough data yet for uncertainty estimates."
    typical = sorted(half_widths)[len(half_widths) // 2]
    note = (
        f"With {n_tasks} tasks × {repeats} repeat{'s' if repeats != 1 else ''}, reliability scores are "
        f"uncertain by about ±{typical * 100:.0f} points; smaller gaps between agents may be noise."
    )
    if n_tasks < 10 or repeats < 3:
        note += " More tasks or repeats narrow this."
    return note


def fleiss_kappa(items: Sequence[Sequence[bool]]) -> float | None:
    """Fleiss' κ for pass/fail ratings; every item must have the same number (≥ 2) of raters."""
    rated = [list(v) for v in items if len(v) >= 2]
    if not rated:
        return None
    n = len(rated[0])
    rated = [v for v in rated if len(v) == n]
    if len(rated) < 2:
        return None
    big_n = len(rated)
    p_pass = sum(sum(v) for v in rated) / (big_n * n)
    p_e = p_pass**2 + (1 - p_pass) ** 2
    p_bar = (
        sum((sum(v) * (sum(v) - 1) + (n - sum(v)) * (n - sum(v) - 1)) / (n * (n - 1)) for v in rated) / big_n
    )
    if math.isclose(p_e, 1.0):
        return None  # every rating identical: agreement beyond chance can't be measured
    return (p_bar - p_e) / (1 - p_e)


def kappa_label(k: float | None) -> str:
    """Landis & Koch interpretation."""
    if k is None:
        return "not measurable (too few votes, or every verdict was the same)"
    if k < 0:
        return "no agreement"
    for limit, label in ((0.2, "slight"), (0.4, "fair"), (0.6, "moderate"), (0.8, "substantial")):
        if k <= limit:
            return f"{label} agreement"
    return "almost perfect agreement"
