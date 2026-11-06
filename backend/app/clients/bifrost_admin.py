"""Bifrost management API: register hosted providers and their keys at runtime.

Facts verified against the pinned Bifrost image (see docs/bifrost.md, "Runtime provider registration"):

* Management endpoints need ``X-Bifrost-Setup-Token`` (Bifrost's ``BIFROST_SETUP_TOKEN``): 401 without it,
  403 with a wrong one.
* ``POST /api/providers {"provider": p}`` adds a provider (409 if it exists); it needs Bifrost's config store,
  otherwise 500 "config store not found" (our Render config disables the store).
* ``POST /api/providers/{p}/keys`` adds a key and **validates it** (``status: success | list_models_failed``,
  ``description``); ``DELETE /api/providers/{p}/keys/{id}``; ``DELETE /api/providers/{p}``.
* Changes apply immediately (no restart), but with ``source_of_truth: "config.json"`` they are **dropped when
  Bifrost restarts**, so our database stays the source of truth and connections are re-registered on demand.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.clients.llm import redact

# Providers whose keys Bifrost already holds from config.json. A user key added there would be load-balanced
# with ours (Bifrost picks among all keys of a provider), so connections to them are always called directly.
BUILTIN_PROVIDERS = frozenset({"groq", "openrouter", "ollama"})
# Native Bifrost providers a user may route through the gateway with their own key.
NATIVE_PROVIDERS = frozenset(
    {"openai", "anthropic", "gemini", "mistral", "deepseek", "xai", "cerebras", "cohere", "perplexity"}
)
KEY_PREFIX = "agentpulse-"
SETUP_HEADER = "X-Bifrost-Setup-Token"


class BifrostAdminError(Exception):
    """A management call failed; ``message`` is safe to show (keys are never echoed)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def key_name(slug: str) -> str:
    return f"{KEY_PREFIX}{slug}"


class BifrostAdmin:
    def __init__(
        self, base_url: str, token: str, timeout_s: float = 20.0, transport: httpx.BaseTransport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            transport=transport,
            headers={SETUP_HEADER: self.token} if self.token else {},
        )

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, path: str, json: Any = None) -> httpx.Response:
        if not self.token:
            raise BifrostAdminError("BIFROST_SETUP_TOKEN is not set, so Bifrost's management API is locked")
        try:
            return self._http.request(method, f"{self.base_url}{path}", json=json)
        except httpx.HTTPError as exc:
            raise BifrostAdminError(f"cannot reach Bifrost: {type(exc).__name__}") from exc

    @staticmethod
    def _error(res: httpx.Response, secret: str | None = None) -> BifrostAdminError:
        try:
            body = res.json()
            msg = (body.get("error") or {}).get("message") or res.text
        except ValueError:
            msg = res.text
        msg = redact(str(msg))
        if secret:
            msg = msg.replace(secret, "***")
        if res.status_code in (401, 403):
            msg = f"setup token rejected by Bifrost ({msg})"
        return BifrostAdminError(msg[:300], res.status_code)

    def providers(self) -> list[str]:
        res = self._call("GET", "/api/providers")
        if res.status_code != 200:
            raise self._error(res)
        return [p.get("name") for p in (res.json().get("providers") or [])]

    def ensure_provider(self, provider: str) -> None:
        res = self._call("POST", "/api/providers", {"provider": provider})
        if res.status_code in (200, 201, 409):
            return
        raise self._error(res)

    def keys(self, provider: str) -> dict[str, str]:
        """Key name → id (empty when the provider doesn't exist)."""
        res = self._call("GET", f"/api/providers/{provider}/keys")
        if res.status_code == 404:
            return {}
        if res.status_code != 200:
            raise self._error(res)
        return {k.get("name"): k.get("id") for k in (res.json().get("keys") or [])}  # null when none

    def upsert_key(self, provider: str, name: str, value: str) -> dict[str, str | None]:
        """Replace key ``name``; returns Bifrost's own validation of the key (``status``, ``description``)."""
        existing = self.keys(provider).get(name)
        if existing:
            self._delete(f"/api/providers/{provider}/keys/{existing}")
        body = {"name": name, "value": value, "models": ["*"], "weight": 1}
        res = self._call("POST", f"/api/providers/{provider}/keys", body)
        if res.status_code not in (200, 201):
            raise self._error(res, secret=value)
        data = res.json()
        description = data.get("description")
        return {
            "id": data.get("id"),
            "status": data.get("status"),
            "description": description.replace(value, "***") if isinstance(description, str) else None,
        }

    def delete_key(self, provider: str, name: str) -> bool:
        key_id = self.keys(provider).get(name)
        if not key_id:
            return False
        self._delete(f"/api/providers/{provider}/keys/{key_id}")
        return True

    def delete_provider_if_unused(self, provider: str) -> bool:
        """Remove a provider we added once it has no keys left (never one from config.json)."""
        if provider in BUILTIN_PROVIDERS or self.keys(provider):
            return False
        self._delete(f"/api/providers/{provider}")
        return True

    def _delete(self, path: str) -> None:
        res = self._call("DELETE", path)
        if res.status_code not in (200, 204, 404):
            raise self._error(res)
