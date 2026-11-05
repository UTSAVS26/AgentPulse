"""Agent runtime behaviour: normal runs, step cap, fault injection/recovery, retries, persistence."""

import random

import pytest
from sqlalchemy.orm import Session

from app.agents.runtime import AgentSpec, RuntimeConfig, run_agent, save_execution
from app.clients.llm import LLMError, LLMResponse, ToolCall
from app.clients.mock_llm import KNOWN_PROFILES, MockLLMClient, ModelProfile
from app.clients.mock_search import MockSearchClient
from app.models import Agent, TaskExecution

MODEL = "groq/openai/gpt-oss-20b"
TOOLS = ["web_search", "calculator", "current_date"]
NO_SLEEP = RuntimeConfig(sleep_scale=0)


def spec(**kw) -> AgentSpec:
    return AgentSpec(name="t", model=MODEL, tools=TOOLS, **kw)


@pytest.fixture()
def search():
    return MockSearchClient(latency_scale=0)


@pytest.fixture()
def reliable_profile(monkeypatch):
    """Model always uses tools, always recovers, never fails."""
    monkeypatch.setitem(KNOWN_PROFILES, MODEL, ModelProfile(1.0, 100, 0, 1, 0.0, 1.0, 1.0))


class ScriptedLLM:
    """Returns a scripted sequence of responses / exceptions, recording every call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, model, messages, tools=None, temperature=0.0, max_tokens=None):
        self.calls.append([dict(m) for m in messages])
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def list_models(self):
        return [MODEL]

    def ping(self):
        return True, "scripted"


def tool_reply(name="calculator", args=None, call_id="c1"):
    args = args if args is not None else {"expression": "2+2"}
    return LLMResponse(
        content="", tool_calls=[ToolCall(call_id, name, args)], prompt_tokens=10, completion_tokens=5
    )


def final(text="The answer is 4."):
    return LLMResponse(content=text, prompt_tokens=20, completion_tokens=7, latency_ms=100)


# ------------------------------------------------------------------------------------ normal runs
def test_tool_then_answer_with_mock_llm(reliable_profile, search):
    llm = MockLLMClient(seed=3, latency_scale=0, answer_key={"What is 17 * 23 + 4?": "395"})
    r = run_agent(spec(), "What is 17 * 23 + 4?", llm, search, NO_SLEEP)
    assert r.status == "ok"
    assert "395" in r.output
    assert [s.type for s in r.steps] == ["llm_call", "tool_call", "tool_result", "llm_call", "final"]
    assert r.steps[2].payload == {
        "id": "call_0",
        "name": "calculator",
        "ok": True,
        "content": "17 * 23 + 4 = 395",
    }
    assert r.llm_calls == 2 and r.tool_calls == 1
    assert r.total_tokens > 0 and r.latency_ms > 0
    assert not r.had_failures and not r.recovered


def test_messages_follow_openai_tool_protocol(search):
    llm = ScriptedLLM([tool_reply(), final()])
    run_agent(spec(system_prompt="SYS"), "2+2?", llm, search, NO_SLEEP)
    second_call = llm.calls[1]
    assert [m["role"] for m in second_call] == ["system", "user", "assistant", "tool"]
    assistant, tool = second_call[2], second_call[3]
    assert assistant["tool_calls"][0]["function"] == {
        "name": "calculator",
        "arguments": '{"expression": "2+2"}',
    }
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "2+2 = 4"}


def test_agent_without_tools_answers_directly(search):
    llm = ScriptedLLM([final("Paris.")])
    r = run_agent(AgentSpec(name="b", model=MODEL, tools=[], max_steps=1), "Capital of France?", llm, search)
    assert (r.status, r.output, r.llm_calls) == ("ok", "Paris.", 1)


# ------------------------------------------------------------------------------------ step cap
def test_max_steps_caps_endless_tool_calls(search):
    llm = ScriptedLLM([tool_reply()])  # always asks for a tool
    r = run_agent(spec(max_steps=3), "loop forever", llm, search, NO_SLEEP)
    assert r.status == "max_steps"
    assert r.llm_calls == 3 and r.tool_calls == 3
    assert r.steps[-1].type == "error" and r.steps[-1].payload["kind"] == "max_steps"


# ------------------------------------------------------------------------------------ fault injection
def test_injected_fault_then_recovery(reliable_profile, search):
    llm = MockLLMClient(seed=3, latency_scale=0, answer_key={"Who is the UK prime minister?": "Andy Burnham"})

    class FirstCallFaults(random.Random):
        """rng.random() < fault_rate only on the first draw; choice() picks 'timeout'."""

        n = 0

        def random(self):
            self.n += 1
            return 0.0 if self.n == 1 else 0.99

        def choice(self, seq):
            return "timeout"

    r = run_agent(
        spec(),
        "Who is the UK prime minister?",
        llm,
        search,
        RuntimeConfig(fault_rate=0.5, sleep_scale=0),
        rng=FirstCallFaults(),
    )
    results = [s for s in r.steps if s.type == "tool_result"]
    assert results[0].payload["fault"] == "timeout" and results[0].payload["ok"] is False
    assert results[0].latency_ms == 5_000.0
    assert results[1].payload["ok"] is True  # agent retried the tool
    assert r.faults_injected == 1 and r.tool_errors == 1
    assert r.status == "ok" and r.recovered


@pytest.mark.parametrize("kind", ["timeout", "http_500", "empty", "malformed"])
def test_every_fault_kind_is_traced(search, kind):
    class Always(random.Random):
        def random(self):
            return 0.0

        def choice(self, seq):
            return kind

    llm = ScriptedLLM([tool_reply(), final()])
    r = run_agent(spec(), "2+2?", llm, search, RuntimeConfig(fault_rate=1.0, sleep_scale=0), rng=Always())
    tr = next(s for s in r.steps if s.type == "tool_result")
    assert tr.payload["fault"] == kind and tr.payload["ok"] is False
    assert r.faults_injected == 1


def test_zero_fault_rate_never_injects(reliable_profile, search):
    llm = MockLLMClient(seed=1, latency_scale=0)
    for i in range(20):
        r = run_agent(spec(), f"What is {i} * 7?", llm, search, NO_SLEEP, rng=random.Random(i))
        assert r.faults_injected == 0


def test_real_tool_error_is_shown_to_model_and_counted(search):
    llm = ScriptedLLM([tool_reply(args={"expression": "1/0"}), final("Cannot divide by zero.")])
    r = run_agent(spec(), "1/0?", llm, search, NO_SLEEP)
    assert llm.calls[1][-1]["content"] == "ERROR: division by zero"
    assert r.tool_errors == 1 and r.recovered


# ------------------------------------------------------------------------------------ LLM retries
def test_retryable_llm_error_is_retried_then_succeeds(search):
    sleeps = []
    llm = ScriptedLLM([LLMError("503", retryable=True, status_code=503), final()])
    r = run_agent(
        spec(), "q", llm, search, RuntimeConfig(backoff_base_ms=500, sleep_scale=1.0), sleep=sleeps.append
    )
    assert r.status == "ok" and r.retries == 1
    assert [s.type for s in r.steps][:2] == ["error", "retry"]
    assert sleeps == [0.5]
    assert r.recovered


def test_retries_exhausted_gives_clean_error_status(search):
    llm = ScriptedLLM([LLMError("timeout", retryable=True, kind="timeout")])
    r = run_agent(spec(), "q", llm, search, RuntimeConfig(max_retries=2, sleep_scale=0))
    assert r.status == "error" and r.output == ""
    assert r.retries == 2
    assert [s.type for s in r.steps] == ["error", "retry", "error", "retry", "error"]
    assert "timeout" in r.error
    assert [s.payload["backoff_ms"] for s in r.steps if s.type == "retry"] == [500, 1000]


def test_non_retryable_error_fails_immediately(search):
    llm = ScriptedLLM([LLMError("model not found", retryable=False, status_code=404)])
    r = run_agent(spec(), "q", llm, search, NO_SLEEP)
    assert r.status == "error" and r.retries == 0 and len(llm.calls) == 1


# ------------------------------------------------------------------------------------ persistence
def test_save_execution_persists_metrics_and_ordered_trace(db_engine, search):
    llm = ScriptedLLM([tool_reply(), final()])
    r = run_agent(spec(), "2+2?", llm, search, NO_SLEEP)
    with Session(db_engine) as db:
        agent = Agent(name="persist", model=MODEL, tools=TOOLS)
        db.add(agent)
        db.flush()
        ex = save_execution(db, r, agent_id=agent.id, prompt="2+2?")
        db.commit()
        loaded = db.get(TaskExecution, ex.id)
        assert loaded.status == "ok" and loaded.output == "The answer is 4."
        assert loaded.steps == 2 and loaded.tool_calls == 1
        assert loaded.prompt_tokens == 30 and loaded.completion_tokens == 12
        assert [t.idx for t in loaded.trace] == list(range(len(r.steps)))
        assert loaded.run_id is None


def test_invalid_tool_call_rejected_by_provider_gets_corrective_retry(search):
    err = LLMError(
        "tool call validation failed: parameters for tool calculator did not match schema",
        retryable=True,
        status_code=400,
        kind="invalid_tool_call",
    )
    llm = ScriptedLLM([err, final("The answer is 4.")])
    r = run_agent(spec(), "2+2?", llm, search, RuntimeConfig(sleep_scale=0))
    assert r.status == "ok" and r.retries == 1 and r.tool_errors == 1 and r.recovered
    notice = llm.calls[1][-1]
    assert notice["role"] == "user" and "rejected" in notice["content"]
    assert [s.type for s in r.steps][:2] == ["error", "retry"]


def test_invalid_tool_call_gives_up_after_max_retries(search):
    err = LLMError("tool call validation failed", retryable=True, status_code=400, kind="invalid_tool_call")
    llm = ScriptedLLM([err])
    r = run_agent(spec(), "2+2?", llm, search, RuntimeConfig(max_retries=2, sleep_scale=0))
    assert r.status == "error" and r.retries == 2
