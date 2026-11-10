import json

import httpx
import pytest

from app.clients.llm import BifrostClient, LLMError, parse_completion, parse_tool_arguments
from app.clients.mock_llm import KNOWN_PROFILES, MockLLMClient, ModelProfile, profile_for

TOOLS = [
    {"type": "function", "function": {"name": n, "parameters": {"type": "object"}}}
    for n in ("web_search", "calculator", "current_date")
]
MODEL = "groq/openai/gpt-oss-20b"


# ---------------------------------------------------------------- response parsing
def test_parse_completion_with_tool_calls_and_usage():
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'}}
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "extra_fields": {"provider": "groq"},
    }
    r = parse_completion(body, MODEL, 12.5)
    assert r.content == ""
    assert r.tool_calls[0].name == "calculator"
    assert r.tool_calls[0].arguments == {"expression": "2+2"}
    assert (r.prompt_tokens, r.completion_tokens, r.total_tokens) == (10, 5, 15)
    assert r.provider == "groq" and r.latency_ms == 12.5


def test_malformed_tool_arguments_are_preserved_not_raised():
    assert parse_tool_arguments("{not json") == {"_raw": "{not json"}
    assert parse_tool_arguments("") == {}
    assert parse_tool_arguments("[1,2]") == {"_value": [1, 2]}


def test_empty_choices_is_retryable_error():
    with pytest.raises(LLMError) as exc:
        parse_completion({"choices": []}, MODEL, 1)
    assert exc.value.retryable


# ---------------------------------------------------------------- Bifrost HTTP error mapping
def _bifrost(handler) -> BifrostClient:
    return BifrostClient("http://bifrost:8080", transport=httpx.MockTransport(handler))


def test_bifrost_success_sends_openai_payload():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}], "usage": {}},
        )

    r = _bifrost(handler).chat(MODEL, [{"role": "user", "content": "ping"}], tools=TOOLS)
    assert r.content == "pong"
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["model"] == MODEL and seen["body"]["tool_choice"] == "auto"


@pytest.mark.parametrize(
    ("status", "retryable", "kind"),
    [(429, True, "rate_limited"), (503, True, "provider_error"), (400, False, "provider_error")],
)
def test_bifrost_http_errors_are_classified(status, retryable, kind):
    client = _bifrost(lambda _: httpx.Response(status, json={"error": {"message": "boom"}}))
    with pytest.raises(LLMError) as exc:
        client.chat(MODEL, [{"role": "user", "content": "x"}])
    assert exc.value.retryable is retryable
    assert exc.value.kind == kind
    assert exc.value.status_code == status
    assert exc.value.message == "boom"


def test_bifrost_timeout_and_connection_errors_are_retryable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, kind in ((timeout, "timeout"), (refused, "connection")):
        with pytest.raises(LLMError) as exc:
            _bifrost(handler).chat(MODEL, [{"role": "user", "content": "x"}])
        assert exc.value.retryable and exc.value.kind == kind


def test_bifrost_ping_and_list_models():
    def handler(request: httpx.Request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": [{"id": "ollama/granite4:3b"}, {"id": "groq/a"}]})

    client = _bifrost(handler)
    assert client.ping() == (True, "ok")
    assert client.list_models() == ["groq/a", "ollama/granite4:3b"]


# ---------------------------------------------------------------- mock LLM behaviour
def _mock(**kw) -> MockLLMClient:
    return MockLLMClient(seed=1, latency_scale=0, failure_rate_override=0.0, **kw)


def test_mock_is_deterministic_for_same_seed():
    msgs = [{"role": "user", "content": "Tell me something."}]
    a, b = _mock().chat(MODEL, msgs), _mock().chat(MODEL, msgs)
    assert (a.content, a.completion_tokens, a.latency_ms) == (b.content, b.completion_tokens, b.latency_ms)


@pytest.mark.parametrize(
    ("prompt", "tool", "args"),
    [
        ("What is 17 * 23 + 4?", "calculator", {"expression": "17 * 23 + 4"}),
        ("What is today's date?", "current_date", {}),
        ("Who won the 2026 World Cup?", "web_search", {"query": "Who won the 2026 World Cup?"}),
    ],
)
def test_mock_chooses_appropriate_tool(monkeypatch, prompt, tool, args):
    monkeypatch.setitem(KNOWN_PROFILES, MODEL, ModelProfile(0.9, 100, 0, 1, 0, 1.0, 1.0))
    r = _mock().chat(MODEL, [{"role": "user", "content": prompt}], tools=TOOLS)
    assert r.finish_reason == "tool_calls"
    assert r.tool_calls[0].name == tool and r.tool_calls[0].arguments == args


def test_mock_retries_tool_after_error_when_recovery_is_certain(monkeypatch):
    monkeypatch.setitem(KNOWN_PROFILES, MODEL, ModelProfile(0.9, 100, 0, 1, 0, 1.0, 1.0))
    msgs = [
        {"role": "user", "content": "Who is the UK prime minister?"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_0"}]},
        {"role": "tool", "tool_call_id": "call_0", "content": "ERROR: tool timed out (injected fault)"},
    ]
    r = _mock().chat(MODEL, msgs, tools=TOOLS)
    assert r.tool_calls and r.tool_calls[0].name == "web_search"


def test_mock_answers_from_answer_key_and_accuracy_drives_correctness(monkeypatch):
    prompt = "What is the capital of France?"
    monkeypatch.setitem(KNOWN_PROFILES, "perfect/model", ModelProfile(1.0, 10, 0, 1, 0, 0, 0))
    monkeypatch.setitem(KNOWN_PROFILES, "hopeless/model", ModelProfile(0.0, 10, 0, 1, 0, 0, 0))
    client = _mock(answer_key={prompt: "Paris"})
    msgs = [{"role": "user", "content": prompt}]
    assert "Paris" in client.chat("perfect/model", msgs).content
    assert "Paris" not in client.chat("hopeless/model", msgs).content


def test_mock_repeats_vary_but_are_reproducible():
    msgs = [{"role": "user", "content": "What is 1234 + 4321?"}]
    key = {"What is 1234 + 4321?": "5555"}
    first = [_mock(answer_key=key).chat("ollama/granite4:3b", msgs).content for _ in range(1)]
    client = _mock(answer_key=key)
    runs = [client.chat("ollama/granite4:3b", msgs).content for _ in range(20)]
    assert len(set(runs)) > 1, "a 58%-accurate model should not give identical answers 20 times"
    assert runs[0] == first[0]


def test_mock_simulated_failure_is_retryable_llm_error():
    client = MockLLMClient(seed=1, latency_scale=0, failure_rate_override=1.0)
    with pytest.raises(LLMError) as exc:
        client.chat(MODEL, [{"role": "user", "content": "x"}])
    assert exc.value.retryable and exc.value.status_code == 503


def test_mock_responders_take_priority():
    client = _mock(responders=[lambda m, msgs, rng: "CANNED" if "extract" in msgs[0]["content"] else None])
    assert client.chat(MODEL, [{"role": "user", "content": "extract claims"}]).content == "CANNED"
    assert client.chat(MODEL, [{"role": "user", "content": "hello"}]).content != "CANNED"


def test_unknown_models_get_stable_profiles():
    assert profile_for("someprovider/new-model") == profile_for("someprovider/new-model")
    assert 0.55 <= profile_for("someprovider/new-model").accuracy <= 0.9


def test_bifrost_400_tool_validation_is_classified_as_invalid_tool_call():
    msg = "tool call validation failed: parameters for tool calculator did not match schema"
    client = _bifrost(lambda _: httpx.Response(400, json={"error": {"message": msg}}))
    with pytest.raises(LLMError) as exc:
        client.chat(MODEL, [{"role": "user", "content": "x"}])
    assert exc.value.kind == "invalid_tool_call" and exc.value.retryable


def test_provider_errors_are_redacted_of_account_ids():
    from app.clients.llm import LLMError

    exc = LLMError(
        "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_01abcDEF` "
        "service tier on_demand",
        retryable=True,
        kind="rate_limited",
    )
    assert "org_" not in exc.message and "org_" not in str(exc)
    assert exc.message == "Rate limit reached for model `openai/gpt-oss-20b` service tier on_demand"


def test_connection_errors_are_described_plainly():
    """A stopped local server must read as 'not running', not as Docker's IPv6 'Network is unreachable'."""
    import httpx

    from app.clients.llm import describe_http_error

    for raw in ("[Errno 101] Network is unreachable", "[Errno 111] Connection refused"):
        assert "Is the server running" in describe_http_error(httpx.ConnectError(raw))
    assert "unknown host" in describe_http_error(httpx.ConnectError("[Errno -2] Name or service not known"))
    assert describe_http_error(httpx.ReadError("boom")) == "boom"


def _ok_body(text="ok"):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }


def test_low_balance_402_retries_once_within_the_affordable_budget():
    """OpenRouter reserves credit for the whole max_tokens; with a low balance it names what it can afford."""
    asked = []

    def handler(request):
        budget = json.loads(request.content).get("max_tokens")
        asked.append(budget)
        if budget > 1124:
            msg = (
                "This request requires more credits, or fewer max_tokens. "
                f"You requested up to {budget} tokens, but can only afford 1124."
            )
            return httpx.Response(402, json={"error": {"message": msg, "code": 402}})
        return httpx.Response(200, json=_ok_body("PASS"))

    res = _bifrost(handler).chat(MODEL, [{"role": "user", "content": "grade"}], max_tokens=1500)
    assert res.content == "PASS" and asked == [1500, 1011]


def test_402_without_an_affordable_budget_is_not_retried():
    asked = []

    def handler(request):
        asked.append(1)
        msg = "You requested up to 300 tokens, but can only afford 40."
        return httpx.Response(402, json={"error": {"message": msg}})

    with pytest.raises(LLMError) as exc:
        _bifrost(handler).chat(MODEL, [{"role": "user", "content": "x"}], max_tokens=300)
    assert exc.value.status_code == 402 and len(asked) == 1

    asked.clear()

    def plain(request):
        asked.append(1)
        return httpx.Response(402, json={"error": {"message": "Payment required"}})

    with pytest.raises(LLMError):
        _bifrost(plain).chat(MODEL, [{"role": "user", "content": "x"}], max_tokens=300)
    assert len(asked) == 1
