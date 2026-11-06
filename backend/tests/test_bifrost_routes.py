"""Hosted providers registered in Bifrost at runtime (v2.3), against a fake Bifrost that behaves like the
real one did in the spike: setup-token auth, 409 on existing providers, key validation on add, no config
store → 500, and registrations forgotten on restart."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy.orm import Session

from app import connection_routes
from app.bench.pricing import Price, PriceBook
from app.clients import llm as llm_module
from app.clients.bifrost_admin import BifrostAdmin, BifrostAdminError, key_name
from app.clients.llm import BifrostClient
from app.clients.mock_llm import MockLLMClient
from app.clients.router import BifrostRoute, ConnectionRegistry, ModelRouter
from app.db import get_sessionmaker
from app.models import ModelConnection
from app.seed import seed_agents

TOKEN = "setup-token-xyz"
GOOD = "good-key-1234567890"
BAD = "AIzaBAD-0000000000"
BUILTIN = ("groq", "openrouter", "ollama")


class FakeBifrost:
    def __init__(self, config_store: bool = True):
        self.config_store = config_store
        self.providers: dict[str, dict[str, tuple[str, str]]] = {
            p: {f"{p}-primary": ("id-" + p, "cfg")} for p in BUILTIN
        }
        self.chats: list[str] = []
        self.registrations = 0

    def restart(self):  # source_of_truth=config.json: runtime additions are dropped
        self.providers = {p: v for p, v in self.providers.items() if p in BUILTIN}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, method = req.url.path, req.method
        if path.startswith("/api/"):
            tok = req.headers.get("x-bifrost-setup-token")
            if tok is None:
                return httpx.Response(401, json={"error": {"message": "setup token required"}})
            if tok != TOKEN:
                return httpx.Response(403, json={"error": {"message": "invalid setup token"}})
            return self._admin(method, path.split("/")[2:], json.loads(req.content) if req.content else None)
        if path == "/v1/models":
            return httpx.Response(
                200, json={"data": [{"id": f"{p}/model-a"} for p, k in self.providers.items() if k]}
            )
        if path == "/v1/chat/completions":
            model = json.loads(req.content)["model"]
            provider = model.split("/", 1)[0]
            self.chats.append(model)
            if provider not in self.providers:
                return httpx.Response(
                    400,
                    json={"error": {"message": f"failed to get config for provider {provider}: not found"}},
                )
            keys = self.providers[provider]
            if not keys:
                return httpx.Response(
                    400, json={"error": {"message": f"no keys found for provider {provider}"}}
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            )
        return httpx.Response(404)

    def _admin(self, method, parts, body):
        # parts: ["providers"], ["providers", p], ["providers", p, "keys"], ["providers", p, "keys", id]
        if parts == ["providers"] and method == "GET":
            return httpx.Response(200, json={"providers": [{"name": p} for p in self.providers]})
        if parts == ["providers"] and method == "POST":
            p = body["provider"]
            if p in self.providers:
                return httpx.Response(409, json={"error": {"message": "provider already exists"}})
            if not self.config_store:
                return httpx.Response(
                    500,
                    json={
                        "error": {
                            "message": "Failed to initialize provider after add: config store not found"
                        }
                    },
                )
            self.providers[p] = {}
            return httpx.Response(200, json={"name": p})
        p = parts[1]
        if p not in self.providers:
            return httpx.Response(404, json={"error": {"message": "Provider not found: not found"}})
        keys = self.providers[p]
        if len(parts) == 2 and method == "DELETE":
            del self.providers[p]
            return httpx.Response(200, json={})
        if parts[2:] == ["keys"] and method == "GET":
            return httpx.Response(
                200, json={"keys": [{"id": i, "name": n} for n, (i, _) in keys.items()] or None}
            )
        if parts[2:] == ["keys"] and method == "POST":
            kid = uuid.uuid4().hex
            keys[body["name"]] = (kid, body["value"])
            self.registrations += 1
            ok = body["value"].startswith("good")
            return httpx.Response(
                200,
                json={
                    "id": kid,
                    "name": body["name"],
                    "status": "success" if ok else "list_models_failed",
                    "description": None
                    if ok
                    else f"API key not valid ({body['value']}). Please pass a valid API key.",
                },
            )
        if len(parts) == 4 and method == "DELETE":
            for n, (i, _) in list(keys.items()):
                if i == parts[3]:
                    del keys[n]
            return httpx.Response(200, json={})
        return httpx.Response(405)


@pytest.fixture()
def fake(monkeypatch):
    bifrost = FakeBifrost()
    transport = httpx.MockTransport(bifrost)
    admin = BifrostAdmin("http://bifrost:8080", TOKEN, transport=transport)
    monkeypatch.setattr(connection_routes, "get_admin", lambda settings=None: admin)
    client = BifrostClient("http://bifrost:8080", transport=transport)
    import app.clients as clients_pkg

    monkeypatch.setattr(clients_pkg, "get_bifrost", lambda: client)
    bifrost.client = client
    return bifrost


@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
    return client


def add(api, name="My Gemini", provider="gemini", key=GOOD, via_bifrost=True, **kw):
    body = {
        "name": name,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["model-a"],
        "api_key": key,
        "provider": provider,
        "via_bifrost": via_bifrost,
        **kw,
    }
    res = api.post("/connections", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# ------------------------------------------------------------------------------------ route decisions
def test_hosted_provider_is_registered_in_bifrost(api, fake):
    c = add(api)
    assert c["kind"] == "hosted" and c["route"] == "bifrost" and c["provider"] == "gemini"
    assert (
        "registered in Bifrost" in c["route_reason"] and c["bifrost_key_status"] == "key accepted by Bifrost"
    )
    assert fake.providers["gemini"][key_name(c["slug"])][1] == GOOD  # the real key reached Bifrost
    assert GOOD not in json.dumps(c)


def test_bifrost_key_validation_is_reported_without_the_key(api, fake):
    c = add(api, key=BAD)
    assert c["route"] == "bifrost" and "API key not valid" in c["bifrost_key_status"]
    assert BAD not in json.dumps(c) and "***" in c["bifrost_key_status"]


@pytest.mark.parametrize("provider", BUILTIN)
def test_builtin_providers_stay_direct_so_keys_never_mix(api, fake, provider):
    c = add(api, name=f"Mine {provider}", provider=provider)
    assert c["route"] == "direct" and "would mix it with yours" in c["route_reason"]
    assert list(fake.providers[provider]) == [f"{provider}-primary"]  # untouched


def test_second_connection_to_same_provider_is_direct(api, fake):
    add(api)
    second = add(api, name="Other Gemini")
    assert second["route"] == "direct" and "already routes gemini" in second["route_reason"]
    assert len(fake.providers["gemini"]) == 1


def test_no_setup_token_means_direct(api, monkeypatch):
    monkeypatch.setattr(connection_routes, "get_admin", lambda settings=None: BifrostAdmin("http://b", ""))
    c = add(api)
    assert c["route"] == "direct" and "BIFROST_SETUP_TOKEN" in c["route_reason"]


def test_no_config_store_falls_back_to_direct(api, fake):
    fake.config_store = False
    c = add(api)
    assert c["route"] == "direct" and "without a config store" in c["route_reason"]


def test_plain_and_unknown_providers(api, fake):
    plain = add(api, name="Plain", provider="gemini", via_bifrost=False)
    assert (
        plain["kind"] == "openai_compatible" and plain["route"] == "direct" and plain["route_reason"] is None
    )
    res = api.post(
        "/connections",
        json={"name": "X", "base_url": "https://x.example/v1", "provider": "acme", "via_bifrost": True},
    )
    assert res.status_code == 422
    no_key = add(api, name="Keyless", key=None)
    assert no_key["route"] == "direct" and "no API key" in no_key["route_reason"]


def test_delete_and_switching_off_unregister(api, fake):
    c = add(api)
    assert api.delete(f"/connections/{c['id']}").status_code == 204
    assert "gemini" not in fake.providers  # our key removed, then the empty provider we added
    c = add(api, name="Again")
    body = {
        "name": "Again",
        "base_url": c["base_url"],
        "models": ["model-a"],
        "provider": "gemini",
        "via_bifrost": False,
    }
    off = api.put(f"/connections/{c['id']}", json=body).json()
    assert off["route"] == "direct" and "gemini" not in fake.providers
    on = api.put(f"/connections/{c['id']}", json={**body, "via_bifrost": True}).json()
    assert on["route"] == "bifrost" and key_name(c["slug"]) in fake.providers["gemini"]
    cleared = api.put(
        f"/connections/{c['id']}", json={**body, "via_bifrost": True, "clear_api_key": True}
    ).json()
    assert cleared["route"] == "direct" and "gemini" not in fake.providers


# ------------------------------------------------------------------------------------ calls & self-healing
def test_router_sends_native_ids_through_bifrost_and_heals_after_restart(api, fake):
    c = add(api)
    router = ModelRouter(
        MockLLMClient(latency_scale=0), ConnectionRegistry(get_sessionmaker(), bifrost=fake.client)
    )
    res = router.chat(f"custom/{c['slug']}/model-a", [{"role": "user", "content": "hi"}])
    assert res.content == "pong" and fake.chats[-1] == "gemini/model-a"
    assert res.model == f"custom/{c['slug']}/model-a"

    fake.restart()  # Bifrost forgets the provider
    before = fake.registrations
    res = router.chat(f"custom/{c['slug']}/model-a", [{"role": "user", "content": "again"}])
    assert res.content == "pong" and fake.registrations == before + 1  # re-registered, then retried
    assert fake.chats[-2:] == ["gemini/model-a", "gemini/model-a"]


def test_heal_gives_up_cleanly_when_registration_fails(api, fake):
    c = add(api)
    router = ModelRouter(
        MockLLMClient(latency_scale=0), ConnectionRegistry(get_sessionmaker(), bifrost=fake.client)
    )
    fake.restart()
    fake.config_store = False
    with pytest.raises(llm_module.LLMError, match="failed to get config for provider"):
        router.chat(f"custom/{c['slug']}/model-a", [{"role": "user", "content": "hi"}])


def test_saved_connection_test_goes_through_bifrost(api, fake):
    c = add(api)
    fake.restart()  # the test re-registers first
    res = api.post(f"/connections/{c['id']}/test").json()
    assert res["ok"] and res["models"] == ["model-a"] and res["base_url"] == "bifrost → gemini"
    assert res["chat_reply"] == "pong" and fake.chats[-1] == "gemini/model-a"


def test_bifrost_route_lists_only_its_provider_models(fake):
    route = BifrostRoute(fake.client, "groq", heal=lambda: False)
    assert route.list_models() == ["model-a"]


def test_pricing_uses_bifrost_catalogue_for_routed_connections(api, fake):
    c = add(api)
    book = PriceBook(None)
    book._catalogue = {"gemini/model-a": Price(1e-6, 2e-6, "bifrost")}
    cost, source = book.cost(f"custom/{c['slug']}/model-a", 1000, 1000)
    assert cost == pytest.approx(0.003) and source == "bifrost (via connection)"


# ------------------------------------------------------------------------------------ admin client
def test_admin_auth_errors_and_secret_scrubbing():
    bifrost = FakeBifrost()
    wrong = BifrostAdmin("http://b", "nope", transport=httpx.MockTransport(bifrost))
    with pytest.raises(BifrostAdminError, match="setup token rejected"):
        wrong.providers()
    with pytest.raises(BifrostAdminError, match="BIFROST_SETUP_TOKEN"):
        BifrostAdmin("http://b", "").providers()
    admin = BifrostAdmin("http://b", TOKEN, transport=httpx.MockTransport(bifrost))
    admin.ensure_provider("openai")
    admin.ensure_provider("openai")  # 409 is fine
    result = admin.upsert_key("openai", "k", BAD)
    assert BAD not in str(result) and result["status"] == "list_models_failed"
    admin.upsert_key("openai", "k", GOOD)  # replace, not duplicate
    assert list(bifrost.providers["openai"]) == ["k"]
    assert admin.delete_key("openai", "k") and admin.delete_provider_if_unused("openai")
    assert not admin.delete_provider_if_unused("groq")  # never a config.json provider


def test_routes_survive_db_round_trip(api, fake, db_engine):
    c = add(api)
    with Session(db_engine) as db:
        row = db.get(ModelConnection, c["id"])
        assert row.route == "bifrost" and row.provider == "gemini" and row.bifrost_key_status


def test_unexpected_registration_error_still_saves_the_connection(api, fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("surprise")

    monkeypatch.setattr(connection_routes, "register", boom)
    c = add(api)
    assert c["route"] == "direct" and "RuntimeError: surprise" in c["route_reason"]
