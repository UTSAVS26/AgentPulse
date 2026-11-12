"""Phase 3.3: Freshness Guard, an OpenAI-compatible fact-checking proxy, driven with the real OpenAI SDK."""

from __future__ import annotations

import pytest
from openai import OpenAI

from app.clients import mock_llm
from app.clients.llm import LLMError
from app.config import Settings
from app.guard.service import Guard, deterministic_correction, last_user_text, needs_check

STALE = "The current UK Prime Minister is Rishi Sunak, leader of the Conservative Party."
MODEL = "groq/openai/gpt-oss-120b"
QUESTION = [{"role": "user", "content": "Who is the current UK prime minister?"}]


@pytest.fixture()
def sdk(client, monkeypatch):
    """The OpenAI SDK pointed at the Guard; the mock model answers questions on known topics stale."""
    monkeypatch.setattr(
        mock_llm,
        "mock_fact_answer",
        lambda prompt, p, rng: STALE if "prime minister" in prompt.lower() else None,
    )
    return OpenAI(base_url="http://testserver/guard/v1", api_key="anything", http_client=client)


def _extra(obj):
    return obj.model_extra["agentpulse"]


def test_annotate_keeps_the_answer_and_reports_the_stale_claim(sdk, client):
    raw = sdk.chat.completions.with_raw_response.create(model=MODEL, messages=QUESTION)
    resp = raw.parse()
    assert resp.choices[0].message.content == STALE and resp.object == "chat.completion"
    info = _extra(resp)
    assert info["checked"] and info["verdict"] == "stale" and info["action"] == "annotated"
    outdated = [c for c in info["claims"] if c["verdict"] == "outdated"]
    assert outdated and "Burnham" in outdated[0]["true_today"] and outdated[0]["sources"][0]["published_date"]
    assert (
        raw.headers["X-AgentPulse-Verdict"] == "stale" and raw.headers["X-AgentPulse-Action"] == "annotated"
    )
    assert raw.headers["X-AgentPulse-Check-Id"] == str(info["check_id"])
    assert resp.usage.total_tokens > 0

    logged = client.get("/freshness/checks", params={"source": "guard"}).json()
    assert logged[0]["id"] == info["check_id"] and logged[0]["model"] == MODEL
    assert logged[0]["guard_action"] == "annotated"
    assert all(
        c["source"] != "guard" for c in client.get("/freshness/checks", params={"manual_only": True}).json()
    )


def test_correct_rewrites_with_citations_and_keeps_the_original(sdk):
    resp = sdk.chat.completions.create(
        model=MODEL, messages=QUESTION, extra_headers={"X-AgentPulse-Guard": "correct"}
    )
    text, info = resp.choices[0].message.content, _extra(resp)
    assert info["action"] == "corrected" and info["original"] == STALE
    assert "Burnham" in text and "[1]" in text and "Sources:" in text and "Rishi Sunak" not in text


def test_block_replaces_the_answer_with_a_warning(sdk):
    resp = sdk.chat.completions.create(model=MODEL, messages=QUESTION, extra_body={"guard": "block"})
    text = resp.choices[0].message.content
    assert text.startswith("⚠️ AgentPulse Freshness Guard withheld this answer") and "Rishi Sunak" in text
    assert _extra(resp)["action"] == "blocked" and _extra(resp)["original"] == STALE


def test_timeless_answers_and_guard_off_are_not_checked(sdk, client):
    before = len(client.get("/freshness/checks", params={"source": "guard"}).json())
    resp = sdk.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "Write a haiku about the sea"}]
    )
    assert _extra(resp) == {
        "guard_mode": "annotate",
        "checked": False,
        "reason": "nothing time-sensitive",
        "mode": "mock",
        "caller": "anonymous",
    }
    off = sdk.chat.completions.create(
        model=MODEL, messages=QUESTION, extra_headers={"X-AgentPulse-Guard": "off"}
    )
    assert off.choices[0].message.content == STALE and _extra(off)["reason"] == "guard off"
    assert len(client.get("/freshness/checks", params={"source": "guard"}).json()) == before


def test_streaming_delivers_the_guarded_answer_and_the_report(sdk):
    chunks = list(
        sdk.chat.completions.create(
            model=MODEL, messages=QUESTION, stream=True, extra_body={"guard": "block"}
        )
    )
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text.startswith("⚠️ AgentPulse Freshness Guard withheld")
    assert chunks[-1].choices[0].finish_reason == "stop" and _extra(chunks[-1])["action"] == "blocked"


def test_tool_calls_pass_through_unchecked(sdk):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "math",
                "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}},
            },
        }
    ]
    for _ in range(5):  # the mock decides to use the tool most of the time
        resp = sdk.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": "What is 17 * 23?"}], tools=tools
        )
        if resp.choices[0].message.tool_calls:
            break
    call = resp.choices[0].message.tool_calls[0]
    assert call.function.name == "calculator" and "17" in call.function.arguments
    assert _extra(resp)["checked"] is False and resp.choices[0].finish_reason == "tool_calls"


def test_openai_style_errors(client, sdk):
    from openai import BadRequestError

    for body, code in (
        ({"messages": QUESTION}, "missing_model"),
        ({"model": MODEL, "messages": []}, "missing_messages"),
        ({"model": MODEL, "messages": QUESTION, "guard": "maybe"}, "bad_guard_mode"),
        ({"model": MODEL, "messages": QUESTION, "n": 2}, "unsupported_n"),
    ):
        res = client.post("/guard/v1/chat/completions", json=body)
        assert res.status_code == 400 and res.json()["error"]["code"] == code, res.json()
    with pytest.raises(BadRequestError):
        sdk.chat.completions.create(model=MODEL, messages=[], extra_body={})
    assert client.post("/guard/v1/chat/completions", content=b"not json").status_code == 400


def test_models_and_stats(sdk, client):
    assert MODEL in [m.id for m in sdk.models.list()]
    sdk.chat.completions.create(model=MODEL, messages=QUESTION)
    stats = client.get("/guard/stats").json()
    row = next(m for m in stats["models"] if m["model"] == MODEL)
    assert row["checked"] >= 1 and row["flagged"] >= 1 and 0 < row["flagged_rate"] <= 1


def test_correction_falls_back_to_a_note_when_no_model_answers():
    class Dead:
        def chat(self, *a, **k):
            raise LLMError("no credit", retryable=False, status_code=402)

    found = [
        {
            "claim": STALE,
            "verdict": "outdated",
            "true_today": "Andy Burnham is Prime Minister.",
            "sources": [{"title": "New PM", "url": "https://x.test/pm", "published_date": "2026-07-24"}],
        }
    ]
    text, how = Guard(Dead(), None, Settings(mock_mode=True), live=False)._correct("Who is PM?", STALE, found)
    assert how.startswith("correction note appended") and text == deterministic_correction(STALE, found)
    assert (
        "True today: Andy Burnham is Prime Minister. [1]" in text
        and "[1] New PM (2026-07-24) https://x.test/pm" in text
    )


def test_helpers():
    multi = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Who is"}, {"type": "text", "text": "the CEO?"}],
        }
    ]
    assert (
        last_user_text(multi) == "Who is\nthe CEO?"
        and last_user_text([{"role": "system", "content": "x"}]) is None
    )
    assert needs_check("Paris.", "What is the current population of Paris?")
    assert not needs_check("Roses are red.", "Write a poem")
