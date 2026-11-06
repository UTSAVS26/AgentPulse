"""Models: bring your own model. Connect any OpenAI-compatible endpoint (self-hosted or hosted)."""

from __future__ import annotations

import streamlit as st

import api
from ui import nav_link, page_setup, show_error

health = page_setup("Models", "🔌")
st.title("🔌 Models")
st.caption(
    "Agents can use the models built into the Bifrost gateway (Groq, OpenRouter, local Ollama), or "
    "**your own**: any OpenAI-compatible endpoint, self-hosted (Ollama, LM Studio, vLLM, llama.cpp…) or "
    "hosted (OpenAI, Anthropic, Gemini, Mistral, DeepSeek…). Give a base URL, a model name and, if needed, "
    "an API key."
)

public = bool(health and health.get("public_deployment"))
live = bool(health and not health.get("mock_mode"))
if public and not live:
    st.info(
        "🔒 On the public demo, adding and testing your own models needs the access code (sidebar → *Unlock "
        "live mode*), because it makes the server call your URL and store your key. You can still browse."
    )
elif health and health.get("mock_mode"):
    st.info("Mock mode: agents on your models run **simulated**. Use live mode to call the real endpoint.")

presets, err = api.get("/connections/presets")
connections, err2 = api.get("/connections")
if show_error(err or err2):
    st.stop()
by_preset = {p["id"]: p for p in presets}
ROUTABLE = {p["bifrost_provider"] for p in presets if p.get("via_bifrost")}  # native, no built-in key
locked = public and not live


def _models_text(text: str) -> list[str]:
    return [m.strip() for m in text.replace("\n", ",").split(",") if m.strip()]


def _show_test(res: dict) -> None:
    if res.get("hint"):
        st.caption(f"ℹ️ {res['hint']}")
    if res.get("models") is not None:
        st.success(f"✅ Reached `{res['base_url']}`: {len(res['models'])} models listed.")
    elif res.get("models_error"):
        st.warning(
            f"Couldn't list models ({res['models_error']}). Some servers don't support it: type the name."
        )
    if res.get("chat_ok"):
        st.success(f"✅ `{res['model']}` answered in {res['chat_latency_ms']:.0f} ms: “{res['chat_reply']}”")
        if res.get("tools_ok"):
            st.success(f"🛠️ Tool calling works ({res['tools_detail']}).")
        else:
            st.warning(f"⚠️ {res.get('tools_detail') or 'no tool calling'}: use it for agents without tools.")
    elif res.get("chat_error"):
        st.error(f"❌ `{res['model']}`: {res['chat_error']}")
        if any(
            k in res["chat_error"].lower()
            for k in ("404", "not found", "no longer available", "does not exist")
        ):
            st.info(
                "💡 The provider doesn't know this model name (it may be retired). Click **🔍 Fetch models** "
                "and pick one from the list your key can use."
            )


# ------------------------------------------------------------------------------------ add
with st.expander("➕ Add a model connection", expanded=not connections):
    groups = {"local": "Self-hosted / local", "hosted": "Hosted APIs", "custom": "Other"}
    preset_id = st.selectbox(
        "Start from",
        [p["id"] for p in presets],
        format_func=lambda i: f"{groups[by_preset[i]['group']]} · {by_preset[i]['label']}",
        key="new_preset",
    )
    preset = by_preset[preset_id]
    if st.session_state.get("_preset_applied") != preset_id:  # prefill once per preset choice
        st.session_state["_preset_applied"] = preset_id
        st.session_state["new_base_url"] = preset["base_url"]
        st.session_state["new_name"] = preset["label"] if preset_id != "custom" else ""
        st.session_state.pop("new_found_models", None)
    c1, c2 = st.columns(2)
    name = c1.text_input("Name", key="new_name", max_chars=80)
    base_url = c2.text_input(
        "Base URL",
        key="new_base_url",
        help="The API root that contains /chat/completions (usually ends in /v1).",
    )
    api_key = st.text_input(
        "API key" + (" (required)" if preset["needs_key"] else " (optional)"),
        type="password",
        key="new_key",
        help="Stored encrypted and never shown again.",
    )
    via_bifrost = False
    if preset.get("via_bifrost"):
        via_bifrost = st.checkbox(
            "🛰️ Route through the Bifrost gateway (recommended)",
            value=True,
            key=f"new_via_{preset_id}",
            help="Your key is registered in Bifrost at runtime, so calls go through the same gateway as the "
            "built-in models. If that isn't possible here, the connection calls the provider directly and "
            "says why.",
        )
    elif preset.get("bifrost_provider") in ("groq", "openrouter"):
        st.caption(
            f"Called directly: Bifrost already has its own {preset['label'].split(' (')[0]} key, and adding "
            "yours there would mix the two."
        )
    if preset["group"] == "local":
        st.caption(
            "Running locally: `localhost` is fine. Inside Docker it is translated to your machine "
            "(`host.docker.internal`). Make sure the server listens on all interfaces if it isn't reachable."
        )
    found = st.session_state.get("new_found_models") or []
    if st.button("🔍 Fetch models", disabled=locked or not base_url.strip()):
        res, err = api.post(
            "/connections/test", json={"base_url": base_url, "api_key": api_key or None}, timeout=90
        )
        if not show_error(err):
            _show_test(res)
            st.session_state["new_found_models"] = res.get("models") or []
            found = st.session_state["new_found_models"]
    if found:
        picked = st.multiselect("Models to use", found, key="new_models_pick")
    else:
        picked = []
    typed = st.text_input("Model name(s)", key="new_models_typed", placeholder=preset["model_hint"])
    models = list(dict.fromkeys([*picked, *_models_text(typed)]))
    price_in = price_out = 0.0
    if st.toggle(
        "Set a price (optional)", key="new_price_on", help="Leave off for local models: they cost $0."
    ):
        p1, p2 = st.columns(2)
        price_in = p1.number_input("USD per 1M input tokens", min_value=0.0, value=0.0, key="new_pin")
        price_out = p2.number_input("USD per 1M output tokens", min_value=0.0, value=0.0, key="new_pout")
        st.caption("Benchmark costs for this connection's models use these prices.")
    if via_bifrost:
        st.caption(
            "ℹ️ Testing before saving calls the provider **directly**. After you save, the key is registered "
            "in Bifrost and the connection card's **🧪 Test** goes through the gateway."
        )
    b1, b2 = st.columns(2)
    if b1.button("🧪 Test first model", disabled=locked or not (base_url.strip() and models)):
        res, err = api.post(
            "/connections/test",
            json={"base_url": base_url, "api_key": api_key or None, "model": models[0]},
            timeout=120,
        )
        if not show_error(err):
            _show_test(res)
    if b2.button(
        "💾 Save connection", type="primary", disabled=locked or not (name.strip() and base_url.strip())
    ):
        body = {
            "name": name,
            "preset": preset_id,
            "base_url": base_url,
            "api_key": api_key or None,
            "models": models,
            "provider": preset.get("bifrost_provider"),
            "via_bifrost": via_bifrost,
            "price_input_per_m": price_in or None,
            "price_output_per_m": price_out or None,
        }
        created, err = api.post("/connections", json=body)
        if not show_error(err):
            for k in (
                "new_key",
                "new_models_typed",
                "new_found_models",
                "_preset_applied",
                "new_models_pick",
            ):
                st.session_state.pop(k, None)
            st.session_state["created_connection"] = created["name"]
            st.rerun()

if st.session_state.get("created_connection"):
    st.success(
        f"Saved **{st.session_state.pop('created_connection')}**. Test it below, then create an agent."
    )

# ------------------------------------------------------------------------------------ list
st.markdown(f"#### Your connections ({len(connections)})")
if not connections:
    st.caption("None yet. Add one above.")
for c in connections:
    status = "⚪ not tested" if c["last_check_ok"] is None else ("🟢" if c["last_check_ok"] else "🔴")
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        h1.markdown(f"**{c['name']}** · `{c['slug']}` · {status}")
        h1.caption(
            (f"🛰️ via Bifrost ({c['provider']})" if c["route"] == "bifrost" else f"↗️ direct · {c['base_url']}")
            + " · "
            + (f"key {c['key_hint']}" if c["has_api_key"] else "no key")
            + (
                f" · ${c['price_input_per_m']:g} / ${c['price_output_per_m'] or 0:g} per 1M"
                if c["price_input_per_m"]
                else ""
            )
        )
        if c["last_check_detail"]:
            h2.caption(f"Last test: {c['last_check_detail']}")
        if c["route_reason"]:
            h2.caption(("ℹ️ " if c["route"] == "bifrost" else "↗️ ") + c["route_reason"])
        if c["bifrost_key_status"]:
            (h2.caption if "accepted" in c["bifrost_key_status"] else h2.warning)(c["bifrost_key_status"])
        tools = {True: "🛠️ tools ✓", False: "🛠️ tools ✗", None: ""}[c["supports_tools"]]
        st.markdown(
            "Models: "
            + (", ".join(f"`{m}`" for m in c["model_ids"]) or "_none yet: edit to add one_")
            + f"  {tools}"
        )
        if c["agents"]:
            st.caption("Used by: " + ", ".join(c["agents"]))

        t1, t2, t3, t4 = st.columns([2, 1, 1, 1])
        test_model = t1.selectbox(
            "Model", c["models"] or ["(list models only)"], key=f"tm_{c['id']}", label_visibility="collapsed"
        )
        if t2.button("🧪 Test", key=f"test_{c['id']}", disabled=locked):
            body = {"model": test_model} if c["models"] else {}
            res, err = api.post(f"/connections/{c['id']}/test", json=body, timeout=120)
            if not show_error(err):
                _show_test(res)
        if t3.button("🤖 New agent", key=f"agent_{c['id']}", disabled=not c["models"]):
            st.session_state["prefill_agent_model"] = (
                c["model_ids"][c["models"].index(test_model)]
                if (test_model in c["models"])
                else c["model_ids"][0]
            )
            st.session_state["prefill_agent_name"] = f"{c['name']} · {test_model}"
            st.switch_page("pages/1_Agents.py")

        with st.expander("✏️ Edit / delete"):
            with st.form(f"edit_{c['id']}"):
                e_name = st.text_input("Name", value=c["name"])
                e_url = st.text_input("Base URL", value=c["base_url"])
                options = list(dict.fromkeys([*(c["discovered_models"] or []), *c["models"]]))
                e_models = st.multiselect(
                    "Models", options, default=c["models"], help="From the last test's list."
                )
                e_more = st.text_input("Add model names", placeholder="comma-separated")
                e_key = st.text_input("New API key (leave empty to keep)", type="password")
                e_clear = st.checkbox("Remove the stored key", disabled=not c["has_api_key"])
                e_via = False
                if c["provider"] in ROUTABLE:
                    e_via = st.checkbox("🛰️ Route through the Bifrost gateway", value=c["kind"] == "hosted")
                q1, q2 = st.columns(2)
                e_pin = q1.number_input(
                    "USD / 1M input", min_value=0.0, value=float(c["price_input_per_m"] or 0)
                )
                e_pout = q2.number_input(
                    "USD / 1M output", min_value=0.0, value=float(c["price_output_per_m"] or 0)
                )
                if st.form_submit_button("Save", disabled=locked):
                    body = {
                        "name": e_name,
                        "preset": c["preset"],
                        "base_url": e_url,
                        "models": list(dict.fromkeys([*e_models, *_models_text(e_more)])),
                        "api_key": e_key or None,
                        "clear_api_key": e_clear,
                        "provider": c["provider"],
                        "via_bifrost": e_via,
                        "price_input_per_m": e_pin or None,
                        "price_output_per_m": e_pout or None,
                    }
                    _, err = api.put(f"/connections/{c['id']}", json=body)
                    if not show_error(err):
                        st.rerun()
            if c["agents"]:
                st.caption("Delete the agents that use this connection first.")
            elif t4.button("🗑️ Delete", key=f"del_{c['id']}", disabled=locked):
                _, err = api.delete(f"/connections/{c['id']}")
                if not show_error(err):
                    st.rerun()

st.divider()
st.caption(
    "Built-in models (no setup): everything Bifrost routes, e.g. `groq/…`, `openrouter/…`, `ollama/…`."
)
nav_link(st, "pages/1_Agents.py", "Create an agent", "🤖")
