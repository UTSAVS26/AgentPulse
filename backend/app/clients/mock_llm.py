"""Deterministic fake LLM used when ``MOCK_MODE=true`` (offline demo and tests).

The mock behaves enough like a real tool-calling model that the whole pipeline (agent
runtime, scoring, metrics, dashboard) shows meaningful, *different* results per agent:

* Every model gets a :class:`ModelProfile` (accuracy, latency, verbosity, failure rate,
  tool-use and recovery tendencies). Known demo models have hand-tuned profiles; any other
  id gets a stable profile derived from a hash of its name.
* With tools available it asks for the right tool (calculator for arithmetic, current_date
  for date questions, web_search otherwise) and then answers from the tool result.
* If a tool returns an error it may retry the tool (recovery) or give up and answer.
* Correct answers come from an optional ``answer_key`` (prompt -> expected answer). Whether
  the model "knows" it is a seeded coin flip weighted by the profile's accuracy, so repeated
  runs vary realistically (needed for consistency metrics) yet are reproducible.
* Extra behaviour (e.g. claim extraction for the freshness checker) plugs in via
  ``responders``: callables that may return a canned reply for specific prompts.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.clients import mock_news
from app.clients.chaos import OUTAGE_MESSAGE, parse_chaos
from app.clients.llm import LLMError, LLMResponse, ToolCall
from app.clients.mock_freshbench import freshbench_responder
from app.clients.mock_freshness import freshness_responder
from app.clients.mock_guard import guard_responder
from app.clients.mock_judge import judge_responder
from app.clients.mock_search import mock_fact_answer

Responder = Callable[[str, list[dict[str, Any]], random.Random], str | None]


@dataclass(frozen=True)
class ModelProfile:
    accuracy: float  # probability of answering a known question correctly
    latency_ms: float  # mean latency per call
    jitter_ms: float  # +/- latency noise
    verbosity: float  # completion-token multiplier
    failure_rate: float  # probability a call raises a transient provider error
    tool_use: float  # probability of using a tool when one would help
    recovery: float  # probability of retrying a tool after it errors
    cutoff_days: int = 240  # knowledge cutoff, in days before today (Freshness Bench questions)


KNOWN_PROFILES: dict[str, ModelProfile] = {
    "groq/openai/gpt-oss-120b": ModelProfile(0.92, 550, 150, 1.4, 0.02, 0.95, 0.90, 210),
    "groq/openai/gpt-oss-20b": ModelProfile(0.82, 480, 120, 1.2, 0.03, 0.90, 0.75, 260),
    "groq/qwen/qwen3.8-27b": ModelProfile(0.86, 160, 60, 0.9, 0.03, 0.85, 0.70, 140),
    "openrouter/nvidia/nemotron-3.5-lightning:free": ModelProfile(
        0.74, 2000, 700, 1.1, 0.08, 0.75, 0.55, 110
    ),
    "ollama/granite4:3b": ModelProfile(0.58, 900, 300, 0.8, 0.01, 0.60, 0.40, 430),
    "ollama/qwen3.5:9b": ModelProfile(0.72, 1800, 500, 1.0, 0.01, 0.80, 0.60, 300),
}


def profile_for(model: str) -> ModelProfile:
    """Return the hand-tuned profile for ``model`` or a stable hash-derived one."""
    if model in KNOWN_PROFILES:
        return KNOWN_PROFILES[model]
    h = int(hashlib.sha256(model.encode()).hexdigest(), 16)
    r = random.Random(h)
    return ModelProfile(
        accuracy=round(r.uniform(0.55, 0.9), 2),
        latency_ms=round(r.uniform(200, 2500)),
        jitter_ms=round(r.uniform(50, 400)),
        verbosity=round(r.uniform(0.7, 1.5), 2),
        failure_rate=round(r.uniform(0.0, 0.06), 3),
        tool_use=round(r.uniform(0.6, 0.95), 2),
        recovery=round(r.uniform(0.4, 0.9), 2),
        cutoff_days=r.randint(90, 450),
    )


def knows_event(profile: ModelProfile, event_date: str, rng: random.Random) -> bool:
    """Whether a model 'learned' an event: likely if it is older than the cutoff, unlikely if newer
    (a smooth ~25-day ramp, so the knowledge curve looks like a real one)."""
    age = (mock_news.today() - date.fromisoformat(event_date)).days
    p_know = 1 / (1 + math.exp(-(age - profile.cutoff_days) / 25))
    return rng.random() < p_know


_ARITH = re.compile(r"[\d(][\d\s.+\-*/%^()]*[\d)]")
_DATE_WORDS = ("today", "current date", "what year", "what is the date", "day of the week")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    return [(t.get("function") or {}).get("name", "") for t in (tools or [])]


def _perturb(answer: str, rng: random.Random) -> str:
    """Produce a plausible wrong answer."""
    num = re.search(r"-?\d+(?:\.\d+)?", answer)
    if num:
        value = float(num.group())
        wrong = value + rng.choice([-1, 1]) * max(1, abs(value) * rng.uniform(0.05, 0.3))
        wrong_str = str(int(wrong)) if "." not in num.group() else f"{wrong:.2f}"
        return answer[: num.start()] + wrong_str + answer[num.end() :]
    return rng.choice(["I'm not certain about this.", "I don't have reliable information on that."])


class MockLLMClient:
    """Fake :class:`~app.clients.llm.LLMClient` with realistic, seeded behaviour."""

    def __init__(
        self,
        seed: int = 0,
        latency_scale: float = 1.0,
        answer_key: dict[str, str] | None = None,
        responders: list[Responder] | None = None,
        failure_rate_override: float | None = None,
    ):
        self.seed = seed
        self.latency_scale = latency_scale
        self.answer_key = {_normalize(k): v for k, v in (answer_key or {}).items()}
        self.stale_key: dict[str, str] = {}
        self.event_dates: dict[str, str] = {}
        # The detector's, judges' and Freshness Bench generator's prompts are always answered.
        self.responders: list[Responder] = [
            freshness_responder,
            judge_responder,
            freshbench_responder,
            guard_responder,
            *(responders or []),
        ]
        self.failure_rate_override = failure_rate_override
        self._calls: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ configuration
    def set_answer_key(self, answer_key: dict[str, str], stale_key: dict[str, str] | None = None) -> None:
        """Register correct answers (and, for time-sensitive prompts, the outdated answer a model
        with old training data would give)."""
        self.answer_key.update({_normalize(k): v for k, v in answer_key.items()})
        self.stale_key.update({_normalize(k): v for k, v in (stale_key or {}).items()})

    def set_event_dates(self, event_dates: dict[str, str]) -> None:
        """Register when each question's event happened (Freshness Bench): the model only knows events older
        than its knowledge cutoff, unless a tool looked them up."""
        self.event_dates.update({_normalize(k): v for k, v in event_dates.items() if v})

    def add_responder(self, responder: Responder) -> None:
        self.responders.append(responder)

    # ------------------------------------------------------------------ LLMClient API
    def list_models(self) -> list[str]:
        return sorted(KNOWN_PROFILES)

    def ping(self) -> tuple[bool, str]:
        return True, "mock"

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        fallbacks: list[str] | None = None,
    ) -> LLMResponse:
        requested = model
        primary_provider, fallback_used = "", False
        chaos = parse_chaos(model)
        if chaos:  # outage drill: like Bifrost with the chaos provider and request-level fallbacks
            rate, real = chaos
            drill = self._rng(f"chaos|{real}", messages)
            if drill.random() < rate:
                if not fallbacks:
                    self._sleep(15)
                    raise LLMError(OUTAGE_MESSAGE, retryable=True, status_code=503, kind="provider_error")
                model, primary_provider, fallback_used = fallbacks[0], "chaos", True
            else:
                model = real
        profile = profile_for(model)
        rng = self._rng(model, messages)
        latency_ms = max(20.0, profile.latency_ms + rng.uniform(-profile.jitter_ms, profile.jitter_ms))
        # the gateway's own share, derived (not drawn) so the seeded answers stay exactly as they were
        overhead_ms = 1.0 + (int(latency_ms * 1000) % 500) / 100 + (8.0 if fallback_used else 0.0)
        self._sleep(latency_ms + overhead_ms)

        failure_rate = (
            self.failure_rate_override if self.failure_rate_override is not None else profile.failure_rate
        )
        if rng.random() < failure_rate:
            raise LLMError(
                "simulated upstream 503 (mock)", retryable=True, status_code=503, kind="provider_error"
            )

        content, tool_calls = self._respond(model, messages, tools, profile, rng)
        prompt_tokens = sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)
        if tools:
            prompt_tokens += 60 * len(tools)
        completion_tokens = int(
            (_estimate_tokens(content) + 25 * len(tool_calls) + rng.randint(5, 40)) * profile.verbosity
        )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms + overhead_ms,
            model=requested,
            provider=model.split("/", 1)[0],
            finish_reason="tool_calls" if tool_calls else "stop",
            fallback_used=fallback_used,
            primary_provider=primary_provider,
            upstream_latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------ internals
    def _rng(self, model: str, messages: list[dict[str, Any]]) -> random.Random:
        """Seeded RNG; the per-conversation call counter makes repeats differ reproducibly."""
        convo = json.dumps(messages, sort_keys=True, default=str)
        key = hashlib.sha256(f"{self.seed}|{model}|{convo}".encode()).hexdigest()
        with self._lock:
            n = self._calls.get(key, 0)
            self._calls[key] = n + 1
        return random.Random(f"{key}|{n}")

    def _sleep(self, latency_ms: float) -> None:
        if self.latency_scale > 0:
            time.sleep(latency_ms * self.latency_scale / 1000)

    def _lookup_answer(self, prompt: str) -> str | None:
        norm = _normalize(prompt)
        if norm in self.answer_key:
            return self.answer_key[norm]
        for key, answer in self.answer_key.items():
            if key and (key in norm or norm in key):
                return answer
        return None

    def _respond(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        profile: ModelProfile,
        rng: random.Random,
    ) -> tuple[str, list[ToolCall]]:
        for responder in self.responders:
            reply = responder(model, messages, rng)
            if reply is not None:
                return reply, []

        user_prompt = next((str(m.get("content") or "") for m in messages if m.get("role") == "user"), "")
        last = messages[-1] if messages else {}
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        available = _tool_names(tools)

        # 1) First turn with tools: decide whether to call one.
        if available and last.get("role") == "user" and rng.random() < profile.tool_use:
            call = self._choose_tool(user_prompt, available)
            if call:
                return "", [call]

        # 2) A tool just failed: maybe retry it (recovery behaviour).
        if last.get("role") == "tool" and _looks_like_error(str(last.get("content") or "")):
            if len(tool_msgs) < 3 and rng.random() < profile.recovery and available:
                call = self._choose_tool(user_prompt, available, call_id=f"call_retry_{len(tool_msgs)}")
                if call:
                    return "", [call]

        # 3) Final answer.
        good_tool_result = next(
            (
                str(m.get("content") or "")
                for m in reversed(tool_msgs)
                if not _looks_like_error(str(m.get("content") or ""))
            ),
            None,
        )
        expected = self._lookup_answer(user_prompt)
        event_date = self.event_dates.get(_normalize(user_prompt))
        if event_date and not good_tool_result and not knows_event(profile, event_date, rng):
            return "I don't have information about that event: it may be after my training data.", []
        stale = self.stale_key.get(_normalize(user_prompt))
        if stale and not good_tool_result and rng.random() < 0.85:
            # Time-sensitive question answered from (simulated) training data: confidently outdated.
            return f"The answer is {stale}.", []
        if expected is not None:
            accuracy = min(0.99, profile.accuracy + (0.08 if good_tool_result else 0.0))
            answer = expected if rng.random() < accuracy else _perturb(expected, rng)
            if answer.endswith("."):  # a hedge ("I'm not certain about this."), not an answer
                return answer, []
            return f"The answer is {answer}.", []
        if good_tool_result:
            return f"Based on the tool result: {good_tool_result[:300]}", []
        # A question on a known topic (e.g. through the Freshness Guard): answered from "training data", so
        # models with an older knowledge cutoff are more often out of date.
        fact = mock_fact_answer(user_prompt, min(0.85, 0.35 + profile.cutoff_days / 1000), rng)
        if fact:
            return fact, []
        return f"(mock {model}) Here is my best answer to: {user_prompt[:160]}", []

    def _choose_tool(self, prompt: str, available: list[str], call_id: str = "call_0") -> ToolCall | None:
        lower = prompt.lower()
        if "calculator" in available:
            match = max(_ARITH.findall(prompt), key=len, default="")
            if re.search(r"\d\s*[-+*/%^]\s*[\d(]", match):
                args = {"expression": match.strip()}
                return ToolCall(call_id, "calculator", args, json.dumps(args))
        if "current_date" in available and any(w in lower for w in _DATE_WORDS):
            return ToolCall(call_id, "current_date", {}, "{}")
        if "web_search" in available:
            args = {"query": prompt.strip()[:200]}
            return ToolCall(call_id, "web_search", args, json.dumps(args))
        return None


def _looks_like_error(text: str) -> bool:
    head = text.strip()[:80].lower()
    return head.startswith(("error", "tool error", '{"error"')) or "timed out" in head
