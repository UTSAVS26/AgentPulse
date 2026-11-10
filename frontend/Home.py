"""AgentPulse Streamlit dashboard: home page: what the product is, how to use it, what every page does."""

import streamlit as st

import api
from ui import nav_link, page_setup, pct, run_label, secs

health = page_setup("Home")

st.title("📈 AgentPulse")
st.subheader("Which AI agent can you trust, and is its answer still true today?")
st.markdown(
    "AgentPulse gives several AI **agents** the **same tasks**, scores their answers, and shows you which "
    "one is "
    "the most **accurate, reliable, fast and cheap**. It also checks whether an answer is **out of date**, "
    "by comparing it with today's news on the web."
)

# ------------------------------------------------------------------------------------ mode
if health:
    if health.get("mock_mode"):
        st.info(
            "🧪 **You're in mock mode.** The AI models and web search are *simulated*. Everything works and "
            "costs "
            "nothing, so it's safe to click around. Results are realistic, but not from real models."
            + (
                " To use real models, unlock **live mode** in the sidebar."
                if health.get("live_available")
                else ""
            )
        )
    else:
        st.success(
            "🟢 **You're in live mode.** Agents call real AI models through the **Bifrost** gateway, and "
            "answers "
            "are checked against live web search (**Tavily**)."
        )

# ------------------------------------------------------------------------------------ how to use it
# cards in one row get the same height (only inside the keyed containers below)
st.markdown(
    "<style>"
    + ", ".join(
        f".st-key-{key} [data-testid='stColumn'] > div{tail}"
        for key in ("home_steps", "home_pages")
        for tail in (
            "",
            " > div",
            " > div > [data-testid='stVerticalBlock']",
            " > div > [data-testid='stVerticalBlock'] > [data-testid='stVerticalBlockBorderWrapper']",
        )
    )
    + " { height: 100%; }</style>",
    unsafe_allow_html=True,
)
st.markdown("### How to use it")
steps = [
    ("🔌", "Pick models", "Use the built-in ones, or add your own on **Models**."),
    ("🤖", "Make agents", "An agent = a model + instructions + tools, on **Agents**."),
    ("🧪", "Choose tasks", "Use a ready-made test set or write your own on **Suites**."),
    ("🏁", "Run a benchmark", "Every agent answers every task, a few times, on **Run Benchmark**."),
    ("📊", "Read the results", "Who won and why: **Compare**, **Traces**, **Reports**."),
    ("🕰️", "Check freshness", "Is an answer outdated? **Stale-Answer Detector**."),
]
with st.container(key="home_steps"):
    cols = st.columns(len(steps))
    for i, (col, (icon, title, text)) in enumerate(zip(cols, steps, strict=True), start=1):
        with col.container(border=True):
            st.markdown(f"**{i}. {icon} {title}**")
            st.caption(text)
st.caption(
    "New here? Everything already has example data: open **Compare** to see a finished benchmark right away."
)

# ------------------------------------------------------------------------------------ every page
st.markdown("### What each page does")
PAGES = [
    (
        "pages/1_Models.py",
        "🔌",
        "Models",
        "Connect AI models: your own (a laptop model like Ollama or LM Studio, or a provider like OpenAI or "
        "Gemini with your API key), on top of the built-in ones. Test that a model answers and can use "
        "tools.",
        "To compare *your* models, not just ours. Keys are stored encrypted and never shown again.",
    ),
    (
        "pages/2_Agents.py",
        "🤖",
        "Agents",
        "Create agents: pick a model, write its instructions, give it tools (web search, calculator, today's "
        "date). Try one on a single question and watch every step it takes.",
        "The same model behaves differently with different instructions and tools. Agents let you test that.",
    ),
    (
        "pages/3_Suites.py",
        "🧪",
        "Suites",
        "Test sets: questions plus the correct answer and how to grade it. Use the built-in ones or write "
        "your own in a table, import or export CSV/YAML, and check a grading rule on a sample answer.",
        "A benchmark is only as good as its questions. Build ones that match *your* use case.",
    ),
    (
        "pages/4_Run_Benchmark.py",
        "🏁",
        "Run Benchmark",
        "Pick a suite and some agents, and how many times to repeat. You can also make tools fail on purpose "
        "(timeouts, errors). Watch the results come in live.",
        "Repeats show whether an agent is *consistent*; injected failures show whether it *recovers*.",
    ),
    (
        "pages/5_Compare.py",
        "📊",
        "Compare",
        "The scoreboard: the most reliable, most accurate, fastest and cheapest agent, plus charts for "
        "speed and cost, a per-question breakdown, failures, and whose answers were out of date.",
        "To pick the right agent with evidence, not gut feeling.",
    ),
    (
        "pages/6_Traces.py",
        "🔍",
        "Traces",
        "Replay any single answer step by step: each model call, tool call, failure and retry, with timing "
        "and tokens on a timeline.",
        "To understand *why* an agent got something right or wrong, or why it was slow.",
    ),
    (
        "pages/7_Stale_Answer_Detector.py",
        "🕰️",
        "Stale-Answer Detector",
        'Paste any AI answer. It finds the facts that can change over time ("the current Prime Minister…"), '
        "checks each against today's web results with dates, and tells you which are outdated, and what's "
        "true now.",
        "AI models learn from old data and confidently repeat facts that are no longer true.",
    ),
    (
        "pages/8_Freshness_Bench.py",
        "📡",
        "Freshness Bench",
        "A test written from today's news: questions about events from this week back to two years ago, each "
        "with its source. Run agents on it and see how their accuracy drops for newer events.",
        "It can't be memorised, and it shows each model's real *knowledge cutoff*: the point after which it "
        "stops knowing what happened, unless it searches the web.",
    ),
    (
        "pages/9_Freshness_Guard.py",
        "🛡️",
        "Freshness Guard",
        "Put AgentPulse between your app and its AI model. It checks every answer's facts against today's "
        "news and flags, corrects or blocks the outdated ones. Apps connect by changing one setting (the API "
        "address), because it speaks the standard OpenAI API.",
        "So the people using your app never get yesterday's facts presented as today's.",
    ),
    (
        "pages/10_Reports.py",
        "📄",
        "Reports",
        "Download a benchmark as a readable report (Markdown), a spreadsheet (CSV) or raw data (JSON), with "
        "the same numbers as Compare.",
        "To share results with your team or keep a record.",
    ),
]
with st.container(key="home_pages"):
    for row_start in range(0, len(PAGES), 2):
        cols = st.columns(2)
        for col, (path, icon, name, what, why) in zip(cols, PAGES[row_start : row_start + 2], strict=False):
            with col.container(border=True):
                st.markdown(f"#### {icon} {name}")
                st.markdown(f"**What it does:** {what}")
                st.markdown(f"**Why:** {why}")
                nav_link(st, path, f"Open {name}", icon)

# ------------------------------------------------------------------------------------ glossary
with st.expander("📖 The words used in this app"):
    st.markdown(
        """
| Word | Meaning |
|---|---|
| **Model** | An AI language model (e.g. GPT-OSS, Llama, Gemini). The "brain". |
| **Agent** | A model + instructions + tools + a limit on steps. The thing being tested. |
| **Tool** | Something an agent can use: web search, a calculator, today's date. |
| **Task / Suite** | One question with its correct answer / a set of tasks. |
| **Run (benchmark)** | Every chosen agent answers every task in a suite, possibly several times. |
| **Accuracy** | Share of answers graded correct. |
| **Consistency** | Does the agent give the same right/wrong result every time it repeats a task? |
| **Recovery** | When something failed (a tool timed out, an error), did the agent still get it right? |
| **Reliability score** | One number combining accuracy, consistency, recovery and stability (0–100%). |
| **p50 / p95 latency** | Typical / slowest-5% time to answer. |
| **Stale answer** | Was true once but isn't any more (e.g. a former office holder). |
| **Freshness** | Share of an answer's time-sensitive facts that are still current. |
| **95% CI / Tier** | The range a score is likely in / agents whose gap could be luck share a tier. |
| **Jury** | Several judge models from different AI labs vote on whether an answer is right. |
| **Freshness Bench** | Questions about real news events of different ages, made fresh each day. |
| **Freshness Guard** | A checkpoint between an app and its model that fact-checks answers for users. |
| **Knowledge cutoff** | Roughly when a model stopped learning: later events need a web search. |
| **Mock / live mode** | Simulated models and search (free, offline) / real ones. |
| **Bifrost / Tavily** | The gateway every model call goes through / the live web search for fact checks. |
"""
    )

# ------------------------------------------------------------------------------------ latest results
runs, err = api.get("/runs", params={"limit": 20})
finished = [r for r in runs or [] if r["status"] == "done"]
if finished:
    latest = finished[0]
    data, err = api.get(f"/runs/{latest['id']}/metrics", timeout=30)
    if data and not err:
        st.markdown("### Latest results")
        st.caption(run_label(latest))
        board = [r for r in data["leaderboard"] if r["executions"]][:4]
        cols = st.columns(len(board))
        for col, r in zip(cols, board, strict=True):
            with col.container(border=True):
                st.markdown(f"**#{r['rank']} {r['agent_name']}**")
                st.caption(f"`{r['model']}`")
                st.markdown(
                    f"Reliability **{pct(r['reliability_score'])}** · accuracy {pct(r['accuracy'])}  \n"
                    f"p50 {secs(r['latency_ms_p50'])}"
                    + (f" · freshness {pct(r['freshness'])}" if r.get("freshness_checked") else "")
                )
        nav_link(st, "pages/5_Compare.py", "See the full comparison", "📊")

st.divider()
st.caption(
    "Built with **Bifrost** (one gateway for every AI model) and **Tavily** (live web search). "
    "Docs: see the project README."
)
