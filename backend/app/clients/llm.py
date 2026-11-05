"""LLM client interface and the real implementation that talks to the Bifrost gateway.

Bifrost exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint for every configured
provider; models are addressed as ``provider/model`` (``groq/openai/gpt-oss-20b``,
``openrouter/...``, ``ollama/granite4:3b``). We call it with ``httpx`` directly rather than an
SDK so that timeouts, error mapping and Bifrost's ``extra_fields`` (resolved provider,
upstream latency) are fully under our control.

Retries are deliberately *not* done here: the agent runtime owns retry policy so that every
retry is visible in the execution trace.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    """Original JSON string; kept so malformed arguments can be shown in traces."""


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    model: str = ""
    provider: str = ""
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMError(Exception):
    """A failed LLM call.

    ``retryable`` is True for transient failures (timeouts, connection errors, 429, 5xx) and
    False for errors that will not succeed on retry (bad model id, invalid request, auth).
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, kind: str = ""):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.kind = kind

    def __str__(self) -> str:
        code = f" [{self.status_code}]" if self.status_code else ""
        return f"{self.kind or 'llm_error'}{code}: {self.message}"


class LLMClient(Protocol):
    """Interface shared by the real Bifrost client and the mock."""

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...

    def list_models(self) -> list[str]: ...

    def ping(self) -> tuple[bool, str]: ...


def parse_tool_arguments(raw: str | dict | None) -> dict[str, Any]:
    """Parse a tool-call ``arguments`` payload; malformed JSON yields ``{"_raw": raw}``."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _extract_error_message(body: Any, fallback: str) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("error") or err)[:500]
        if isinstance(err, str):
            return err[:500]
        if "message" in body:
            return str(body["message"])[:500]
    return fallback[:500]


_INVALID_TOOL_CALL_MARKERS = ("tool call validation failed", "tool_use_failed", "failed to call a function")


def _is_invalid_tool_call(message: str) -> bool:
    lower = message.lower()
    return any(m in lower for m in _INVALID_TOOL_CALL_MARKERS)


def parse_completion(body: dict[str, Any], requested_model: str, latency_ms: float) -> LLMResponse:
    """Convert an OpenAI-style chat completion body into an :class:`LLMResponse`."""
    choices = body.get("choices") or []
    if not choices:
        raise LLMError("response contained no choices", retryable=True, kind="empty_response")
    choice = choices[0]
    message = choice.get("message") or {}
    tool_calls = [
        ToolCall(
            id=tc.get("id") or f"call_{i}",
            name=(tc.get("function") or {}).get("name", ""),
            arguments=parse_tool_arguments((tc.get("function") or {}).get("arguments")),
            raw_arguments=str((tc.get("function") or {}).get("arguments") or ""),
        )
        for i, tc in enumerate(message.get("tool_calls") or [])
    ]
    usage = body.get("usage") or {}
    extra = body.get("extra_fields") or {}
    return LLMResponse(
        content=message.get("content") or "",
        tool_calls=tool_calls,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        latency_ms=latency_ms,
        model=requested_model,
        provider=str(extra.get("provider") or requested_model.split("/", 1)[0]),
        finish_reason=choice.get("finish_reason") or "",
    )


class BifrostClient:
    """Real LLM client: OpenAI-compatible calls through the Bifrost gateway."""

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 60.0,
        health_timeout_s: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.health_timeout_s = health_timeout_s
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport)

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        started = time.perf_counter()
        try:
            res = self._http.post(f"{self.base_url}/v1/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"no response within {self.timeout_s:.0f}s", retryable=True, kind="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"cannot reach Bifrost: {exc}", retryable=True, kind="connection") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        try:
            body = res.json()
        except ValueError:
            body = None

        if res.status_code >= 400:
            message = _extract_error_message(body, res.text or res.reason_phrase)
            retryable = res.status_code == 429 or res.status_code >= 500
            kind = "rate_limited" if res.status_code == 429 else "provider_error"
            if res.status_code == 400 and _is_invalid_tool_call(message):
                # The *model* produced a tool call that the provider rejected against the schema.
                # The agent runtime gives the model a chance to correct it.
                retryable, kind = True, "invalid_tool_call"
            raise LLMError(message, retryable=retryable, status_code=res.status_code, kind=kind)
        if not isinstance(body, dict):
            raise LLMError("non-JSON response from Bifrost", retryable=True, kind="bad_response")
        return parse_completion(body, model, latency_ms)

    def list_models(self) -> list[str]:
        """Return every ``provider/model`` id Bifrost can route to."""
        try:
            res = self._http.get(f"{self.base_url}/v1/models", timeout=30.0)
            res.raise_for_status()
            return sorted(m["id"] for m in res.json().get("data", []) if "id" in m)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LLMError(f"could not list models: {exc}", retryable=True, kind="list_models") from exc

    def ping(self) -> tuple[bool, str]:
        try:
            res = self._http.get(f"{self.base_url}/health", timeout=self.health_timeout_s)
        except httpx.HTTPError as exc:
            return False, f"unreachable: {type(exc).__name__}"
        return (True, "ok") if res.status_code == 200 else (False, f"HTTP {res.status_code}")
