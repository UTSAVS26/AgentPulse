"""The outage-drill (chaos) provider's token: created on the UI, stored encrypted, pushed into Bifrost.

Bifrost calls AgentPulse's ``/chaos`` endpoint with the chaos provider's key as a bearer token, so only
Bifrost can trigger it. By default that key is ``CHAOS_TOKEN`` from the environment. From the dashboard
you can
replace it with a random token (or your own): it is stored **encrypted** (like model keys), set as the
chaos provider's key in Bifrost through its management API at runtime, and re-applied whenever it may have
been lost (Bifrost forgets runtime changes on restart): at startup and before every live outage drill. The
token is never shown, logged or returned by any API.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto
from app.clients.bifrost_admin import BifrostAdmin, BifrostAdminError
from app.config import Settings
from app.models import GatewaySecret

log = logging.getLogger(__name__)
NAME = "chaos_token"
PROVIDER, KEY_NAME = "chaos", "chaos-key"
DEFAULT_TOKEN = "agentpulse-chaos"
MIN_LENGTH = 24


def stored(db: Session) -> GatewaySecret | None:
    return db.scalar(select(GatewaySecret).where(GatewaySecret.name == NAME))


def effective_token(db: Session, settings: Settings) -> str:
    """The token Bifrost must present: the dashboard-created one if there is one, else ``CHAOS_TOKEN``."""
    row = stored(db)
    if row is not None:
        try:
            return crypto.decrypt(row.value_enc)
        except crypto.SecretError:
            log.warning("the stored chaos token can't be decrypted (APP_SECRET changed): using CHAOS_TOKEN")
    return settings.chaos_token


def matches(db: Session, settings: Settings, presented: str) -> bool:
    expected = effective_token(db, settings)
    return bool(expected) and hmac.compare_digest(presented.encode(), expected.encode())


def status(db: Session, settings: Settings, admin: BifrostAdmin) -> dict:
    row = stored(db)
    token = effective_token(db, settings)
    return {
        "source": "dashboard" if row is not None else "environment",
        "created_at": row.created_at.isoformat(timespec="seconds") if row is not None else None,
        "is_default": row is None and token == DEFAULT_TOKEN,
        "can_manage": admin.configured,
        "length": len(token),
        "note": None
        if admin.configured
        else "Bifrost's management API is locked (no BIFROST_SETUP_TOKEN), so the token can only be set "
        "through the CHAOS_TOKEN setting shared by Bifrost and the backend.",
    }


def apply_to_bifrost(admin: BifrostAdmin, token: str) -> None:
    """Set the chaos provider's key in Bifrost. Raises :class:`BifrostAdminError` (token redacted)."""
    admin.ensure_provider(PROVIDER)
    admin.upsert_key(PROVIDER, KEY_NAME, token)


def set_token(db: Session, settings: Settings, admin: BifrostAdmin, token: str | None) -> dict:
    """Create (random when ``token`` is None) and activate a token. Bifrost is updated first: if it can't be,
    nothing is stored and the old token stays valid, so drills never break half-way."""
    if not admin.configured:
        raise BifrostAdminError("BIFROST_SETUP_TOKEN is not set, so Bifrost's management API is locked")
    if token is not None:
        token = token.strip()
        if len(token) < MIN_LENGTH:
            raise ValueError(f"a chaos token needs at least {MIN_LENGTH} characters")
    new = token or secrets.token_urlsafe(32)
    apply_to_bifrost(admin, new)
    row = stored(db)
    if row is None:
        db.add(GatewaySecret(name=NAME, value_enc=crypto.encrypt(new)))
    else:
        row.value_enc, row.created_at = crypto.encrypt(new), datetime.now(UTC)
    db.commit()
    return status(db, settings, admin)


def reset(db: Session, settings: Settings, admin: BifrostAdmin) -> dict:
    """Forget the dashboard token and go back to ``CHAOS_TOKEN`` (also pushed to Bifrost)."""
    row = stored(db)
    if row is not None:
        if admin.configured:
            apply_to_bifrost(admin, settings.chaos_token)
        db.delete(row)
        db.commit()
    return status(db, settings, admin)


def ensure_applied(db: Session, settings: Settings, admin: BifrostAdmin) -> bool:
    """Re-apply a dashboard token to Bifrost (it forgets runtime keys on restart). True if all is well."""
    row = stored(db)
    if row is None or not admin.configured:
        return True
    try:
        apply_to_bifrost(admin, effective_token(db, settings))
        return True
    except BifrostAdminError as exc:
        log.warning("could not re-apply the chaos token to Bifrost: %s", exc)
        return False
