"""Freshness Guard: an OpenAI-compatible proxy that fact-checks every answer passing through it."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
from ui import ANSWER_VERDICTS, CLAIM_VERDICTS, DETECTOR_PAGE, page_setup, pct, show_error

health = page_setup("Freshness Guard", "🛡️")
st.title("🛡️ Freshness Guard")
st.caption(
    "Put AgentPulse between your app and its model. It forwards each chat request (through Bifrost), checks "
    "the answer's time-sensitive facts against live **Tavily** sources, and then **annotates**, **corrects** "
    "or **blocks** it. It speaks the OpenAI API, so any OpenAI SDK, LangChain app or chat UI can use it by "
    "changing one line: the base URL. Answers with nothing time-sensitive pass straight through."
)
live = health is not None and not health.get("mock_mode")
public = bool(health and health.get("public_deployment"))
if health is not None and not live:
    st.info(
        "**Mock mode:** a simulated model often answers questions on the demo topics (UK Prime Minister, "
        "World "
        "Cup, latest Python) from *old training data*, and the checks use a small built-in corpus. "
        + ("Unlock live mode in the sidebar to guard real models." if health.get("live_available") else "")
    )

MODES = {
    "annotate": "📝 Annotate: answer unchanged, verdict and sources attached",
    "correct": "✏️ Correct: rewrite stale answers from the dated sources, with citations",
    "block": "⛔ Block: replace stale answers with a warning and what's true now",
}

# ------------------------------------------------------------------------------------ use it
with st.expander("🔌 Use it from your app (one line)", expanded=False):
    base = "http://localhost:8000/guard/v1"
    if public:
        st.warning(
            "On this hosted demo only the dashboard is public, so try the Guard below. To call it from your "
            "own code, run AgentPulse yourself (`make up`) and use the URL shown here."
        )
    key = "<access code>" if public else "not-needed"
    t1, t2, t3 = st.tabs(["Python (OpenAI SDK)", "curl", "Modes & headers"])
    t1.code(
        f"""from openai import OpenAI

client = OpenAI(base_url="{base}", api_key="{key}")
resp = client.chat.completions.create(
    model="groq/openai/gpt-oss-20b",          # any model AgentPulse can route
    messages=[{{"role": "user", "content": "Who is the UK prime minister?"}}],
    extra_headers={{"X-AgentPulse-Guard": "correct"}},  # annotate | correct | block | off
)
print(resp.choices[0].message.content)
print(resp.model_extra["agentpulse"]["verdict"])   # fresh | stale | wrong | ...""",
        language="python",
    )
    t2.code(
        f"""curl -s {base}/chat/completions -H 'content-type: application/json' \\
  -H 'X-AgentPulse-Guard: block' \\
  -d '{{"model": "groq/openai/gpt-oss-20b",
       "messages": [{{"role": "user", "content": "Who won the latest World Cup?"}}]}}'""",
        language="bash",
    )
    t3.markdown(
        "- Choose the mode with the header `X-AgentPulse-Guard` or a `guard` field in the body; default "
        "`annotate`.\n"
        "- Every response carries an `agentpulse` object (verdict, freshness, claims with dated sources, the "
        "original answer when changed) and headers `X-AgentPulse-Verdict`, `X-AgentPulse-Freshness`, "
        "`X-AgentPulse-Action`, `X-AgentPulse-Check-Id`.\n"
        "- `stream: true` works: the checked answer is streamed as OpenAI chunks, with the report in the "
        "last "
        "chunk. Tool calls pass through unchecked. `GET /guard/v1/models` lists the models.\n"
        "- Checking adds a few seconds when an answer has time-sensitive facts (search + verdict)."
    )

# ------------------------------------------------------------------------------------ try it
st.markdown("### Try it")
EXAMPLES = {
    "Who is the current prime minister of the United Kingdom?": "time-sensitive",
    "Who won the latest FIFA World Cup?": "time-sensitive",
    "What is the latest stable version of Python?": "time-sensitive",
    "Write a two-line poem about rain.": "timeless: passes straight through",
}
agents, _ = api.get("/agents")
models = list(dict.fromkeys(a["model"] for a in agents or [])) or ["groq/openai/gpt-oss-20b"]
with st.container(border=True):
    c1, c2 = st.columns([2, 3])
    model = c1.selectbox(
        "Model", models, help="The model your app would call. Any id AgentPulse can route works."
    )
    mode = c2.radio("Guard mode", list(MODES), format_func=MODES.get, horizontal=False)
    example = st.selectbox(
        "Question", list(EXAMPLES), format_func=lambda q: f"{q}  ({EXAMPLES[q]})", key="guard_example"
    )
    prompt = st.text_area("Or write your own", value=example, height=80, key=f"guard_prompt_{example}")
    if st.button("🛡️ Send through the Guard", type="primary", disabled=not prompt.strip()):
        with st.spinner("Asking the model, then checking its facts…"):
            res, err = api.post(
                "/guard/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "guard": mode},
                timeout=240,
            )
        if not show_error(err):
            st.session_state["guard_result"] = res

res = st.session_state.get("guard_result")
if res:
    info = res.get("agentpulse") or {}
    answer = res["choices"][0]["message"].get("content") or ""
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**What your app receives**")
        with st.container(border=True):
            st.markdown(answer)
        if info.get("original"):
            with st.expander("Original answer from the model"):
                st.markdown(info["original"])
    with right:
        st.markdown("**Guard report**")
        if not info.get("checked"):
            st.info(f"Not checked: {info.get('reason', 'n/a')}. The answer passed straight through.")
        else:
            verdict = ANSWER_VERDICTS.get(info["verdict"], info["verdict"])
            did = {
                "annotated": "passed on unchanged, with this report attached",
                "corrected": "✏️ rewritten from the dated sources before reaching your app",
                "blocked": "⛔ blocked: your app received a warning instead",
            }.get(info["action"], info["action"])
            styles = {"fresh": st.success, "stale": st.warning, "wrong": st.error}
            banner = styles.get(info["verdict"], st.info)
            banner(f"**{verdict}** · freshness {pct(info.get('freshness_score'))}  \nAnswer {did}.")
            st.caption(
                f"{res['model']} · check {info['check_latency_ms'] / 1000:.1f} s · {info['mode']} mode · "
                f"[full check](/{DETECTOR_PAGE}?check={info['check_id']})"
            )
            for c in info["claims"]:
                if c["verdict"] == "not_time_sensitive":
                    continue
                st.markdown(f"{CLAIM_VERDICTS.get(c['verdict'], c['verdict'])} · {c['text']}")
                if c.get("true_today") and c["verdict"] in ("outdated", "contradicted"):
                    st.caption(f"True today: {c['true_today']}")
                for s in c["sources"][:2]:
                    st.caption(
                        f"[{s.get('title') or s['url']}]({s['url']}) · {s.get('published_date') or 'undated'}"
                    )

# ------------------------------------------------------------------------------------ traffic
st.markdown("### Guarded traffic")
stats, err = api.get("/guard/stats")
if not show_error(err) and stats and stats["models"]:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Model": m["model"],
                    "Checked": m["checked"],
                    "Stale or wrong": m["flagged"],
                    "Flagged": pct(m["flagged_rate"]),
                    "Corrected": m["actions"].get("corrected", 0),
                    "Blocked": m["actions"].get("blocked", 0),
                }
                for m in stats["models"]
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
    recent, err = api.get("/freshness/checks", params={"source": "guard", "limit": 30})
    if not show_error(err) and recent:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "When (UTC)": c["created_at"][:19].replace("T", " "),
                        "Model": c["model"],
                        "Question": c["question"],
                        "Verdict": ANSWER_VERDICTS.get(c["verdict"], c["verdict"]),
                        "Action": c["guard_action"],
                        "Mode": "mock" if c["mock_mode"] else "live",
                        "Check": f"/{DETECTOR_PAGE}?check={c['id']}",
                    }
                    for c in recent
                ]
            ),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Check": st.column_config.LinkColumn("Check", display_text="open ↗"),
                "Question": st.column_config.TextColumn("Question", width="medium"),
            },
        )
    st.caption("Only answers with time-sensitive facts are checked and listed; others pass straight through.")
else:
    st.caption("No guarded answers yet. Send one above, or point an app at the Guard.")
