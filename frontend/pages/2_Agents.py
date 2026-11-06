"""Agents: list, create, edit, delete and try an agent with optional fault injection."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import api
from ui import execution_summary, page_setup, render_trace, route_badge, show_error

page_setup("Agents", "🤖")
st.title("🤖 Agents")
st.caption(
    "An agent = a model (built into Bifrost, or one of **your own** from the Models page) + a system "
    "prompt + a set of tools + a step budget. Benchmarks run every agent on identical tasks."
)

CUSTOM_MODEL = "✏️ Custom model id…"


@st.cache_data(ttl=300, show_spinner=False)
def load_models() -> tuple[list[str], str | None]:
    data, err = api.get("/models", timeout=40)
    return (data or {}).get("models", []), err


@st.cache_data(ttl=3600, show_spinner=False)
def load_tools() -> list[dict]:
    data, _ = api.get("/tools")
    return data or []


agents, err = api.get("/agents")
if show_error(err):
    st.stop()
models, models_err = load_models()
connections, _ = api.get("/connections")
CONN_MODELS = {
    mid: c for c in connections or [] for mid in c["model_ids"]
}  # custom/<slug>/<model> -> its connection
tools_catalogue = load_tools()
tool_names = [t["name"] for t in tools_catalogue]

# ------------------------------------------------------------------------------------ list
if agents:
    table = pd.DataFrame(
        [
            {
                "ID": a["id"],
                "Name": a["name"],
                "Route": route_badge(a.get("route"), a.get("connection")),
                "Model": a["model"],
                "Tools": ", ".join(a["tools"]) or "none",
                "Max steps": a["max_steps"],
                "Temp": a["temperature"],
            }
            for a in agents
        ]
    )
    st.dataframe(table, hide_index=True, use_container_width=True)
else:
    st.info("No agents yet. Create one in the **Create** tab.")

by_label = {f"{a['name']}  ·  {a['model']}": a for a in agents}

tab_try, tab_create, tab_edit = st.tabs(["▶️ Try an agent", "➕ Create", "✏️ Edit / delete"])


def _model_label(model: str) -> str:
    conn = CONN_MODELS.get(model)
    if conn is None:
        return model
    return f"🔌 {conn['name']} · {model.split('/', 2)[2]}"


def agent_form(key: str, initial: dict | None = None) -> dict | None:
    """Render the agent form; returns the submitted payload or None."""
    initial = initial or {}
    if models_err:
        st.warning(f"Could not load model list ({models_err}). Enter a model id manually.")
    model_options = [*CONN_MODELS, *(m for m in models if m not in CONN_MODELS)]
    if initial.get("model") and initial["model"] not in model_options:
        model_options.insert(0, initial["model"])
    model_options.append(CUSTOM_MODEL)

    with st.form(key, clear_on_submit=False):
        name = st.text_input("Name", value=initial.get("name", ""))
        description = st.text_input("Description", value=initial.get("description", ""))
        default_idx = model_options.index(initial["model"]) if initial.get("model") in model_options else 0
        model_choice = st.selectbox(
            "Model",
            model_options,
            index=default_idx,
            format_func=_model_label,
            help="🔌 = one of your model connections (Models page); others are routed by Bifrost. "
            "Type to search.",
        )
        custom_model = st.text_input(
            "Custom model id (used when 'Custom model id…' is selected)",
            placeholder="provider/model, e.g. groq/openai/gpt-oss-20b",
        )
        selected_tools = st.multiselect(
            "Tools",
            tool_names,
            default=[t for t in initial.get("tools", []) if t in tool_names],
            help="; ".join(f"{t['name']}: {t['description'][:80]}" for t in tools_catalogue),
        )
        system_prompt = st.text_area("System prompt", value=initial.get("system_prompt", ""), height=120)
        c1, c2 = st.columns(2)
        temperature = c1.slider("Temperature", 0.0, 2.0, float(initial.get("temperature", 0.0)), 0.1)
        max_steps = c2.number_input("Max model calls", 1, 20, int(initial.get("max_steps", 6)))
        submitted = st.form_submit_button("Save", type="primary")

    if not submitted:
        return None
    model = custom_model.strip() if model_choice == CUSTOM_MODEL else model_choice
    if not name.strip() or not model:
        st.error("Name and model are required.")
        return None
    conn = CONN_MODELS.get(model)
    if conn and selected_tools and conn["supports_tools"] is False:
        st.warning(
            f"⚠️ {conn['name']} failed the tool-calling test: this agent's tools may not be used. "
            "Consider an agent without tools for this model."
        )
    return {
        "name": name.strip(),
        "description": description,
        "model": model,
        "tools": selected_tools,
        "system_prompt": system_prompt,
        "temperature": temperature,
        "max_steps": int(max_steps),
    }


# ------------------------------------------------------------------------------------ try
with tab_try:
    if not agents:
        st.info("Create an agent first.")
    else:
        label = st.selectbox("Agent", list(by_label), key="try_agent")
        agent = by_label[label]
        examples = {
            "Arithmetic (calculator)": "What is 17 * 23 + 4?",
            "Fresh fact (web search)": "Who is the current Prime Minister of the United Kingdom?",
            "Date (current_date)": "What day of the week is it today?",
            "Custom": "",
        }
        example = st.radio("Example prompt", list(examples), horizontal=True)
        prompt = st.text_area("Prompt", value=examples[example], height=90)
        c1, c2 = st.columns([3, 1])
        fault_rate = c1.slider(
            "Fault injection rate",
            0.0,
            1.0,
            0.0,
            0.05,
            help="Probability that each tool call is replaced by a timeout, HTTP 500, empty or malformed "
            "response. Tests whether the agent recovers.",
        )
        seed = c2.number_input("Seed", 0, 1_000_000, 0, help="Same seed reproduces the same injected faults.")
        if st.button("Run agent", type="primary", disabled=not prompt.strip()):
            with st.spinner(f"Running {agent['name']} on {agent['model']}…"):
                ex, err = api.post(
                    f"/agents/{agent['id']}/try",
                    json={"prompt": prompt, "fault_rate": fault_rate, "seed": int(seed)},
                    timeout=api.LONG_TIMEOUT,
                )
            if not show_error(err):
                st.session_state["last_try"] = ex

        ex = st.session_state.get("last_try")
        if ex and ex["agent_id"] == agent["id"]:
            st.divider()
            st.markdown(f"#### Answer · execution #{ex['id']}")
            st.markdown(ex["output"] or "_(no answer)_")
            execution_summary(ex)
            st.markdown("#### Trace")
            render_trace(ex["trace"])

# ------------------------------------------------------------------------------------ create
with tab_create:
    prefill = {}
    if st.session_state.get("prefill_agent_model"):
        prefill = {
            "model": st.session_state["prefill_agent_model"],
            "name": st.session_state.get("prefill_agent_name", ""),
        }
        st.info("Model pre-selected from the **Models** page: the ➕ Create tab has it ready.")
    payload = agent_form("create_form", prefill or None)
    if payload:
        created, err = api.post("/agents", json=payload)
        if not show_error(err):
            st.success(f"Created agent **{created['name']}** (id {created['id']}).")
            st.session_state.pop("prefill_agent_model", None)
            st.session_state.pop("prefill_agent_name", None)
            st.rerun()

# ------------------------------------------------------------------------------------ edit / delete
with tab_edit:
    if not agents:
        st.info("No agents to edit.")
    else:
        label = st.selectbox("Agent to edit", list(by_label), key="edit_agent")
        agent = by_label[label]
        payload = agent_form(f"edit_form_{agent['id']}", agent)
        if payload:
            _, err = api.put(f"/agents/{agent['id']}", json=payload)
            if not show_error(err):
                st.success("Saved.")
                st.rerun()

        st.divider()
        confirm = st.checkbox(f"I want to delete **{agent['name']}** and all its executions")
        if st.button("Delete agent", disabled=not confirm):
            _, err = api.delete(f"/agents/{agent['id']}")
            if not show_error(err):
                st.success("Deleted.")
                st.rerun()
