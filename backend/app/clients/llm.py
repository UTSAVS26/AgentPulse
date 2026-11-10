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
import re
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


_ORG_ID = re.compile(r"\s*in organi[sz]ation `?[\w-]+`?", re.IGNORECASE)


def redact(message: str) -> str:
    """Strip account identifiers (e.g. Groq's ``in organization `org_…```) from provider errors, since they
    end up in the database, the UI and exported reports."""
    return _ORG_ID.sub("", message)


class LLMError(Exception):
    """A failed LLM call.

    ``retryable`` is True for transient failures (timeouts, connection errors, 429, 5xx) and
    False for errors that will not succeed on retry (bad model id, invalid request, auth).
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, kind: str = ""):
        message = redact(message)
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


class OpenAICompatibleClient:
    """Client for any OpenAI-compatible chat API: Bifrost, or a user's endpoint (Ollama, LM Studio, vLLM,
    llama.cpp, OpenAI, Anthropic/Gemini compatibility endpoints...).

    ``base_url`` is the API root that contains ``/chat/completions`` (e.g. ``http://host:11434/v1``). The API
    key, if any, is sent as ``Authorization: Bearer`` and scrubbed from every error message.
    """

    chat_path = "/chat/completions"
    models_path = "/models"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        health_timeout_s: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        label: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.health_timeout_s = health_timeout_s
        self.label = label or (httpx.URL(self.base_url).host or self.base_url)
        self._api_key = api_key or None
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0), transport=transport, headers=headers
        )

    def _scrub(self, message: str) -> str:
        if self._api_key and len(self._api_key) >= 6:
            message = message.replace(self._api_key, "***")
        return message

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        try:
            return self._chat_once(model, messages, tools, temperature, max_tokens)
        except LLMError as exc:
            budget = affordable_budget(exc, max_tokens)
            if budget is None:
                raise
            # Low prepaid balance: the provider reserves credit for the whole max_tokens up front and
            # names what it can afford. A judge verdict needs ~200 tokens, so retry once within budget.
            return self._chat_once(model, messages, tools, temperature, budget)

    def _chat_once(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int | None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        started = time.perf_counter()
        try:
            res = self._http.post(f"{self.base_url}{self.chat_path}", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"no response within {self.timeout_s:.0f}s", retryable=True, kind="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                self._scrub(f"cannot reach {self.label}: {describe_http_error(exc)}"),
                retryable=True,
                kind="connection",
            ) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        try:
            body = res.json()
        except ValueError:
            body = None

        if res.status_code >= 400:
            message = self._scrub(_extract_error_message(body, res.text or res.reason_phrase))
            retryable = res.status_code == 429 or res.status_code >= 500
            kind = "rate_limited" if res.status_code == 429 else "provider_error"
            if res.status_code == 400 and _is_invalid_tool_call(message):
                # The *model* produced a tool call that the provider rejected against the schema.
                # The agent runtime gives the model a chance to correct it.
                retryable, kind = True, "invalid_tool_call"
            if res.status_code in (401, 403):
                kind = "auth"
            raise LLMError(message, retryable=retryable, status_code=res.status_code, kind=kind)
        if not isinstance(body, dict):
            raise LLMError(f"non-JSON response from {self.label}", retryable=True, kind="bad_response")
        return parse_completion(body, model, latency_ms)

    def list_models(self) -> list[str]:
        """Model ids the endpoint reports (``GET /models``)."""
        try:
            res = self._http.get(f"{self.base_url}{self.models_path}", timeout=30.0)
            res.raise_for_status()
            return sorted(m["id"] for m in res.json().get("data", []) if "id" in m)
        except (httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError) as exc:
            reason = describe_http_error(exc) if isinstance(exc, httpx.HTTPError) else exc
            raise LLMError(
                self._scrub(f"could not list models: {reason}"), retryable=True, kind="list_models"
            ) from exc

    def ping(self) -> tuple[bool, str]:
        try:
            res = self._http.get(f"{self.base_url}{self.models_path}", timeout=self.health_timeout_s)
        except httpx.HTTPError as exc:
            return False, f"unreachable: {type(exc).__name__}"
        return (True, "ok") if res.status_code == 200 else (False, f"HTTP {res.status_code}")


AFFORD_RE = re.compile(r"can only afford (\d+)", re.IGNORECASE)
MIN_AFFORDABLE_TOKENS = 128


def affordable_budget(exc: LLMError, requested: int | None) -> int | None:
    """A smaller ``max_tokens`` to retry with after "402 … can only afford N" (OpenRouter), else None."""
    if exc.status_code != 402:
        return None
    match = AFFORD_RE.search(str(exc))
    if not match:
        return None
    budget = int(int(match.group(1)) * 0.9)  # margin: the prompt is billed too
    if budget < MIN_AFFORDABLE_TOKENS or (requested is not None and budget >= requested):
        return None
    return budget


def describe_http_error(exc: Exception) -> str:
    """Plain-language reason for a failed request. A server that isn't running often surfaces as the *last*
    address tried (e.g. Docker's unroutable IPv6 → "Network is unreachable"), which misleads users."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, httpx.ConnectError):
        if any(k in low for k in ("refused", "unreachable", "errno 111", "errno 101", "errno 61", "10061")):
            return "nothing is answering at this address. Is the server running and listening on this port?"
        dns = ("name or service", "nodename", "getaddrinfo", "name resolution", "errno -2")
        if any(k in low for k in dns):
            return "unknown host name. Check the base URL."
        if "ssl" in low or "certificate" in low:
            return f"TLS/certificate error ({text})"
    return text or type(exc).__name__


class BifrostClient(OpenAICompatibleClient):
    """Real LLM client: OpenAI-compatible calls through the Bifrost gateway (``/v1/...``)."""

    chat_path = "/v1/chat/completions"
    models_path = "/v1/models"

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 60.0,
        health_timeout_s: float = 3.0,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(base_url, None, timeout_s, health_timeout_s, transport, label="Bifrost")

    def list_models(self) -> list[str]:
        """Return every ``provider/model`` id Bifrost can route to."""
        return super().list_models()

    def ping(self) -> tuple[bool, str]:
        try:
            res = self._http.get(f"{self.base_url}/health", timeout=self.health_timeout_s)
        except httpx.HTTPError as exc:
            return False, f"unreachable: {type(exc).__name__}"
        return (True, "ok") if res.status_code == 200 else (False, f"HTTP {res.status_code}")
