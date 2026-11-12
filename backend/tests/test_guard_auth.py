"""Freshness Guard authorization: API keys, OIDC ID tokens, scopes, rate limits, admin-only key management."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from openai import AuthenticationError, OpenAI, PermissionDeniedError
from sqlalchemy.orm import Session

from app.clients import mock_llm
from app.config import Settings, get_settings
from app.guard import auth
from app.main import app
from app.models import FreshnessCheck, GuardKey

MODEL = "groq/openai/gpt-oss-120b"
STALE = "The current UK Prime Minister is Rishi Sunak."
Q = [{"role": "user", "content": "Who is the current UK prime minister?"}]
ISSUER = "https://idp.example.test"
AUD = "agentpulse-guard"


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    monkeypatch.setattr(auth, "limiter", auth.RateLimiter())
    monkeypatch.setattr(
        mock_llm,
        "mock_fact_answer",
        lambda prompt, p, rng: STALE if "prime minister" in prompt.lower() else None,
    )


def _use(settings: Settings):
    app.dependency_overrides[get_settings] = lambda: settings


def _sdk(client, key: str) -> OpenAI:
    return OpenAI(base_url="http://testserver/guard/v1", api_key=key, http_client=client, max_retries=0)


# ------------------------------------------------------------------------------------ resolution rules
def test_resolution_rules(db_engine):
    with Session(db_engine) as db:
        local = Settings(mock_mode=False)
        anon = auth.resolve(db, local, "Bearer sk-anything", None)
        assert anon.kind == "anonymous" and "guard:live" not in anon.scopes and auth.wants_live(anon, local)
        public = Settings(mock_mode=True, live_access_code="secret")
        admin = auth.resolve(db, public, "Bearer secret", None)
        assert (
            admin.kind == "admin"
            and admin.scopes == frozenset(auth.SCOPES)
            and auth.wants_live(admin, public)
        )
        assert auth.resolve(db, public, None, "secret").kind == "admin"
        assert not auth.wants_live(auth.resolve(db, public, "Bearer mock", None), public)
        for bad in (("Bearer wrong", None), (None, "wrong")):
            with pytest.raises(auth.AuthError) as exc:
                auth.resolve(db, public, *bad)
            assert exc.value.status == 401
        strict = Settings(mock_mode=False, guard_auth="required")
        with pytest.raises(auth.AuthError) as exc:
            auth.resolve(db, strict, None, None)
        assert exc.value.status == 401 and "WWW-Authenticate" in exc.value.headers()


def test_required_mode_refuses_anonymous_calls_with_www_authenticate(client):
    _use(Settings(mock_mode=True, guard_auth="required"))
    res = client.post("/guard/v1/chat/completions", json={"model": MODEL, "messages": Q})
    assert res.status_code == 401 and res.json()["error"]["type"] == "authentication_error"
    assert res.headers["WWW-Authenticate"].startswith('Bearer realm="agentpulse-guard"')
    assert client.get("/guard/stats").status_code == 401


# ------------------------------------------------------------------------------------ API keys
def test_keys_are_shown_once_stored_hashed_and_scoped(client, db_engine):
    _use(Settings(mock_mode=True, guard_auth="required"))
    made = client.post(
        "/guard/keys", json={"name": "support-bot", "scopes": ["guard:annotate"], "rpm": 30}
    ).json()
    secret = made["key"]
    assert secret.startswith("apg_") and len(secret) > 40 and made["prefix"] == secret[:10]
    with Session(db_engine) as db:
        row = db.get(GuardKey, made["id"])
        assert row.key_hash == auth.hash_key(secret) and secret not in str(row.__dict__)
    assert all("key" not in k for k in client.get("/guard/keys").json())

    sdk = _sdk(client, secret)
    resp = sdk.chat.completions.create(model=MODEL, messages=Q)
    assert resp.model_extra["agentpulse"]["caller"] == "key:support-bot"
    with Session(db_engine) as db:
        check = db.get(FreshnessCheck, resp.model_extra["agentpulse"]["check_id"])
        assert check.caller == "key:support-bot"
        assert db.get(GuardKey, made["id"]).last_used_at is not None
    with pytest.raises(PermissionDeniedError) as exc:
        sdk.chat.completions.create(model=MODEL, messages=Q, extra_headers={"X-AgentPulse-Guard": "block"})
    assert (
        exc.value.status_code == 403
        and 'scope="guard:block"' in exc.value.response.headers["WWW-Authenticate"]
    )
    assert client.get("/guard/stats", headers={"Authorization": f"Bearer {secret}"}).status_code == 403
    who = client.get("/guard/whoami", headers={"Authorization": f"Bearer {secret}"}).json()
    assert who["caller"] == "key:support-bot" and who["scopes"] == ["guard:annotate"] and who["live"] is False

    assert client.delete(f"/guard/keys/{made['id']}").status_code == 204
    with pytest.raises(AuthenticationError, match="revoked"):
        sdk.chat.completions.create(model=MODEL, messages=Q)
    assert next(k for k in client.get("/guard/keys").json() if k["id"] == made["id"])["status"] == "revoked"
    with pytest.raises(AuthenticationError, match="unknown"):
        _sdk(client, "apg_" + "x" * 43).chat.completions.create(model=MODEL, messages=Q)


def test_expired_keys_and_bad_scopes(client, db_engine):
    _use(Settings(mock_mode=True))
    made = client.post(
        "/guard/keys", json={"name": "old", "scopes": ["guard:annotate"], "expires_in_days": 1}
    ).json()
    with Session(db_engine) as db:
        db.get(GuardKey, made["id"]).expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    res = client.post(
        "/guard/v1/chat/completions",
        json={"model": MODEL, "messages": Q},
        headers={"Authorization": f"Bearer {made['key']}"},
    )
    assert res.status_code == 401 and "expired" in res.json()["error"]["message"]
    assert client.post("/guard/keys", json={"name": "x", "scopes": ["guard:root"]}).status_code == 422
    assert client.post("/guard/keys", json={"name": "x", "scopes": []}).status_code == 422


def test_key_management_needs_the_access_code_when_one_exists(client):
    _use(Settings(mock_mode=False, live_access_code="secret"))
    assert client.post("/guard/keys", json={"name": "k"}).status_code == 403
    assert client.get("/guard/keys").status_code == 403
    made = client.post(
        "/guard/keys",
        json={"name": "k", "scopes": ["guard:live", "guard:annotate"]},
        headers={"X-Access-Code": "secret"},
    )
    assert made.status_code == 201
    assert client.delete(f"/guard/keys/{made.json()['id']}").status_code == 403


def test_a_live_scoped_key_unlocks_live_mode_on_the_public_demo(db_engine):
    public = Settings(mock_mode=True, live_access_code="secret")
    with Session(db_engine) as db:
        key, secret = auth.issue_key(
            db, name="partner", scopes=["guard:annotate", "guard:live"], expires_in_days=30, rpm=5
        )
        p = auth.resolve(db, public, f"Bearer {secret}", None)
    assert p.kind == "key" and auth.wants_live(p, public)


def test_rate_limit_per_caller(client):
    _use(Settings(mock_mode=True))
    secret = client.post("/guard/keys", json={"name": "tiny", "scopes": ["guard:annotate"], "rpm": 2}).json()[
        "key"
    ]
    h = {"Authorization": f"Bearer {secret}"}
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Write a haiku"}]}
    assert [
        client.post("/guard/v1/chat/completions", json=body, headers=h).status_code for _ in range(2)
    ] == [200, 200]
    res = client.post("/guard/v1/chat/completions", json=body, headers=h)
    assert res.status_code == 429 and int(res.headers["Retry-After"]) > 0
    assert client.post("/guard/v1/chat/completions", json=body).status_code == 200  # others unaffected

    limiter = auth.RateLimiter()
    p = auth.Principal("key", "w", frozenset(), rpm=1)
    limiter.check(p, now=0.0)
    with pytest.raises(auth.AuthError):
        limiter.check(p, now=30.0)
    limiter.check(p, now=61.0)  # the window slides


# ------------------------------------------------------------------------------------ OIDC ID tokens
@pytest.fixture()
def idp(monkeypatch):
    """A fake identity provider: an RSA key, its JWKS and discovery document."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {
        "kid": "k1",
        "use": "sig",
        "alg": "RS256",
    }
    docs = {
        f"{ISSUER}/.well-known/openid-configuration": {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks"},
        f"{ISSUER}/jwks": {"keys": [jwk]},
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return docs[url]

    monkeypatch.setattr(auth, "jwks_cache", auth.JWKSCache(fetch=fetch))

    def token(claims=None, *, kid="k1", alg="RS256", signer=key, **overrides):
        now = int(time.time())
        payload = (
            {
                "iss": ISSUER,
                "aud": AUD,
                "sub": "user-1",
                "email": "ana@example.test",
                "iat": now,
                "exp": now + 600,
                "scope": "guard:annotate guard:correct",
            }
            | (claims or {})
            | overrides
        )
        return jwt.encode(payload, signer, algorithm=alg, headers={"kid": kid})

    return type("IdP", (), {"key": key, "token": staticmethod(token), "docs": docs, "fetched": fetched})


OIDC = Settings(mock_mode=True, guard_oidc_issuer=ISSUER, guard_oidc_audience=AUD)


def test_id_token_grants_its_scopes(client, idp, db_engine):
    _use(OIDC)
    sdk = _sdk(client, idp.token())
    resp = sdk.chat.completions.create(
        model=MODEL, messages=Q, extra_headers={"X-AgentPulse-Guard": "correct"}
    )
    info = resp.model_extra["agentpulse"]
    assert info["caller"] == "oidc:ana@example.test" and info["action"] == "corrected"
    with pytest.raises(PermissionDeniedError):
        sdk.chat.completions.create(model=MODEL, messages=Q, extra_headers={"X-AgentPulse-Guard": "block"})
    assert idp.fetched.count(f"{ISSUER}/jwks") == 1  # JWKS cached


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda idp: idp.token(exp=int(time.time()) - 3600), "expired"),
        (lambda idp: idp.token(aud="someone-else"), "audience"),
        (lambda idp: idp.token(iss="https://evil.test"), "issuer"),
        (lambda idp: idp.token(kid="unknown"), "no signing key"),
        (
            lambda idp: idp.token(signer=rsa.generate_private_key(public_exponent=65537, key_size=2048)),
            "signature",
        ),
        (
            lambda idp: jwt.encode(
                {"iss": ISSUER, "aud": AUD, "sub": "x", "exp": 9_999_999_999, "iat": 0},
                None,
                algorithm="none",
            ),
            "algorithm",
        ),
        (
            lambda idp: jwt.encode(
                {"iss": ISSUER, "aud": AUD, "sub": "x", "exp": 9_999_999_999, "iat": 0},
                "s" * 32,
                algorithm="HS256",
                headers={"kid": "k1"},
            ),
            "algorithm 'HS256'",
        ),
    ],
)
def test_bad_id_tokens_are_refused(idp, make, why):
    with pytest.raises(auth.AuthError) as exc:
        auth.oidc_principal(make(idp), OIDC)
    assert exc.value.status == 401 and why.lower() in exc.value.message.lower(), exc.value.message


def test_id_tokens_need_oidc_configured_and_an_honest_issuer(idp):
    with pytest.raises(auth.AuthError, match="not enabled"):
        auth.oidc_principal(idp.token(), Settings(mock_mode=True))
    idp.docs[f"{ISSUER}/.well-known/openid-configuration"]["issuer"] = "https://other.test"
    with pytest.raises(auth.AuthError, match="different issuer"):
        auth.oidc_principal(idp.token(), OIDC)


def test_roles_claim_and_default_scopes(idp):
    roles = Settings(
        mock_mode=True, guard_oidc_issuer=ISSUER, guard_oidc_audience=AUD, guard_oidc_scopes_claim="roles"
    )
    p = auth.oidc_principal(idp.token({"roles": ["guard:block", "admin", "guard:stats"]}), roles)
    assert p.scopes == frozenset({"guard:block", "guard:stats"})
    plain = auth.oidc_principal(idp.token(scope="openid email"), OIDC)
    assert plain.scopes == frozenset({"guard:annotate"}) and plain.kind == "oidc"
