"""Shared Streamlit UI helpers: sidebar status, execution summaries and trace rendering."""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

import api

STEP_ICONS = {
    "llm_call": "🧠",
    "tool_call": "🛠️",
    "tool_result": "📦",
    "error": "❌",
    "retry": "🔁",
    "final": "✅",
}
STATUS_LABELS = {"ok": "✅ ok", "error": "❌ error", "max_steps": "⏱️ max steps"}


def page_setup(title: str, icon: str = "📈") -> dict | None:
    """Configure the page and render the sidebar status. Returns the health payload (or None)."""
    st.set_page_config(page_title=f"{title} · AgentPulse", page_icon=icon, layout="wide")
    return sidebar_status()


def sidebar_status() -> dict | None:
    data, err = api.health()
    if err and "401" in err and st.session_state.get("access_code"):
        # A stale/wrong code must never lock the user out: drop it and fall back to mock mode.
        st.session_state["access_code"] = ""
        st.session_state["access_code_error"] = "Invalid access code."
        data, err = api.health()
    with st.sidebar:
        st.markdown("### System status")
        if err:
            st.error(err)
            return None
        if data.get("mock_mode"):
            st.warning("**MOCK MODE**: simulated LLM and search responses")
        else:
            st.info("**LIVE MODE**: Bifrost + Tavily")
        if data.get("public_deployment"):
            _access_code_box(live=not data.get("mock_mode"))
        for name, comp in data.get("components", {}).items():
            icon = "🟢" if comp["ok"] else "🔴"
            st.caption(f"{icon} {name}: {comp['detail']}")
        st.caption(f"Backend v{data['version']}")
    return data


def _access_code_box(live: bool) -> None:
    """Public demo: everyone gets mock mode; an access code unlocks live runs and admin actions."""
    if live:
        st.success("🔓 Live mode unlocked")
        if st.button("Lock (back to mock)", key="lock_live"):
            st.session_state["access_code"] = ""
            st.rerun()
        return
    with st.expander("🔒 Unlock live mode"):
        st.caption("Real models via Bifrost + live Tavily search. Ask the team for the access code.")
        code = st.text_input("Access code", type="password", key="access_code_input")
        if st.button("Unlock", key="unlock_live") and code:
            data, err = api.health(code=code)
            if err or data is None or data.get("mock_mode"):
                st.session_state["access_code_error"] = "Invalid access code."
            else:
                st.session_state["access_code"] = code
                st.session_state.pop("access_code_error", None)
            st.rerun()
        if st.session_state.get("access_code_error"):
            st.error(st.session_state["access_code_error"])


def show_error(err: str | None) -> bool:
    """Render ``err`` if set; returns True when there was an error."""
    if err:
        st.error(err)
        return True
    return False


def execution_summary(ex: dict[str, Any]) -> None:
    """Status line plus a metric row for one execution."""
    st.markdown(f"**Status:** {STATUS_LABELS.get(ex['status'], ex['status'])}")
    cols = st.columns(6)
    cols[0].metric("Latency", f"{ex['latency_ms'] / 1000:.2f} s")
    cols[1].metric("Tokens", f"{ex['prompt_tokens'] + ex['completion_tokens']:,}")
    cols[2].metric("Model calls", ex["steps"])
    cols[3].metric("Tool calls", ex["tool_calls"])
    cols[4].metric("Faults injected", ex["faults_injected"])
    cols[5].metric("LLM retries", ex["retries"])
    if ex.get("recovered"):
        st.success("🛡️ Recovered: a failure occurred during the run, but the agent still produced an answer.")
    if ex.get("error"):
        st.error(ex["error"])


def render_trace(trace: list[dict[str, Any]]) -> None:
    """Render an execution trace as a step-by-step timeline."""
    if not trace:
        st.info("No trace steps recorded.")
        return
    for step in trace:
        p = step["payload"]
        icon = STEP_ICONS.get(step["type"], "•")
        latency = f" · {step['latency_ms']:.0f} ms" if step["latency_ms"] else ""
        tokens = step["prompt_tokens"] + step["completion_tokens"]
        tok = f" · {tokens} tok" if tokens else ""
        title, body = _describe(step["type"], p)
        expanded = step["type"] in {"error", "final"} or (
            step["type"] == "tool_result" and not p.get("ok", True)
        )
        with st.expander(f"{icon} **{step['idx']}. {title}**{latency}{tok}", expanded=expanded):
            if body:
                body()
            st.caption("Raw payload")
            st.json(p, expanded=False)


def _describe(kind: str, p: dict[str, Any]):
    if kind == "llm_call":
        calls = p.get("tool_calls") or []
        if calls:
            names = ", ".join(c["name"] for c in calls)
            return f"Model → requests tool: {names}", lambda: st.code(
                json.dumps(calls, indent=2, ensure_ascii=False), language="json"
            )
        return "Model → answer", lambda: st.markdown(p.get("content") or "_(empty)_")
    if kind == "tool_call":
        return f"Call `{p.get('name')}`", lambda: st.code(
            json.dumps(p.get("arguments"), ensure_ascii=False), language="json"
        )
    if kind == "tool_result":
        if p.get("fault"):
            return f"`{p.get('name')}` → 💥 injected fault: {p['fault']}", lambda: st.code(
                p.get("content", "")
            )
        status = "ok" if p.get("ok") else "failed"
        return f"`{p.get('name')}` → {status}", lambda: st.code(p.get("content", ""), language=None)
    if kind == "error":
        code = f" [{p['status_code']}]" if p.get("status_code") else ""
        return f"Error: {p.get('kind', 'error')}{code}", lambda: st.error(p.get("message", ""))
    if kind == "retry":
        return f"Retry #{p.get('attempt', 0) - 1} after {p.get('backoff_ms', 0):.0f} ms backoff", None
    if kind == "final":
        return "Final answer", lambda: st.markdown(p.get("content") or "_(empty)_")
    return kind, None


# ------------------------------------------------------------------------------------ formatting
def pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.0f}%"


def secs(ms: float | None) -> str:
    return "–" if ms is None else f"{ms / 1000:.2f} s"


def usd(v: float | None) -> str:
    if v is None:
        return "–"
    if v == 0:
        return "$0"
    return f"${v:.4f}" if v >= 0.0001 else f"${v:.6f}"


RUN_STATUS = {
    "pending": "⏳ pending",
    "running": "🏃 running",
    "done": "✅ done",
    "cancelled": "🛑 cancelled",
    "failed": "❌ failed",
}


def run_label(r: dict) -> str:
    mode = "mock" if r["mock_mode"] else "live"
    return (
        f"#{r['id']} · {r['suite_name']} · {len(r['agent_ids'])} agents × {r['repeats']} · "
        f"faults {r['fault_rate']:.0%} · {mode} · {RUN_STATUS.get(r['status'], r['status'])}"
    )


def leaderboard_frame(board: list[dict]):
    import pandas as pd

    return pd.DataFrame(
        [
            {
                "Rank": r["rank"],
                "Agent": r["agent_name"],
                "Model": r["model"],
                "Reliability": pct(r["reliability_score"]),
                "Accuracy": pct(r["accuracy"]),
                "Consistency": pct(r["consistency"]),
                "Recovery": pct(r["recovery_rate"]),
                "Error rate": pct(r["error_rate"]),
                "p50 latency": secs(r["latency_ms_p50"]),
                "p95 latency": secs(r["latency_ms_p95"]),
                "Avg tokens": "–" if r["tokens_mean"] is None else f"{r['tokens_mean']:,.0f}",
                "Total cost": usd(r["cost_usd_total"]),
                "Done": r["executions"],
            }
            for r in board
        ]
    )
