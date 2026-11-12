"""Freshness Guard authorization: who is calling, and what they may do.

Credentials (all as ``Authorization: Bearer <token>``, OpenAI SDKs send ``api_key`` that way):

* **Guard API keys** ``apg_…``: issued by an admin (``POST /guard/keys``) with a name, scopes, an expiry and a
  rate limit. The secret is shown once and stored only as a SHA-256 hash; keys can be revoked.
* **OIDC ID tokens** (JWT) from your identity provider: the signature is verified against the issuer's JWKS
  (discovered from ``<issuer>/.well-known/openid-configuration``, cached, refreshed for an unknown ``kid``),
  plus ``iss``, ``aud``, ``exp``, ``nbf`` and ``iat`` (60 s leeway). Only asymmetric algorithms are accepted,
  so
  ``none`` and HMAC key-confusion tokens are refused. Scopes come from a claim (``GUARD_OIDC_SCOPES_CLAIM``).
* The **admin access code** (``LIVE_ACCESS_CODE``, header ``X-Access-Code`` or bearer): every scope.

Scopes: ``guard:annotate`` · ``guard:correct`` · ``guard:block`` · ``guard:off`` (plain proxy, no check) ·
``guard:live`` (real models on a public deployment) · ``guard:stats``.

``GUARD_AUTH=required`` refuses calls without a valid credential (401 + ``WWW-Authenticate``); a missing scope
is a 403 ``insufficient_scope``. With ``GUARD_AUTH=open`` (default) anonymous callers keep the open behaviour,
but a presented credential is still verified and its scopes enforced. Every caller has a per-minute rate
limit.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.mode import is_public_deployment
from app.models import GuardKey

SCOPES = ("guard:annotate", "guard:correct", "guard:block", "guard:off", "guard:live", "guard:stats")
MODE_SCOPE = {
    "annotate": "guard:annotate",
    "correct": "guard:correct",
    "block": "guard:block",
    "off": "guard:off",
}
ANONYMOUS_SCOPES = ("guard:annotate", "guard:correct", "guard:block", "guard:off", "guard:stats")
KEY_PREFIX = "apg_"
ASYMMETRIC_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
LEEWAY_S = 60
JWKS_TTL_S = 600


class AuthError(Exception):
    """Authentication or authorization failed. ``status`` is 401, 403 or 429."""

    def __init__(
        self, status: int, message: str, code: str, *, scope: str | None = None, retry_after: int = 0
    ):
        super().__init__(message)
        self.status, self.message, self.code, self.scope, self.retry_after = (
            status,
            message,
            code,
            scope,
            retry_after,
        )

    def headers(self) -> dict[str, str]:
        if self.status == 401:
            return {"WWW-Authenticate": f'Bearer realm="agentpulse-guard", error="{self.code}"'}
        if self.status == 403:
            return {
                "WWW-Authenticate": 'Bearer realm="agentpulse-guard", error="insufficient_scope", '
                f'scope="{self.scope}"'
            }
        if self.status == 429:
            return {"Retry-After": str(self.retry_after)}
        return {}


@dataclass(frozen=True)
class Principal:
    kind: str  # anonymous | key | oidc | admin
    name: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    rpm: int = 60
    key_id: int | None = None
    expires_at: str | None = None

    @property
    def caller(self) -> str:
        return self.kind if self.kind in ("anonymous", "admin") else f"{self.kind}:{self.name}"

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthError(
                403,
                f"this credential ({self.caller}) lacks the scope '{scope}'",
                "insufficient_scope",
                scope=scope,
            )

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "caller": self.caller,
            "scopes": sorted(self.scopes),
            "rpm": self.rpm,
            "expires_at": self.expires_at,
        }


# ------------------------------------------------------------------------------------ API keys
def hash_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_key(
    db: Session, *, name: str, scopes: list[str], expires_in_days: int | None, rpm: int
) -> tuple[GuardKey, str]:
    """Create a key; returns the row and the secret (shown once, never stored)."""
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {list(SCOPES)}; unknown: {bad}")
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    key = GuardKey(
        name=name.strip()[:120],
        key_hash=hash_key(token),
        prefix=token[: len(KEY_PREFIX) + 6],
        scopes=sorted(set(scopes)),
        rpm=rpm,
        created_at=now,
        expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, token


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def key_principal(db: Session, token: str) -> Principal:
    key = db.scalar(select(GuardKey).where(GuardKey.key_hash == hash_key(token)))
    if key is None or not hmac.compare_digest(key.key_hash, hash_key(token)):
        raise AuthError(401, "unknown Guard API key", "invalid_token")
    if key.revoked_at is not None:
        raise AuthError(401, f"the Guard API key '{key.name}' was revoked", "invalid_token")
    expires = _aware(key.expires_at)
    if expires is not None and expires <= datetime.now(UTC):
        raise AuthError(401, f"the Guard API key '{key.name}' expired on {expires.date()}", "invalid_token")
    key.last_used_at = datetime.now(UTC)
    db.commit()
    return Principal(
        "key",
        key.name,
        frozenset(key.scopes or []),
        key.rpm or 60,
        key.id,
        expires.isoformat() if expires else None,
    )


# ------------------------------------------------------------------------------------ OIDC ID tokens
class JWKSCache:
    """JWKS per issuer, discovered via OpenID configuration; refreshed after a TTL or for an unknown kid."""

    def __init__(self, fetch=None):
        self._fetch = fetch or self._http_get
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _http_get(url: str) -> dict[str, Any]:
        res = httpx.get(url, timeout=10.0, follow_redirects=False)
        res.raise_for_status()
        return res.json()

    def jwks(self, issuer: str, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            hit = self._cache.get(issuer)
            if hit and not refresh and time.monotonic() - hit[0] < JWKS_TTL_S:
                return hit[1]
        conf = self._fetch(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        if conf.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
            raise AuthError(
                401, "the identity provider's configuration names a different issuer", "invalid_token"
            )
        keys = self._fetch(conf["jwks_uri"])
        with self._lock:
            self._cache[issuer] = (time.monotonic(), keys)
        return keys

    def key_for(self, issuer: str, kid: str | None):
        for refresh in (False, True):
            for jwk in self.jwks(issuer, refresh=refresh).get("keys", []):
                if kid is None or jwk.get("kid") == kid:
                    return jwt.PyJWK(jwk)
        raise AuthError(401, f"no signing key '{kid}' at the identity provider", "invalid_token")


jwks_cache = JWKSCache()


def oidc_principal(token: str, settings: Settings, cache: JWKSCache | None = None) -> Principal:
    if not settings.guard_oidc_issuer or not settings.guard_oidc_audience:
        raise AuthError(
            401, "ID tokens are not enabled (set GUARD_OIDC_ISSUER and GUARD_OIDC_AUDIENCE)", "invalid_token"
        )
    cache = cache or jwks_cache
    try:
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg not in ASYMMETRIC_ALGS:
            raise AuthError(401, f"ID token algorithm '{alg}' is not allowed", "invalid_token")
        signing_key = cache.key_for(settings.guard_oidc_issuer, header.get("kid"))
        claims = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=[alg],
            audience=settings.guard_oidc_audience,
            issuer=settings.guard_oidc_issuer,
            leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except AuthError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(401, "the ID token has expired", "invalid_token") from exc
    except (jwt.InvalidTokenError, httpx.HTTPError, KeyError, ValueError) as exc:
        raise AuthError(401, f"invalid ID token: {exc}"[:200], "invalid_token") from exc
    raw = claims.get(settings.guard_oidc_scopes_claim)
    values = raw.split() if isinstance(raw, str) else [str(v) for v in raw or []]
    scopes = {v for v in values if v in SCOPES} or set(settings.guard_oidc_default_scopes.split())
    name = str(claims.get("email") or claims.get("preferred_username") or claims["sub"])[:120]
    exp = datetime.fromtimestamp(int(claims["exp"]), UTC).isoformat()
    return Principal(
        "oidc", name, frozenset(s for s in scopes if s in SCOPES), settings.guard_oidc_rpm, None, exp
    )


# ------------------------------------------------------------------------------------ resolution
def _looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("eyJ")


def resolve(
    db: Session, settings: Settings, authorization: str | None, x_access_code: str | None
) -> Principal:
    """The caller, from the access-code header or the bearer token."""
    code = settings.live_access_code
    if x_access_code:
        if code and hmac.compare_digest(x_access_code.encode(), code.encode()):
            return Principal("admin", "admin", frozenset(SCOPES), rpm=600)
        raise AuthError(401, "invalid access code", "invalid_token")
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if code and token and hmac.compare_digest(token.encode(), code.encode()):
        return Principal("admin", "admin", frozenset(SCOPES), rpm=600)
    if token.startswith(KEY_PREFIX):
        return key_principal(db, token)
    if _looks_like_jwt(token):
        return oidc_principal(token, settings)
    if settings.guard_auth == "required":
        raise AuthError(
            401, "a Guard API key or an ID token is required (Authorization: Bearer …)", "invalid_request"
        )
    if (
        is_public_deployment(settings)
        and token
        and token.lower() not in ("mock", "none", "not-needed", "sk-mock")
    ):
        raise AuthError(401, "invalid access code", "invalid_token")
    return Principal("anonymous", "anonymous", frozenset(ANONYMOUS_SCOPES), rpm=settings.guard_anonymous_rpm)


def wants_live(principal: Principal, settings: Settings) -> bool:
    """Live models: always without MOCK_MODE (your own machine); otherwise only with ``guard:live``."""
    return not settings.mock_mode or "guard:live" in principal.scopes


# ------------------------------------------------------------------------------------ rate limits
class RateLimiter:
    """Sliding one-minute window per caller (in memory, per process)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(self, principal: Principal, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(principal.caller, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= principal.rpm:
                wait = int(60 - (now - hits[0])) + 1
                raise AuthError(
                    429,
                    f"rate limit of {principal.rpm} requests per minute reached for " f"{principal.caller}",
                    "rate_limited",
                    retry_after=wait,
                )
            hits.append(now)


limiter = RateLimiter()
