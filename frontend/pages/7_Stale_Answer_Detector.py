"""Stale-Answer Detector: find claims that were true once but are not any more (Tavily-verified)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
from ui import ANSWER_VERDICTS, CLAIM_VERDICTS, page_setup, pct, show_error

health = page_setup("Stale-Answer Detector", "🕰️")
st.title("🕰️ Stale-answer detector")
st.caption(
    "Splits a text into claims, flags the time-sensitive ones (office holders, champions, latest versions, "
    "prices, today's date…) and checks each against live **Tavily** search results and their publication "
    "dates. Timeless claims are not searched."
)
live = health is not None and not health.get("mock_mode")
if health is not None and not live:
    st.info(
        "**Mock mode:** search results come from a small built-in corpus (UK Prime Minister, World Cup, "
        "latest Python, UN Secretary-General, tallest building). Other topics come back *unverifiable*. "
        + (
            "Unlock live mode in the sidebar for real Tavily verification."
            if health.get("live_available")
            else ""
        )
    )

EXAMPLES = {
    "A stale answer": (
        "The current UK Prime Minister is Rishi Sunak. Argentina won the latest FIFA World Cup. "
        "Water boils at 100 °C at sea level.",
        "",
    ),
    "An up-to-date answer": (
        "Andy Burnham is the Prime Minister of the United Kingdom, and Spain are the reigning FIFA World Cup "
        "champions.",
        "",
    ),
    "A short answer (needs its question)": ("Python 3.13.", "What is the latest stable version of Python?"),
    "Today's date": ("It is 2024.", "What year is it right now?"),
}


def _show_check(check_id: int) -> None:
    st.query_params["check"] = str(check_id)
    st.query_params.pop("execution", None)


# ------------------------------------------------------------------------------------ input
linked_check = st.query_params.get("check")
linked_exec = st.query_params.get("execution")
sources = ["Paste text", "An agent's answer", "History"]
source = st.radio(
    "What to check",
    sources,
    horizontal=True,
    index=1 if linked_exec else (2 if linked_check else 0),
)

if source == "Paste text":
    c1, c2 = st.columns([3, 1])
    example = c2.selectbox("Load an example", ["–", *EXAMPLES])
    if example != "–" and st.session_state.get("_example") != example:
        st.session_state["_example"] = example
        st.session_state["fc_text"], st.session_state["fc_question"] = EXAMPLES[example]
    with c1:
        text = st.text_area(
            "Text to check", key="fc_text", height=130, max_chars=6000, placeholder="Paste an AI answer…"
        )
    question = st.text_input(
        "Question it answers (optional)",
        key="fc_question",
        help="Gives short answers context: '2024' means nothing without 'What year is it?'.",
    )
    depth = st.radio(
        "Search depth",
        ["advanced", "basic"],
        horizontal=True,
        help="Tavily 'advanced' finds better sources (2 credits per search); 'basic' is 1 credit.",
    )
    if st.button("Check freshness", type="primary", disabled=not text.strip()):
        with st.spinner("Extracting claims, searching Tavily and judging each claim…"):
            check, err = api.post(
                "/freshness/check",
                json={"text": text, "question": question or None, "search_depth": depth},
                timeout=120,
            )
        if not show_error(err):
            _show_check(check["id"])
            st.rerun()

elif source == "An agent's answer":
    default = int(linked_exec) if linked_exec and linked_exec.isdigit() else 1
    exec_id = int(st.number_input("Execution ID", min_value=1, step=1, value=default))
    ex, err = api.get(f"/executions/{exec_id}")
    if show_error(err):
        st.stop()
    st.markdown(f"**{ex['agent_name']}** · `{ex['model']}` · [trace ↗](Traces?execution={ex['id']})")
    st.markdown(f"**Q:** {ex['prompt']}")
    st.markdown(f"**A:** {ex['output'] or '_(no answer)_'}")
    previous, _ = api.get("/freshness/checks", params={"execution_id": exec_id, "limit": 1})
    c1, c2 = st.columns([1, 3])
    if c1.button("Check this answer", type="primary", disabled=not (ex["output"] or "").strip()):
        with st.spinner("Searching Tavily and judging each claim…"):
            check, err = api.post("/freshness/check", json={"execution_id": exec_id}, timeout=120)
        if not show_error(err):
            _show_check(check["id"])
            st.rerun()
    if previous and str(previous[0]["id"]) != linked_check:
        c2.caption(f"Already checked: {ANSWER_VERDICTS.get(previous[0]['verdict'], '')}. Showing that check.")
        linked_check = str(previous[0]["id"])

else:  # history
    show_auto = st.checkbox("Include automatic checks from benchmark runs")
    history, err = api.get("/freshness/checks", params={"limit": 100, "manual_only": not show_auto})
    if show_error(err):
        st.stop()
    if not history:
        st.info("No checks yet. Paste a text on the first tab.")
        st.stop()
    ids = [h["id"] for h in history]
    by_id = {h["id"]: h for h in history}
    current = int(linked_check) if linked_check and linked_check.isdigit() else ids[0]
    picked = st.selectbox(
        "Check",
        ids,
        index=ids.index(current) if current in ids else 0,
        format_func=lambda i: (
            f"#{i} · {ANSWER_VERDICTS.get(by_id[i]['verdict'], by_id[i]['verdict'])} · "
            f"{by_id[i]['created_at'][:16].replace('T', ' ')} · {by_id[i]['input_text'][:70]}"
        ),
    )
    linked_check = str(picked)

# ------------------------------------------------------------------------------------ result
if not (linked_check and linked_check.isdigit()):
    st.stop()
check, err = api.get(f"/freshness/checks/{linked_check}")
if show_error(err):
    st.stop()

st.divider()
counts = check["counts"]
outdated, wrong = counts.get("outdated", 0), counts.get("contradicted", 0)
banner = {
    "stale": (st.warning, f"⚠️ **Stale answer**: {outdated} claim(s) are outdated."),
    "wrong": (st.error, f"❌ **Incorrect**: {wrong} claim(s) are contradicted by current sources."),
    "fresh": (st.success, "✅ **Up to date**: every checked time-sensitive claim is current."),
    "unverified": (
        st.info,
        "❔ **Unverified**: the time-sensitive claims could not be confirmed either way.",
    ),
    "timeless": (
        st.info,
        "➖ **Nothing to verify**: no time-sensitive claims (timeless facts aren't searched).",
    ),
    "no_claims": (st.info, "∅ **No factual claims** found in this text."),
}
show, message = banner.get(check["verdict"], (st.info, check["verdict"]))
show(message)

cols = st.columns(6)
cols[0].metric(
    "Freshness",
    pct(check["freshness_score"]),
    help="Current ÷ (current + outdated + contradicted) over time-sensitive claims.",
)
for col, verdict in zip(cols[1:], CLAIM_VERDICTS, strict=True):
    col.metric(CLAIM_VERDICTS[verdict], counts.get(verdict, 0))
meta = [
    f"Check #{check['id']}",
    "mock" if check["mock_mode"] else "live (Tavily)",
    f"claims extracted by {check['extraction']}",
]
if check["latency_ms"]:
    meta.append(f"{check['latency_ms'] / 1000:.1f} s")
if check["execution_id"]:
    meta.append(f"[execution #{check['execution_id']} ↗](Traces?execution={check['execution_id']})")
st.caption(" · ".join(meta))
if check["question"]:
    st.markdown(f"**Question:** {check['question']}")
with st.expander("Checked text"):
    st.markdown(check["input_text"])

order = {"outdated": 0, "contradicted": 1, "unverifiable": 2, "current": 3, "not_time_sensitive": 4}
for claim in sorted(check["claims"], key=lambda c: order.get(c["verdict"], 9)):
    with st.container(border=True):
        label = CLAIM_VERDICTS.get(claim["verdict"], claim["verdict"])
        conf = (
            f" · confidence {claim['confidence']:.0%}"
            if claim["time_sensitive"] and claim["confidence"]
            else ""
        )
        st.markdown(f"**{label}**{conf}  \n{claim['text']}")
        if claim.get("correction"):
            st.markdown(f"**True today:** {claim['correction']}")
        if claim["explanation"]:
            st.caption(claim["explanation"])
        if claim["time_sensitive"]:
            how = {"clock": "checked against the clock", "llm": "judged against the sources"}.get(
                claim.get("method") or "", claim.get("method") or ""
            )
            st.caption(f"🔎 Search: `{claim['query']}` · {how}")
        if claim["sources"]:
            lines = []
            for s in claim["sources"]:
                date = s.get("published_date") or "undated"
                title = s.get("title") or s.get("url")
                lines.append(f"- [{title}]({s['url']}) · {date}" if s.get("url") else f"- {title} · {date}")
            st.markdown("**Sources** (newest first)\n" + "\n".join(lines))

with st.expander("Data table"):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Verdict": CLAIM_VERDICTS.get(c["verdict"], c["verdict"]),
                    "Claim": c["text"],
                    "True today": c.get("correction") or "",
                    "Confidence": c["confidence"],
                    "Query": c["query"] or "",
                    "Sources": len(c["sources"]),
                    "Newest source": max((s.get("published_date") or "" for s in c["sources"]), default=""),
                }
                for c in check["claims"]
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
