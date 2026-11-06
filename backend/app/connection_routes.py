"""Decide and apply each model connection's route: through Bifrost, or directly.

A connection to a hosted provider that Bifrost supports natively (OpenAI, Anthropic, Gemini, Mistral…)
is **registered in Bifrost** with the user's key, so its traffic goes through the gateway like every
built-in model. Everything else is called **directly** (OpenAI-compatible client). The reason is stored on
the connection (``route_reason``) and shown in the UI:

* self-hosted / custom endpoints → direct (an arbitrary ``base_url`` can't be set through Bifrost's
  management API without an admin session);
* Groq / OpenRouter / Ollama → direct: Bifrost already holds keys for them and load-balances across all
  keys of a provider, so a user key would get mixed with the built-in one;
* a second connection to the same provider → direct, for the same reason;
* no ``BIFROST_SETUP_TOKEN`` / no config store (Render) / registration failed → direct, using the
  provider's OpenAI-compatible base URL.

Bifrost forgets runtime registrations when it restarts, so registration is re-applied on demand:
:func:`reregister` is called by the router when Bifrost answers "unknown provider".
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto
from app.clients.bifrost_admin import (
    BUILTIN_PROVIDERS,
    NATIVE_PROVIDERS,
    BifrostAdmin,
    BifrostAdminError,
    key_name,
)
from app.config import Settings, get_settings
from app.models import ModelConnection

log = logging.getLogger(__name__)
_heal_lock = threading.Lock()


@lru_cache
def _admin(base_url: str, token: str) -> BifrostAdmin:
    return BifrostAdmin(base_url, token)


def get_admin(settings: Settings | None = None) -> BifrostAdmin:
    s = settings or get_settings()
    return _admin(s.bifrost_url, s.bifrost_setup_token)


def wants_bifrost(conn: ModelConnection) -> bool:
    return conn.kind == "hosted" and bool(conn.provider)


def _blocker(db: Session, conn: ModelConnection, admin: BifrostAdmin) -> str | None:
    """Why this connection can't be routed through Bifrost (None = it can)."""
    provider = conn.provider or ""
    if provider in BUILTIN_PROVIDERS:
        return (
            f"Bifrost already uses its own {provider} key and would mix it with yours, so this connection "
            "calls the provider directly"
        )
    if provider not in NATIVE_PROVIDERS:
        return f"'{provider}' is not a provider Bifrost supports natively"
    if not conn.api_key_enc:
        return "no API key: hosted providers need one"
    other = db.scalar(
        select(ModelConnection.name).where(
            ModelConnection.provider == provider,
            ModelConnection.route == "bifrost",
            ModelConnection.id != conn.id,
        )
    )
    if other:
        return (
            f"'{other}' already routes {provider} through Bifrost, which would share keys between the two, "
            "so this one is called directly"
        )
    if not admin.configured:
        return "BIFROST_SETUP_TOKEN is not set, so providers can't be registered in Bifrost"
    return None


def apply(db: Session, conn: ModelConnection, settings: Settings | None = None) -> None:
    """Set ``route``, ``route_reason`` and ``bifrost_key_status``, registering in Bifrost when routed there.
    Never raises; the caller commits."""
    admin = get_admin(settings)
    if not wants_bifrost(conn):
        conn.route, conn.route_reason, conn.bifrost_key_status = "direct", None, None
        return
    blocker = _blocker(db, conn, admin)
    if blocker:
        _unregister_quietly(conn, admin)
        conn.route, conn.route_reason, conn.bifrost_key_status = "direct", blocker, None
        return
    try:
        status = register(conn, admin)
    except Exception as exc:  # never let routing break saving a connection
        message = exc.message if isinstance(exc, BifrostAdminError) else f"{type(exc).__name__}: {exc}"
        if "config store" in message.lower():
            message = "Bifrost runs without a config store here (e.g. the free Render deployment)"
        conn.route = "direct"
        conn.route_reason = f"Bifrost registration failed ({message}); calling {conn.provider} directly"
        conn.bifrost_key_status = None
        log.warning("Connection %s: %s", conn.slug, conn.route_reason)
        return
    conn.route = "bifrost"
    conn.route_reason = f"registered in Bifrost as provider '{conn.provider}'"
    conn.bifrost_key_status = status


def register(conn: ModelConnection, admin: BifrostAdmin) -> str:
    """Add the provider (if needed) and the connection's key; returns Bifrost's key check as text."""
    if not conn.api_key_enc:
        raise BifrostAdminError("no API key")
    admin.ensure_provider(conn.provider)
    result = admin.upsert_key(conn.provider, key_name(conn.slug), crypto.decrypt(conn.api_key_enc))
    if result.get("status") in (None, "success"):
        return "key accepted by Bifrost"
    return f"Bifrost key check: {result.get('description') or result.get('status')}"


def _unregister_quietly(conn: ModelConnection, admin: BifrostAdmin) -> None:
    if not (conn.provider and admin.configured) or conn.provider in BUILTIN_PROVIDERS:
        return
    try:
        if admin.delete_key(conn.provider, key_name(conn.slug)):
            admin.delete_provider_if_unused(conn.provider)
    except Exception as exc:
        log.info("Could not remove %s's key from Bifrost: %s", conn.slug, exc)


def unregister(conn: ModelConnection, settings: Settings | None = None) -> None:
    """Remove the connection's key from Bifrost (on delete or when it stops being Bifrost-routed)."""
    if conn.route == "bifrost":
        _unregister_quietly(conn, get_admin(settings))


def reregister(slug: str) -> bool:
    """Re-apply a Bifrost registration (Bifrost forgets them on restart). True if it is registered now."""
    from app.db import get_sessionmaker

    with _heal_lock, get_sessionmaker()() as db:
        conn = db.scalar(select(ModelConnection).where(ModelConnection.slug == slug))
        if conn is None or conn.route != "bifrost":
            return False
        try:
            register(conn, get_admin())
            log.info("Re-registered connection %s in Bifrost", slug)
            return True
        except Exception as exc:
            log.warning("Re-registering %s in Bifrost failed: %s", slug, exc)
            return False
