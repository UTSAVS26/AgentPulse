"""The outage-drill token created on the dashboard: stored encrypted, pushed to Bifrost, never exposed."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app import chaos_token
from app.clients.bifrost_admin import BifrostAdmin, BifrostAdminError
from app.config import Settings, get_settings
from app.connection_routes import get_admin
from app.main import app
from app.models import GatewaySecret

SETTINGS = Settings(mock_mode=True, chaos_token="env-token-env-token-env-token")


class FakeBifrost:
    """A minimal stand-in for Bifrost's management API: providers and their keys."""

    def __init__(self, fail: bool = False):
        self.keys: dict[str, dict[str, str]] = {"chaos": {"chaos-key": "env-token-env-token-env-token"}}
        self.fail, self.calls = fail, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.fail:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        if path == "/api/providers" and request.method == "POST":
            return httpx.Response(201, json={})
        parts = path.strip("/").split("/")  # api providers <p> keys [id]
        provider = parts[2]
        if len(parts) == 4 and request.method == "GET":
            return httpx.Response(
                200, json={"keys": [{"name": n, "id": n} for n in self.keys.get(provider, {})]}
            )
        if len(parts) == 4 and request.method == "POST":
            body = json.loads(request.content)
            self.keys.setdefault(provider, {})[body["name"]] = body["value"]
            return httpx.Response(201, json={"id": body["name"], "status": "success"})
        if len(parts) == 5 and request.method == "DELETE":
            self.keys.get(provider, {}).pop(parts[4], None)
            return httpx.Response(204)
        return httpx.Response(404, json={})

    def admin(self) -> BifrostAdmin:
        return BifrostAdmin("http://bifrost:8080", "setup", transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def api(client, db_engine):
    fake = FakeBifrost()
    admin = fake.admin()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    import app.routers.chaos as chaos_router

    chaos_router.get_admin = lambda settings=None: admin
    yield client, fake
    chaos_router.get_admin = get_admin


def test_default_status_and_the_endpoint_accepts_the_environment_token(api):
    client, _ = api
    st = client.get("/chaos/token").json()
    assert st["source"] == "environment" and st["can_manage"] is True and st["created_at"] is None
    body = {"model": "r99/groq/x", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/chaos/v1/chat/completions", json=body).status_code == 401
    ok = client.post(
        "/chaos/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {SETTINGS.chaos_token}"}
    )
    assert ok.status_code in (200, 502, 503)  # authorised (the upstream is not running here)


def test_generated_token_is_stored_encrypted_pushed_to_bifrost_and_replaces_the_old_one(api, db_engine):
    client, fake = api
    st = client.post("/chaos/token").json()
    assert st["source"] == "dashboard" and st["created_at"] and st["length"] >= 40
    new = fake.keys["chaos"]["chaos-key"]
    assert new != SETTINGS.chaos_token and len(new) >= 40
    with Session(db_engine) as db:
        row = db.query(GatewaySecret).one()
        assert new not in row.value_enc and chaos_token.effective_token(db, SETTINGS) == new
    assert new not in json.dumps(client.get("/chaos/token").json())
    body = {"model": "r99/groq/x", "messages": [{"role": "user", "content": "hi"}]}
    old = client.post(
        "/chaos/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {SETTINGS.chaos_token}"}
    )
    assert old.status_code == 401  # the old token is dead
    fresh = client.post("/chaos/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {new}"})
    assert fresh.status_code != 401


def test_your_own_token_and_validation(api):
    client, fake = api
    assert client.post("/chaos/token", json={"token": "short"}).status_code == 422
    mine = "my-very-own-chaos-token-0123456789"
    assert client.post("/chaos/token", json={"token": mine}).status_code == 200
    assert fake.keys["chaos"]["chaos-key"] == mine


def test_nothing_changes_when_bifrost_cannot_be_updated(api, db_engine):
    client, fake = api
    fake.fail = True
    res = client.post("/chaos/token")
    assert res.status_code == 502 and "previous token is still active" in res.json()["detail"]
    with Session(db_engine) as db:
        assert (
            db.query(GatewaySecret).count() == 0
            and chaos_token.effective_token(db, SETTINGS) == SETTINGS.chaos_token
        )


def test_locked_bifrost_cannot_manage_the_token(client, db_engine):
    import app.routers.chaos as chaos_router

    app.dependency_overrides[get_settings] = lambda: SETTINGS
    chaos_router.get_admin = lambda settings=None: BifrostAdmin("http://bifrost:8080", "")
    try:
        assert client.get("/chaos/token").json()["can_manage"] is False
        res = client.post("/chaos/token")
        assert res.status_code == 409 and "locked" in res.json()["detail"]
    finally:
        chaos_router.get_admin = get_admin


def test_reset_goes_back_to_the_environment_token(api, db_engine):
    client, fake = api
    client.post("/chaos/token")
    st = client.delete("/chaos/token").json()
    assert st["source"] == "environment" and fake.keys["chaos"]["chaos-key"] == SETTINGS.chaos_token
    with Session(db_engine) as db:
        assert db.query(GatewaySecret).count() == 0


def test_token_is_reapplied_after_a_bifrost_restart(api, db_engine):
    client, fake = api
    client.post("/chaos/token")
    wanted = fake.keys["chaos"]["chaos-key"]
    fake.keys["chaos"]["chaos-key"] = SETTINGS.chaos_token  # Bifrost restarted: back to config.json's value
    with Session(db_engine) as db:
        assert chaos_token.ensure_applied(db, SETTINGS, fake.admin()) is True
    assert fake.keys["chaos"]["chaos-key"] == wanted
    fake.fail = True
    with Session(db_engine) as db:
        assert chaos_token.ensure_applied(db, SETTINGS, fake.admin()) is False  # logged, never raises


def test_access_code_is_needed_on_a_public_deployment(client, db_engine):
    import app.routers.chaos as chaos_router

    fake = FakeBifrost()
    public = Settings(mock_mode=True, live_access_code="secret", chaos_token="x" * 30)
    app.dependency_overrides[get_settings] = lambda: public
    chaos_router.get_admin = lambda settings=None: fake.admin()
    try:
        assert client.post("/chaos/token").status_code == 403
        assert client.post("/chaos/token", headers={"X-Access-Code": "secret"}).status_code == 200
        assert client.delete("/chaos/token").status_code == 403
    finally:
        chaos_router.get_admin = get_admin


def test_set_token_raises_for_a_locked_admin_directly(db_engine):
    with Session(db_engine) as db, pytest.raises(BifrostAdminError):
        chaos_token.set_token(db, SETTINGS, BifrostAdmin("http://bifrost:8080", ""), None)
