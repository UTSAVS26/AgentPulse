"""The agent runtime: an OpenAI-style tool-calling loop with retries, fault injection and tracing.

``run_agent`` is pure with respect to storage: it returns an :class:`AgentResult` holding the
output, aggregate metrics and an ordered list of :class:`StepRecord`. ``save_execution`` persists
that result. This keeps the loop easy to test and lets the benchmark engine decide when to commit.

Loop
----
1. Send ``[system, user, ...]`` to the model with the agent's tools.
2. If the call fails with a *retryable* :class:`LLMError` (timeout, 429, 5xx), retry with
   exponential backoff up to ``max_retries`` times; every failure and retry is traced. A
   non-retryable error, or running out of retries, ends the execution with ``status="error"``.
3. If the model answered without tool calls, that is the final answer (``status="ok"``).
4. Otherwise run each requested tool and append its result as a ``tool`` message. With
   probability ``fault_rate`` the tool call is replaced by an injected fault (timeout, HTTP 500,
   empty or malformed payload) to measure whether the agent recovers.
5. Stop after ``max_steps`` model calls (``status="max_steps"``).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.agents.tools import ToolContext, ToolError, run_tool, tool_definitions
from app.clients.llm import LLMClient, LLMError, LLMResponse
from app.clients.search import SearchClient
from app.models import TaskExecution, TraceStep

FAULT_KINDS: tuple[str, ...] = ("timeout", "http_500", "empty", "malformed")
DEFAULT_SYSTEM_PROMPT = (
    "You are a precise assistant. Use the available tools when they help. "
    "Answer concisely; put the final answer in the last sentence."
)


@dataclass
class AgentSpec:
    """Everything the runtime needs to know about an agent (decoupled from the ORM)."""

    name: str
    model: str
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools: list[str] = field(default_factory=list)
    temperature: float = 0.0
    max_steps: int = 6

    @classmethod
    def from_model(cls, agent: Any) -> AgentSpec:
        return cls(
            name=agent.name,
            model=agent.model,
            system_prompt=agent.system_prompt or DEFAULT_SYSTEM_PROMPT,
            tools=list(agent.tools or []),
            temperature=agent.temperature,
            max_steps=agent.max_steps,
        )


@dataclass
class StepRecord:
    type: str  # llm_call | tool_call | tool_result | error | retry | final
    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


@dataclass
class AgentResult:
    output: str = ""
    status: str = "ok"  # ok | error | max_steps
    error: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    faults_injected: int = 0
    tool_errors: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def had_failures(self) -> bool:
        return self.faults_injected > 0 or self.tool_errors > 0 or self.retries > 0

    @property
    def recovered(self) -> bool:
        """A failure happened during the run, yet the agent still produced a final answer.

        The benchmark engine tightens this to "and the answer was correct" once it is scored.
        """
        return self.had_failures and self.status == "ok" and bool(self.output.strip())


@dataclass
class RuntimeConfig:
    fault_rate: float = 0.0
    max_retries: int = 2
    backoff_base_ms: float = 500.0
    sleep_scale: float = 1.0
    """Multiplier on real backoff sleeps (0 in tests, the mock latency scale in mock mode)."""
    max_tokens: int | None = 1024


def _preview(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _inject_fault(kind: str) -> tuple[str, bool]:
    """Return ``(tool message, is_error)`` for an injected fault."""
    if kind == "timeout":
        return "ERROR: tool timed out after 5s (injected fault)", True
    if kind == "http_500":
        return "ERROR: upstream service returned HTTP 500 (injected fault)", True
    if kind == "empty":
        return "ERROR: tool returned an empty response (injected fault)", True
    return '{"results": [{"title": null, "content": "\\u0000\\u0000", "url": ', False  # malformed JSON


def run_agent(
    spec: AgentSpec,
    prompt: str,
    llm: LLMClient,
    search: SearchClient,
    config: RuntimeConfig | None = None,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentResult:
    """Run ``spec`` on ``prompt`` and return the full result with its trace."""
    cfg = config or RuntimeConfig()
    rng = rng or random.Random()
    result = AgentResult()
    ctx = ToolContext(search=search)
    tools = tool_definitions(spec.tools)
    messages: list[dict[str, Any]] = []
    if spec.system_prompt:
        messages.append({"role": "system", "content": spec.system_prompt})
    messages.append({"role": "user", "content": prompt})

    def add(step: StepRecord) -> None:
        result.steps.append(step)
        result.latency_ms += step.latency_ms

    last_content = ""
    for _ in range(spec.max_steps):
        response = _call_llm_with_retries(spec, messages, tools, llm, cfg, result, add, sleep)
        if response is None:
            return result  # status/error already set

        result.llm_calls += 1
        result.prompt_tokens += response.prompt_tokens
        result.completion_tokens += response.completion_tokens
        last_content = response.content or last_content
        add(
            StepRecord(
                type="llm_call",
                payload={
                    "model": spec.model,
                    "provider": response.provider,
                    "content": _preview(response.content),
                    "tool_calls": [
                        {"name": tc.name, "arguments": tc.arguments} for tc in response.tool_calls
                    ],
                    "finish_reason": response.finish_reason,
                    "messages_sent": len(messages),
                },
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
            )
        )

        if not response.tool_calls:
            result.output = response.content.strip()
            result.status = "ok"
            add(StepRecord(type="final", payload={"content": _preview(result.output)}))
            return result

        messages.append(
            {
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.raw_arguments or json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
        )
        for tc in response.tool_calls:
            result.tool_calls += 1
            add(
                StepRecord(
                    type="tool_call", payload={"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                )
            )
            content, payload, latency = _execute_tool(tc.name, tc.arguments, ctx, cfg, rng, result)
            add(
                StepRecord(
                    type="tool_result", payload={"id": tc.id, "name": tc.name, **payload}, latency_ms=latency
                )
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

    result.status = "max_steps"
    result.output = last_content.strip()
    result.error = f"stopped after {spec.max_steps} model calls without a final answer"
    add(StepRecord(type="error", payload={"kind": "max_steps", "message": result.error}))
    return result


def _call_llm_with_retries(
    spec: AgentSpec,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    llm: LLMClient,
    cfg: RuntimeConfig,
    result: AgentResult,
    add: Callable[[StepRecord], None],
    sleep: Callable[[float], None],
) -> LLMResponse | None:
    attempt = 0
    while True:
        try:
            return llm.chat(
                spec.model,
                messages,
                tools=tools or None,
                temperature=spec.temperature,
                max_tokens=cfg.max_tokens,
            )
        except LLMError as exc:
            add(
                StepRecord(
                    type="error",
                    payload={
                        "kind": exc.kind or "llm_error",
                        "status_code": exc.status_code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                        "attempt": attempt + 1,
                    },
                )
            )
            if not exc.retryable or attempt >= cfg.max_retries:
                result.status = "error"
                result.error = str(exc)
                result.output = ""
                return None
            backoff_ms = cfg.backoff_base_ms * (2**attempt)
            attempt += 1
            result.retries += 1
            add(
                StepRecord(
                    type="retry",
                    payload={"attempt": attempt + 1, "backoff_ms": backoff_ms},
                    latency_ms=backoff_ms,
                )
            )
            if cfg.sleep_scale > 0:
                sleep(backoff_ms * cfg.sleep_scale / 1000)


def _execute_tool(
    name: str,
    args: dict[str, Any],
    ctx: ToolContext,
    cfg: RuntimeConfig,
    rng: random.Random,
    result: AgentResult,
) -> tuple[str, dict[str, Any], float]:
    """Run one tool call (or inject a fault). Returns ``(message, trace payload, latency_ms)``."""
    if cfg.fault_rate > 0 and rng.random() < cfg.fault_rate:
        kind = rng.choice(FAULT_KINDS)
        result.faults_injected += 1
        content, is_error = _inject_fault(kind)
        if is_error:
            result.tool_errors += 1
        latency = 5_000.0 if kind == "timeout" else rng.uniform(50, 400)
        return content, {"ok": False, "fault": kind, "content": content}, latency

    started = time.perf_counter()
    try:
        content = run_tool(name, args, ctx)
        ok = True
    except ToolError as exc:
        content = f"ERROR: {exc}"
        ok = False
        result.tool_errors += 1
    except Exception as exc:  # a bug in a tool must never crash a benchmark run
        content = f"ERROR: tool crashed: {type(exc).__name__}: {exc}"
        ok = False
        result.tool_errors += 1
    latency = (time.perf_counter() - started) * 1000
    return content, {"ok": ok, "content": _preview(content)}, latency


def save_execution(
    db: Session,
    result: AgentResult,
    *,
    agent_id: int,
    prompt: str,
    run_id: int | None = None,
    task_id: int | None = None,
    repeat_idx: int = 0,
) -> TaskExecution:
    """Persist an :class:`AgentResult` and its trace (flushes; the caller commits)."""
    execution = TaskExecution(
        run_id=run_id,
        agent_id=agent_id,
        task_id=task_id,
        repeat_idx=repeat_idx,
        prompt=prompt,
        output=result.output,
        status=result.status,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=round(result.latency_ms, 1),
        steps=result.llm_calls,
        tool_calls=result.tool_calls,
        faults_injected=result.faults_injected,
        retries=result.retries,
        recovered=result.recovered,
        error=result.error,
        trace=[
            TraceStep(
                idx=i,
                type=s.type,
                payload=s.payload,
                prompt_tokens=s.prompt_tokens,
                completion_tokens=s.completion_tokens,
                latency_ms=round(s.latency_ms, 1),
            )
            for i, s in enumerate(result.steps)
        ],
    )
    db.add(execution)
    db.flush()
    return execution
