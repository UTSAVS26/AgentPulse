"""Routes each model id to the right client: Bifrost, or a user's model connection.

* ``custom/<slug>/<model>`` → the direct OpenAI-compatible client of connection ``<slug>``, which is sent
  ``<model>``.
* anything else (``groq/...``, ``openrouter/...``, ``ollama/...``) → Bifrost.

Because the router implements the same :class:`~app.clients.llm.LLMClient` protocol, the agent runtime, the
benchmark engine, the LLM judge and the stale-answer detector use connections without any change.

Clients are built from the ``model_connections`` table on first use and cached; a connection's
``updated_at`` is checked on every call (one indexed query, negligible next to an LLM call), so edits take
effect immediately. The URL policy (:mod:`app.net_guard`) is re-checked whenever a client is (re)built.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.llm import LLMClient, LLMError, LLMResponse, OpenAICompatibleClient
from app.config import Settings, get_settings

CUSTOM_PREFIX = "custom/"


def custom_model_id(slug: str, model: str) -> str:
    return f"{CUSTOM_PREFIX}{slug}/{model}"


def parse_custom(model: str) -> tuple[str, str] | None:
    """``custom/<slug>/<model>`` → ``(slug, model)``; None for any other id."""
    if not model.startswith(CUSTOM_PREFIX):
        return None
    slug, _, name = model[len(CUSTOM_PREFIX) :].partition("/")
    return (slug, name) if slug and name else None


default_transport: httpx.BaseTransport | None = None
"""Test seam: an ``httpx.MockTransport`` standing in for every connection's endpoint."""


def build_client(
    base_url: str, api_key: str | None, settings: Settings, label: str
) -> OpenAICompatibleClient:
    from app import net_guard  # lazy: avoids an import cycle through app.mode

    checked = net_guard.check(base_url, settings)
    return OpenAICompatibleClient(
        checked.url,
        api_key=api_key,
        timeout_s=settings.llm_timeout_s,
        health_timeout_s=settings.health_timeout_s,
        transport=default_transport,
        label=label,
    )


class ConnectionRegistry:
    """Direct clients for model connections, cached by slug and rebuilt when the row changes."""

    def __init__(self, sessions: Callable[[], Session], settings: Settings | None = None):
        self.sessions = sessions
        self.settings = settings
        self._clients: dict[str, tuple[datetime | None, OpenAICompatibleClient]] = {}
        self._lock = threading.Lock()

    def _settings(self) -> Settings:
        return self.settings or get_settings()

    def client(self, slug: str) -> OpenAICompatibleClient:
        from app import crypto, net_guard
        from app.models import ModelConnection

        with self.sessions() as db:
            conn = db.scalar(select(ModelConnection).where(ModelConnection.slug == slug))
            if conn is None:
                raise LLMError(f"unknown model connection '{slug}'", retryable=False, kind="provider_error")
            with self._lock:
                cached = self._clients.get(slug)
                if cached and cached[0] == conn.updated_at:
                    return cached[1]
            try:
                key = crypto.decrypt(conn.api_key_enc) if conn.api_key_enc else None
                client = build_client(conn.base_url, key, self._settings(), label=conn.name)
            except (crypto.SecretError, net_guard.UnsafeURL) as exc:
                raise LLMError(
                    f"connection '{conn.name}': {exc}", retryable=False, kind="provider_error"
                ) from exc
            with self._lock:
                self._clients[slug] = (conn.updated_at, client)
            return client

    def model_ids(self) -> list[str]:
        from app.models import ModelConnection

        with self.sessions() as db:
            return sorted(
                custom_model_id(c.slug, m)
                for c in db.scalars(select(ModelConnection))
                for m in (c.models or [])
            )

    def invalidate(self, slug: str | None = None) -> None:
        with self._lock:
            if slug is None:
                self._clients.clear()
            else:
                self._clients.pop(slug, None)


class ModelRouter:
    """An :class:`LLMClient` that dispatches ``custom/...`` ids to connections and the rest to Bifrost."""

    def __init__(self, bifrost: LLMClient, registry: ConnectionRegistry):
        self.bifrost = bifrost
        self.registry = registry

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        parsed = parse_custom(model)
        if parsed is None:
            return self.bifrost.chat(
                model, messages, tools=tools, temperature=temperature, max_tokens=max_tokens
            )
        slug, name = parsed
        res = self.registry.client(slug).chat(
            name, messages, tools=tools, temperature=temperature, max_tokens=max_tokens
        )
        return replace(res, model=model, provider=f"custom/{slug}")

    def list_models(self) -> list[str]:
        custom = self.registry.model_ids()
        try:
            return sorted(set(self.bifrost.list_models()) | set(custom))
        except LLMError:
            if custom:
                return custom
            raise

    def ping(self) -> tuple[bool, str]:
        return self.bifrost.ping()


def connection_prices(model: str) -> tuple[float, float] | None:
    """USD per 1M tokens (input, output) set on the connection of a ``custom/...`` model, if any."""
    parsed = parse_custom(model)
    if parsed is None:
        return None
    from app.db import get_sessionmaker
    from app.models import ModelConnection

    with get_sessionmaker()() as db:
        conn = db.scalar(select(ModelConnection).where(ModelConnection.slug == parsed[0]))
        if conn is None or (conn.price_input_per_m is None and conn.price_output_per_m is None):
            return None
        return float(conn.price_input_per_m or 0.0), float(conn.price_output_per_m or 0.0)
