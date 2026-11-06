"""Benchmark reports: one run as Markdown (to read and share), CSV (to analyse) or JSON (to archive).

Every number comes from the same functions as the dashboard (`metrics_payload`, `execution_rows`), so a
report always matches the Compare page. Formulas: docs/metrics.md.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__
from app.models import BenchmarkRun, FreshnessCheck
from app.routers.runs import execution_rows, metrics_payload

REPORT_VERSION = 1
MAX_FAILURES_MD = 50

LEADERBOARD_COLUMNS = [
    ("rank", "Rank"),
    ("agent_name", "Agent"),
    ("model", "Model"),
    ("route", "Route"),
    ("connection", "Connection"),
    ("reliability_score", "Reliability"),
    ("accuracy", "Accuracy"),
    ("consistency", "Consistency"),
    ("pass_at_k", "pass@k"),
    ("pass_all_k", "pass-all-k"),
    ("recovery_rate", "Recovery"),
    ("error_rate", "Error rate"),
    ("latency_ms_p50", "p50 latency (ms)"),
    ("latency_ms_p95", "p95 latency (ms)"),
    ("tokens_mean", "Tokens / task"),
    ("tokens_total", "Total tokens"),
    ("cost_usd_total", "Total cost (USD)"),
    ("cost_per_correct_usd", "Cost / correct (USD)"),
    ("steps_mean", "Model calls / task"),
    ("tool_calls_mean", "Tool calls / task"),
    ("faults_injected", "Faults injected"),
    ("llm_retries", "LLM retries"),
    ("executions", "Executions"),
    ("passed", "Passed"),
    ("freshness", "Freshness"),
    ("freshness_checked", "Freshness checked"),
    ("stale_answers", "Stale answers"),
]
EXECUTION_COLUMNS = [
    "id",
    "agent_name",
    "task_key",
    "category",
    "repeat_idx",
    "passed",
    "score",
    "status",
    "output",
    "error",
    "score_detail",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "steps",
    "tool_calls",
    "faults_injected",
    "retries",
    "tool_errors",
    "recovered",
    "freshness_score",
    "freshness_verdict",
]


# ------------------------------------------------------------------------------------ data
def build_report(db: Session, run: BenchmarkRun) -> dict[str, Any]:
    """Everything a report needs, as plain JSON-serialisable data."""
    metrics = metrics_payload(db, run)
    executions = [e.model_dump(mode="json") for e in execution_rows(db, run)]
    for e in executions:
        e["score_detail"] = (e.get("score_detail") or "").split(" · price:")[0]
    board = [r for r in metrics["leaderboard"] if r["executions"]]
    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agentpulse_version": __version__,
        "run": metrics["run"],
        "weights": metrics["weights"],
        "highlights": _highlights(board),
        "leaderboard": metrics["leaderboard"],
        "task_matrix": metrics["task_matrix"],
        "failures": [e for e in executions if not e["passed"]],
        "freshness": _freshness(db, board, executions),
        "executions": executions,
    }


def _best(board: list[dict], key: str, lowest: bool = False) -> dict | None:
    rows = [r for r in board if r.get(key) is not None]
    if not rows:
        return None
    r = (min if lowest else max)(rows, key=lambda x: x[key])
    return {"agent_name": r["agent_name"], "value": r[key]}


def _highlights(board: list[dict]) -> dict[str, Any]:
    return {
        "most_reliable": _best(board, "reliability_score"),
        "most_accurate": _best(board, "accuracy"),
        "fastest_p50_ms": _best(board, "latency_ms_p50", lowest=True),
        "cheapest_per_correct_usd": _best(board, "cost_per_correct_usd", lowest=True),
        "freshest": _best(board, "freshness"),
    }


def _freshness(db: Session, board: list[dict], executions: list[dict]) -> dict[str, Any] | None:
    if not any(r.get("freshness_checked") for r in board):
        return None
    flagged = [e for e in executions if e.get("freshness_verdict") in ("stale", "wrong")]
    checks: dict[int, FreshnessCheck] = {}
    if flagged:
        ids = [e["id"] for e in flagged]
        for c in db.scalars(
            select(FreshnessCheck).where(FreshnessCheck.execution_id.in_(ids)).order_by(FreshnessCheck.id)
        ):
            checks[c.execution_id] = c  # latest check per execution wins
    stale = []
    for e in flagged:
        check = checks.get(e["id"])
        claims = [cl for cl in (check.claims if check else []) if cl.verdict in ("outdated", "contradicted")]
        stale.append(
            {
                "execution_id": e["id"],
                "check_id": check.id if check else None,
                "agent_name": e["agent_name"],
                "task_key": e["task_key"],
                "repeat": e["repeat_idx"] + 1,
                "answer": e["output"],
                "verdict": e["freshness_verdict"],
                "claims": [
                    {
                        "claim": cl.text,
                        "verdict": cl.verdict,
                        "true_today": cl.correction,
                        "newest_source": next(iter(cl.sources or []), None),
                    }
                    for cl in claims
                ],
            }
        )
    return {
        "per_agent": [
            {
                "agent_name": r["agent_name"],
                "freshness": r.get("freshness"),
                "checked": r.get("freshness_checked", 0),
                "stale_answers": r.get("stale_answers", 0),
                "stale_rate": r.get("stale_rate"),
                "verdicts": r.get("freshness_verdicts", {}),
            }
            for r in board
        ],
        "stale_or_wrong": stale,
    }


# ------------------------------------------------------------------------------------ formatting
def route_label(route: str | None, connection: str | None = None) -> str:
    """How an agent's model is called: through the Bifrost gateway or directly (never shows keys)."""
    if route is None:
        return "–"
    where = "Bifrost" if route == "bifrost" else "direct"
    return f"{where} · {connection}" if connection else where


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.0f}%"


def _secs(ms: float | None) -> str:
    return "–" if ms is None else f"{ms / 1000:.2f} s"


def _usd(v: float | None) -> str:
    if v is None:
        return "–"
    if v == 0:
        return "$0"
    return f"${v:.4f}" if v >= 0.0001 else f"${v:.6f}"


def _num(v: float | None, digits: int = 0) -> str:
    return "–" if v is None else f"{v:,.{digits}f}"


def _cell(text: Any, limit: int = 140) -> str:
    """Make a value safe inside a Markdown table cell."""
    s = " ".join(str(text if text is not None else "").split())
    s = s.replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


VERDICT_ICON = {
    "fresh": "✅",
    "stale": "⚠️",
    "wrong": "❌",
    "unverified": "❔",
    "timeless": "➖",
    "no_claims": "∅",
}


def to_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    board = [r for r in report["leaderboard"] if r["executions"]]
    mode = "mock (simulated LLM and search)" if run["mock_mode"] else "live (Bifrost + Tavily)"
    out: list[str] = [
        f"# AgentPulse benchmark report: run #{run['id']}",
        "",
        f"**Suite:** {run['suite_name']} · **Mode:** {mode} · **Status:** {run['status']}  ",
        f"**Agents:** {len(run['agent_ids'])} · **Repeats:** {run['repeats']} · "
        f"**Fault injection:** {run['fault_rate']:.0%} of tool calls · "
        f"**Executions:** {run['progress_done']}/{run['progress_total']}  ",
        f"**Started:** {(run['started_at'] or '–')[:19].replace('T', ' ')} UTC · "
        f"**Duration:** {_num(run['duration_s'])} s · "
        f"**Generated:** {report['generated_at'][:19].replace('T', ' ')} UTC "
        f"by AgentPulse v{report['agentpulse_version']}",
        "",
    ]
    if run["status"] in ("pending", "running"):
        out += ["> ⚠️ The run was still in progress when this report was generated: figures are partial.", ""]

    h = report["highlights"]
    out += ["## Highlights", ""]
    items = [
        ("🏆 Most reliable", h["most_reliable"], _pct),
        ("🎯 Most accurate", h["most_accurate"], _pct),
        ("⚡ Fastest (p50)", h["fastest_p50_ms"], _secs),
        ("💰 Cheapest per correct answer", h["cheapest_per_correct_usd"], _usd),
        ("🕰️ Freshest answers", h["freshest"], _pct),
    ]
    out += [f"- **{label}:** {x['agent_name']} ({fmt(x['value'])})" for label, x, fmt in items if x]
    out.append("")

    weights = " · ".join(f"{k.replace('_', ' ')} {v:.0%}" for k, v in report["weights"].items())
    out += [
        "## Leaderboard",
        "",
        _table(
            [
                "#",
                "Agent",
                "Model",
                "Route",
                "Reliability",
                "Accuracy",
                "Consistency",
                "Recovery",
                "Errors",
                "p50",
                "p95",
            ]
            + ["Tokens/task", "Total cost", "Cost/correct"],
            [
                [
                    r["rank"],
                    r["agent_name"],
                    f"`{r['model']}`",
                    route_label(r.get("route"), r.get("connection")),
                    _pct(r["reliability_score"]),
                    _pct(r["accuracy"]),
                    _pct(r["consistency"]),
                    _pct(r["recovery_rate"]),
                    _pct(r["error_rate"]),
                    _secs(r["latency_ms_p50"]),
                    _secs(r["latency_ms_p95"]),
                    _num(r["tokens_mean"]),
                    _usd(r["cost_usd_total"]),
                    _usd(r["cost_per_correct_usd"]),
                ]
                for r in board
            ],
        ),
        "",
        f"Reliability = {weights}. Undefined components (consistency with 1 repeat, recovery when nothing "
        "failed) are dropped and the weights renormalised. Formulas: docs/metrics.md.",
        "",
        "## Reliability detail",
        "",
        _table(
            ["Agent", "pass@k", "pass-all-k", "Passed", "Executions", "Faults injected", "Recovered"]
            + ["LLM retries", "Model calls/task", "Tool calls/task"],
            [
                [
                    r["agent_name"],
                    _pct(r["pass_at_k"]),
                    _pct(r["pass_all_k"]),
                    r["passed"],
                    r["executions"],
                    r["faults_injected"],
                    f"{r['recovered']}/{r['executions_with_failures']}",
                    r["llm_retries"],
                    _num(r["steps_mean"], 2),
                    _num(r["tool_calls_mean"], 2),
                ]
                for r in board
            ],
        ),
        "",
    ]

    categories = sorted({c for r in board for c in (r.get("accuracy_by_category") or {})})
    if categories:
        out += [
            "## Accuracy by category",
            "",
            _table(
                ["Agent", *categories],
                [
                    [r["agent_name"], *(_pct(r["accuracy_by_category"].get(c)) for c in categories)]
                    for r in board
                ],
            ),
            "",
        ]

    names = {str(r["agent_id"]): r["agent_name"] for r in board}
    if report["task_matrix"] and names:
        out += [
            "## Pass rate per task",
            "",
            "⏰ = time-sensitive task. Cells: share of repeats passed.",
            "",
            _table(
                ["Task", *names.values()],
                [
                    [
                        t["key"] + (" ⏰" if t.get("time_sensitive") else ""),
                        *(_pct(t["pass_rate_by_agent"].get(a)) for a in names),
                    ]
                    for t in report["task_matrix"]
                ],
            ),
            "",
        ]

    fr = report["freshness"]
    if fr:
        out += [
            "## Freshness (stale-answer detection)",
            "",
            "Answers to time-sensitive tasks were checked by the stale-answer detector: claims extracted, "
            + (
                "searched in the built-in mock corpus (mock run)"
                if run["mock_mode"]
                else "searched live on Tavily"
            )
            + " and judged current, outdated or contradicted. See docs/stale-answer-detection.md.",
            "",
            _table(
                ["Agent", "Freshness", "Checked", "✅ fresh", "⚠️ stale", "❌ wrong", "❔ unverified"],
                [
                    [
                        a["agent_name"],
                        _pct(a["freshness"]),
                        a["checked"],
                        a["verdicts"].get("fresh", 0),
                        a["verdicts"].get("stale", 0),
                        a["verdicts"].get("wrong", 0),
                        a["verdicts"].get("unverified", 0),
                    ]
                    for a in fr["per_agent"]
                ],
            ),
            "",
        ]
        if fr["stale_or_wrong"]:
            out += ["### Stale or wrong answers", ""]
            for s in fr["stale_or_wrong"]:
                icon = VERDICT_ICON.get(s["verdict"], "")
                out.append(
                    f"- {icon} **{s['agent_name']}** · `{s['task_key']}` #{s['repeat']}: "
                    f"“{_cell(s['answer'], 160)}”"
                )
                for c in s["claims"]:
                    src = c.get("newest_source") or {}
                    cite = ""
                    if src.get("url"):
                        cite = f" ([{_cell(src.get('title') or src['url'], 80)}]({src['url']})"
                        cite += f", {src['published_date']})" if src.get("published_date") else ")"
                    today = f" **True today:** {_cell(c['true_today'], 200)}" if c.get("true_today") else ""
                    out.append(f"  - {c['verdict']}: {_cell(c['claim'], 200)}.{today}{cite}")
            out.append("")
        else:
            out += ["No stale or wrong answers. 🎉", ""]

    failures = report["failures"]
    out += [f"## Failed executions ({len(failures)} of {len(report['executions'])})", ""]
    if failures:
        out.append(
            _table(
                ["Agent", "Task", "Repeat", "Status", "Why", "Answer"],
                [
                    [
                        e["agent_name"],
                        e["task_key"],
                        e["repeat_idx"] + 1,
                        e["status"],
                        e["score_detail"] or e.get("error") or "",
                        e["output"] or e.get("error") or "",
                    ]
                    for e in failures[:MAX_FAILURES_MD]
                ],
            )
        )
        if len(failures) > MAX_FAILURES_MD:
            out.append(f"\n…and {len(failures) - MAX_FAILURES_MD} more (see the CSV or JSON export).")
    else:
        out.append("Every execution passed.")
    out += [
        "",
        "---",
        "Costs use each model's list price from Bifrost's model catalogue (local models are $0)"
        + ("; this was a mock run, so tokens and costs are simulated." if run["mock_mode"] else ".")
        + " The stale-answer detector's own LLM and search usage is not included in agent costs.",
        "",
    ]
    return "\n".join(out)


def to_csv(report: dict[str, Any], table: str = "leaderboard") -> str:
    """``table``: ``leaderboard`` (one row per agent) or ``executions`` (one row per attempt)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if table == "executions":
        w.writerow(EXECUTION_COLUMNS)
        for e in report["executions"]:
            w.writerow(["" if e.get(c) is None else e.get(c) for c in EXECUTION_COLUMNS])
    else:
        w.writerow([label for _, label in LEADERBOARD_COLUMNS])
        for r in report["leaderboard"]:
            w.writerow(["" if r.get(k) is None else r.get(k) for k, _ in LEADERBOARD_COLUMNS])
    return buf.getvalue()
