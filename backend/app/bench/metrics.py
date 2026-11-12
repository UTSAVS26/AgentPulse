"""Aggregate benchmark metrics per agent (pure functions; formulas documented in docs/metrics.md).

Definitions (per agent, over all its executions in a run):

accuracy          passed / executions                       (errors count as failures)
mean_score        mean of per-execution scores (0–1; judge scores can be fractional)
consistency       share of tasks whose pass/fail outcome is identical across all repeats
                  (only defined when repeats ≥ 2)
pass_at_k         share of tasks passed in at least one repeat
pass_all_k        share of tasks passed in every repeat
error_rate        executions with status error or max_steps / executions
recovery_rate     recovered / executions that experienced a failure (injected fault, tool error
                  or LLM retry); recovered = failure happened AND the task still passed
latency p50/p95   percentiles of per-execution latency (ms, linear interpolation)
cost              list-price USD from token usage; cost_per_correct = total cost / passed
freshness         mean freshness score of the answers the stale-answer detector checked
                  (time-sensitive tasks only); stale_rate = answers judged stale / answers checked.
                  Reported next to, not inside, the reliability score.

reliability_score = weighted mean of the available components:
    accuracy 0.40 · consistency 0.25 · recovery_rate 0.20 · (1 − error_rate) 0.15
  Components that are undefined for a run (consistency with 1 repeat, recovery with no failures)
  are dropped and the remaining weights renormalised, so scores stay on a 0–1 scale.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.clients.chaos import OUTAGE_MESSAGE

RELIABILITY_WEIGHTS: dict[str, float] = {
    "accuracy": 0.40,
    "consistency": 0.25,
    "recovery_rate": 0.20,
    "stability": 0.15,  # 1 - error_rate
}


FRESHNESS_VERDICTS = ("fresh", "stale", "wrong", "unverified", "timeless", "no_claims")


@dataclass(frozen=True)
class ExecRecord:
    """The fields of an execution that metrics need (decoupled from the ORM)."""

    agent_id: int
    task_id: int
    repeat_idx: int
    passed: bool
    score: float
    status: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    steps: int
    tool_calls: int
    faults_injected: int
    retries: int
    tool_errors: int
    recovered: bool
    freshness_score: float | None = None
    freshness_verdict: str | None = None
    jury: tuple[bool, ...] | None = None  # each juror's pass/fail (scorer: jury)
    fallback_calls: int = 0  # model calls answered by a Bifrost fallback
    gateway_ms: float | None = None  # Bifrost's own time over the execution's model calls
    error: str | None = None

    @property
    def had_failure(self) -> bool:
        return self.faults_injected > 0 or self.retries > 0 or self.tool_errors > 0

    @classmethod
    def from_orm(cls, e: Any) -> ExecRecord:
        return cls(
            agent_id=e.agent_id,
            task_id=e.task_id or 0,
            repeat_idx=e.repeat_idx,
            passed=bool(e.passed),
            score=float(e.score or 0.0),
            status=e.status,
            latency_ms=float(e.latency_ms or 0.0),
            prompt_tokens=e.prompt_tokens or 0,
            completion_tokens=e.completion_tokens or 0,
            cost_usd=float(e.cost_usd or 0.0),
            steps=e.steps or 0,
            tool_calls=e.tool_calls or 0,
            faults_injected=e.faults_injected or 0,
            retries=e.retries or 0,
            tool_errors=e.tool_errors or 0,
            recovered=bool(e.recovered),
            freshness_score=getattr(e, "freshness_score", None),
            freshness_verdict=getattr(e, "freshness_verdict", None),
            jury=_jury_votes(getattr(e, "jury", None)),
            fallback_calls=getattr(e, "fallback_calls", None) or 0,
            gateway_ms=getattr(e, "gateway_ms", None),
            error=getattr(e, "error", None),
        )


def _jury_votes(raw: Any) -> tuple[bool, ...] | None:
    if not raw:
        return None
    votes = tuple(bool(v.get("passed")) for v in raw if isinstance(v, dict) and v.get("passed") is not None)
    return votes or None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0–100); None for an empty sequence."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = (len(data) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return data[lo] + (data[hi] - data[lo]) * (pos - lo)


def _mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def reliability_score(components: dict[str, float | None]) -> tuple[float | None, dict[str, float]]:
    """Weighted mean over defined components. Returns ``(score, weights actually used)``."""
    used = {k: w for k, w in RELIABILITY_WEIGHTS.items() if components.get(k) is not None}
    total = sum(used.values())
    if not total:
        return None, {}
    weights = {k: w / total for k, w in used.items()}
    return sum(components[k] * w for k, w in weights.items()), weights


def reliability_components(records: Sequence[ExecRecord]) -> dict[str, float | None]:
    """The four reliability components for one agent's executions (shared by the leaderboard and the
    bootstrap in ``bench/stats.py``, so both use exactly the same definitions)."""
    n = len(records)
    by_task: dict[int, list[ExecRecord]] = defaultdict(list)
    for r in records:
        by_task[r.task_id].append(r)
    multi = [rs for rs in by_task.values() if len(rs) >= 2]
    failed = [r for r in records if r.had_failure]
    error_rate = _ratio(sum(r.status != "ok" for r in records), n)
    return {
        "accuracy": _ratio(sum(r.passed for r in records), n),
        "consistency": _ratio(sum(len({r.passed for r in rs}) == 1 for rs in multi), len(multi)),
        "recovery_rate": _ratio(sum(r.recovered for r in failed), len(failed)),
        "stability": None if error_rate is None else 1 - error_rate,
    }


def agent_metrics(records: Sequence[ExecRecord]) -> dict[str, Any]:
    """Metrics for one agent's executions."""
    n = len(records)
    passed = sum(r.passed for r in records)
    errors = sum(r.status != "ok" for r in records)

    by_task: dict[int, list[ExecRecord]] = defaultdict(list)
    for r in records:
        by_task[r.task_id].append(r)
    multi = {t: rs for t, rs in by_task.items() if len(rs) >= 2}
    consistency = _ratio(sum(len({r.passed for r in rs}) == 1 for rs in multi.values()), len(multi))
    pass_at_k = _ratio(sum(any(r.passed for r in rs) for rs in by_task.values()), len(by_task))
    pass_all_k = _ratio(sum(all(r.passed for r in rs) for rs in by_task.values()), len(by_task))

    failed_runs = [r for r in records if r.had_failure]
    recovered = sum(r.recovered for r in failed_runs)
    latencies = [r.latency_ms for r in records]
    tokens = [r.prompt_tokens + r.completion_tokens for r in records]
    cost_total = sum(r.cost_usd for r in records)

    accuracy = _ratio(passed, n)
    error_rate = _ratio(errors, n)
    recovery_rate = _ratio(recovered, len(failed_runs))
    components = {
        "accuracy": accuracy,
        "consistency": consistency,
        "recovery_rate": recovery_rate,
        "stability": None if error_rate is None else 1 - error_rate,
    }
    score, weights = reliability_score(components)

    checked = [r for r in records if r.freshness_verdict]
    verdicts = Counter(r.freshness_verdict for r in checked)

    llm_calls = sum(r.steps for r in records)
    fallback_calls = sum(r.fallback_calls for r in records)
    per_call_overhead = [r.gateway_ms / r.steps for r in records if r.gateway_ms is not None and r.steps]

    return {
        "executions": n,
        "passed": passed,
        "accuracy": accuracy,
        "mean_score": _mean(r.score for r in records),
        "consistency": consistency,
        "pass_at_k": pass_at_k,
        "pass_all_k": pass_all_k,
        "error_rate": error_rate,
        "errors": errors,
        "executions_with_failures": len(failed_runs),
        "faults_injected": sum(r.faults_injected for r in records),
        "llm_retries": sum(r.retries for r in records),
        "recovered": recovered,
        "recovery_rate": recovery_rate,
        "latency_ms_mean": _mean(latencies),
        "latency_ms_p50": percentile(latencies, 50),
        "latency_ms_p95": percentile(latencies, 95),
        "tokens_mean": _mean(tokens),
        "tokens_total": sum(tokens),
        "cost_usd_total": cost_total,
        "cost_usd_mean": _mean(r.cost_usd for r in records),
        "cost_per_correct_usd": cost_total / passed if passed else None,
        "steps_mean": _mean(r.steps for r in records),
        "tool_calls_mean": _mean(r.tool_calls for r in records),
        "reliability_score": score,
        "reliability_weights": weights,
        "freshness_checked": len(checked),
        "freshness": _mean(r.freshness_score for r in checked if r.freshness_score is not None),
        "stale_answers": verdicts.get("stale", 0),
        "stale_rate": _ratio(verdicts.get("stale", 0), len(checked)),
        "freshness_verdicts": {v: verdicts.get(v, 0) for v in FRESHNESS_VERDICTS},
        # Bifrost: fallbacks (outage drills or real outages) and the gateway's own overhead per model call
        "llm_calls": llm_calls,
        "fallback_calls": fallback_calls,
        "fallback_rate": _ratio(fallback_calls, llm_calls),
        "rescued_by_gateway": sum(r.status == "ok" and r.fallback_calls > 0 for r in records),
        "outage_failures": sum(r.status == "error" and OUTAGE_MESSAGE in (r.error or "") for r in records),
        "gateway_overhead_ms_p50": percentile(per_call_overhead, 50),
        "gateway_overhead_ms_p95": percentile(per_call_overhead, 95),
    }


def run_metrics(
    records: Sequence[ExecRecord],
    agents: dict[int, dict[str, Any]],
    tasks: dict[int, dict[str, Any]],
    seed: int = 0,
) -> dict[str, Any]:
    """Metrics for a whole run.

    ``agents``: id → {"name", "model"}; ``tasks``: id → {"key", "category", "time_sensitive"}.
    Returns a leaderboard (sorted by reliability score, then accuracy), per-category accuracy and
    a task × agent pass-rate matrix.
    """
    by_agent: dict[int, list[ExecRecord]] = defaultdict(list)
    for r in records:
        by_agent[r.agent_id].append(r)

    leaderboard = []
    for agent_id, info in agents.items():
        rs = by_agent.get(agent_id, [])
        row = {"agent_id": agent_id, "agent_name": info["name"], "model": info["model"], **agent_metrics(rs)}
        cats: dict[str, list[ExecRecord]] = defaultdict(list)
        for r in rs:
            cats[tasks.get(r.task_id, {}).get("category", "general")].append(r)
        row["accuracy_by_category"] = {c: sum(x.passed for x in v) / len(v) for c, v in sorted(cats.items())}
        leaderboard.append(row)
    leaderboard.sort(key=lambda r: (r["reliability_score"] or -1, r["accuracy"] or -1), reverse=True)
    for rank, row in enumerate(leaderboard, 1):
        row["rank"] = rank
    statistics = _statistics(leaderboard, by_agent, seed)

    matrix = []
    for task_id, info in tasks.items():
        cells = {}
        for agent_id in agents:
            rs = [r for r in by_agent.get(agent_id, []) if r.task_id == task_id]
            cells[str(agent_id)] = sum(r.passed for r in rs) / len(rs) if rs else None
        matrix.append({"task_id": task_id, **info, "pass_rate_by_agent": cells})

    return {
        "leaderboard": leaderboard,
        "task_matrix": matrix,
        "weights": RELIABILITY_WEIGHTS,
        "total_executions": len(records),
        "statistics": statistics,
        "jury": _jury_summary(records, tasks),
    }


def _bootstrap_score(records: Sequence[Any]) -> float | None:
    return reliability_score(reliability_components(records))[0]


def _statistics(
    leaderboard: list[dict[str, Any]], by_agent: dict[int, list[ExecRecord]], seed: int
) -> dict[str, Any]:
    """Intervals, rank tiers and pairwise tests (bench/stats.py); annotates the leaderboard rows."""
    from app.bench import stats

    for row in leaderboard:
        ci = stats.wilson(row["passed"], row["executions"])
        row["accuracy_ci"] = list(ci) if ci else None
        row["reliability_ci"], row["tier"], row["tied_with"] = None, None, []
    scored = {
        r["agent_id"]: by_agent[r["agent_id"]] for r in leaderboard if r["reliability_score"] is not None
    }
    all_records = [x for rs in scored.values() for x in rs]
    n_tasks = len({x.task_id for x in all_records})
    repeats = max((x.repeat_idx for x in all_records), default=-1) + 1
    if not scored:
        return {"method": "paired task-level bootstrap", "resamples": 0, "comparisons": [],
                "power_note": stats.power_note(0, 0, [])}  # fmt: skip
    draws = stats.paired_bootstrap(scored, _bootstrap_score, seed=seed)
    half_widths = []
    for row in leaderboard:
        ci = stats.interval(draws.get(row["agent_id"], []))
        if ci and ci[1] - ci[0] > 1e-9:  # a zero-width interval (all pass / all fail) would claim certainty
            row["reliability_ci"] = list(ci)
        boot_hw = (ci[1] - ci[0]) / 2 if ci else 0.0
        # Floor with the Wilson accuracy interval: 5/5 passes is not "±0" (Wilson: 57–100%).
        acc = row.get("accuracy_ci")
        wilson_hw = (acc[1] - acc[0]) / 2 if acc else 0.0
        if row["agent_id"] in scored:
            half_widths.append(max(boot_hw, wilson_hw))
    order = [r["agent_id"] for r in leaderboard if r["agent_id"] in scored]
    tiers, comparisons = stats.rank_tiers(order, draws)
    names = {r["agent_id"]: r["agent_name"] for r in leaderboard}
    for row in leaderboard:
        tier = tiers.get(row["agent_id"])
        row["tier"] = tier
        row["tied_with"] = (
            [names[a] for a, t in tiers.items() if t == tier and a != row["agent_id"]] if tier else []
        )
    return {
        "method": "paired task-level bootstrap",
        "resamples": len(next(iter(draws.values()), [])),
        "tasks": n_tasks,
        "repeats": repeats,
        "power_note": stats.power_note(n_tasks, repeats, half_widths),
        "comparisons": [
            {
                "a": names[c["a"]],
                "b": names[c["b"]],
                "p": c["p"],
                "significant": c["p"] is not None and c["p"] < stats.ALPHA,
            }
            for c in comparisons
        ],
    }


def _jury_summary(records: Sequence[ExecRecord], tasks: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    """Fleiss' κ across jurors and the tasks they disagreed on most."""
    from app.bench import stats

    voted = [r for r in records if r.jury]
    if not voted:
        return None
    kappa = stats.fleiss_kappa([r.jury for r in voted])
    split: dict[int, int] = defaultdict(int)
    for r in voted:
        if len(set(r.jury)) > 1:
            split[r.task_id] += 1
    disputed = sorted(split.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "verdicts": len(voted),
        "jurors": max(len(r.jury) for r in voted),
        "kappa": kappa,
        "agreement": stats.kappa_label(kappa),
        "unanimous_share": sum(len(set(r.jury)) == 1 for r in voted) / len(voted),
        "disputed": [{"task": tasks.get(t, {}).get("key", str(t)), "split_votes": n} for t, n in disputed],
    }
