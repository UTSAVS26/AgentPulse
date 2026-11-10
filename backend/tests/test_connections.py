"""Bring-your-own-model connections: crypto, URL guard, API (keys never leak), probing, routing, pricing."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app import crypto, net_guard
from app.bench.engine import BenchmarkEngine, InlineExecutor
from app.bench.pricing import PriceBook
from app.clients import router as model_router
from app.clients.llm import LLMError
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.clients.router import ConnectionRegistry, ModelRouter, custom_model_id, parse_custom
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.main import app
from app.models import BenchmarkRun, ModelConnection, TaskExecution, TaskSuite
from app.seed import seed_agents, seed_suites

KEY = "sk-test-SECRET-1234567890abcd"
BASE = "https://llm.example.com/v1"


class FakeServer:
    """An OpenAI-compatible endpoint: /models, /chat/completions (plain answers and tool calls)."""

    def __init__(self, require_key: str | None = None, tools: bool = True):
        self.require_key = require_key
        self.tools = tools
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization", "")
        if self.require_key and auth != f"Bearer {self.require_key}":
            return httpx.Response(
                401,
                json={"error": {"message": f"Incorrect API key provided: {auth.removeprefix('Bearer ')}"}},
            )
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "tiny"}, {"id": "big"}]})
        body = json.loads(request.content)
        if body["model"] == "nope":
            return httpx.Response(404, json={"error": {"message": "model 'nope' not found"}})
        if body.get("tools") and self.tools and not any(m["role"] == "tool" for m in body["messages"]):
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"expression": "17 * 23"}'},
                    }
                ],
            }
        else:
            last = body["messages"][-1]
            content = f"The answer is {last['content']}." if last["role"] == "tool" else "pong"
            message = {"role": "assistant", "content": content}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )


@pytest.fixture()
def server(monkeypatch):
    fake = FakeServer(require_key=KEY)
    monkeypatch.setattr(model_router, "default_transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    return client


def create(api, **kw):
    body = {"name": "My endpoint", "base_url": BASE, "models": ["tiny"], "api_key": KEY, **kw}
    res = api.post("/connections", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# ------------------------------------------------------------------------------------ crypto
def test_encrypt_round_trip_and_mask(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: Settings(app_secret="one"))
    token = crypto.encrypt(KEY)
    assert KEY not in token and crypto.decrypt(token) == KEY
    assert crypto.mask(KEY) == "sk-…abcd" and crypto.mask("abc") == "…bc"
    monkeypatch.setattr(crypto, "get_settings", lambda: Settings(app_secret="two"))
    with pytest.raises(crypto.SecretError, match="APP_SECRET changed"):
        crypto.decrypt(token)


def test_generated_secret_is_persisted(monkeypatch, tmp_path):
    path = tmp_path / "secret"
    monkeypatch.setattr(crypto, "get_settings", lambda: Settings(app_secret="", app_secret_file=str(path)))
    token = crypto.encrypt("x")
    assert path.read_text().strip() and crypto.decrypt(token) == "x"


# ------------------------------------------------------------------------------------ URL guard
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("http://box:1234/v1?x=1#frag", "http://box:1234/v1"),
        ("  https://api.example.com  ", "https://api.example.com"),
    ],
)
def test_normalize(url, expected):
    assert net_guard.normalize(url).url == expected


@pytest.mark.parametrize(
    "url", ["ftp://x/v1", "https://", "http://user:pw@host/v1", "localhost:11434", "http://h:99999"]
)
def test_normalize_rejects(url):
    with pytest.raises(net_guard.UnsafeURL):
        net_guard.normalize(url)


def test_localhost_is_rewritten_inside_docker():
    checked = net_guard.normalize("http://localhost:11434/v1", "host.docker.internal")
    assert checked.url == "http://host.docker.internal:11434/v1" and "container itself" in checked.hint
    assert net_guard.normalize("http://127.0.0.1:1234/v1", "").url == "http://127.0.0.1:1234/v1"


PUBLIC = Settings(mock_mode=True, live_access_code="code")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1/v1",
        "http://10.0.0.5/v1",
        "http://192.168.1.10:1234/v1",
        "http://169.254.169.254/latest",  # cloud metadata
        "http://[::1]:8080/v1",
        "http://metadata.google.internal/v1",
        "http://host.docker.internal:11434/v1",
    ],
)
def test_public_deployment_blocks_private_addresses(url):
    with pytest.raises(net_guard.UnsafeURL):
        net_guard.check(url, PUBLIC)


def test_public_deployment_allows_public_ips_and_local_allows_private():
    assert net_guard.check("https://8.8.8.8/v1", PUBLIC).url == "https://8.8.8.8/v1"
    assert net_guard.check("http://10.0.0.5/v1", Settings(mock_mode=False)).url == "http://10.0.0.5/v1"
    assert not net_guard.private_allowed(PUBLIC)
    assert net_guard.private_allowed(
        Settings(mock_mode=True, live_access_code="c", allow_private_endpoints=True)
    )


@pytest.mark.parametrize(
    "url", ["http://169.254.169.254/latest", "http://metadata.google.internal/v1", "http://[fe80::1]:80/v1"]
)
def test_cloud_metadata_blocked_even_where_private_endpoints_are_allowed(url):
    local = Settings(mock_mode=False)  # private endpoints allowed (local Ollama, LAN servers)
    assert net_guard.private_allowed(local)
    with pytest.raises(net_guard.UnsafeURL, match="metadata|link-local"):
        net_guard.check(url, local)
    assert net_guard.check("http://192.168.1.10:1234/v1", local).url == "http://192.168.1.10:1234/v1"


def test_unresolvable_host_rejected_on_public(monkeypatch):
    with pytest.raises(net_guard.UnsafeURL, match="can't resolve"):
        net_guard.check("https://no-such-host.invalid/v1", PUBLIC)


# ------------------------------------------------------------------------------------ API
def test_create_never_returns_the_key(api, db_engine):
    created = create(api)
    raw = json.dumps(created)
    assert KEY not in raw and "SECRET" not in raw
    assert created["has_api_key"] and created["key_hint"] == "sk-…abcd"
    assert created["slug"] == "my-endpoint" and created["model_ids"] == ["custom/my-endpoint/tiny"]
    assert KEY not in json.dumps(api.get("/connections").json())
    with Session(db_engine) as db:
        row = db.get(ModelConnection, created["id"])
        assert row.api_key_enc and KEY not in row.api_key_enc and crypto.decrypt(row.api_key_enc) == KEY


def test_slugs_names_and_validation(api):
    a = create(api, name="Box")
    b = create(api, name="Box!", slug=None)
    assert a["slug"] == "box" and b["slug"] == "box-2"
    assert api.post("/connections", json={"name": "Box", "base_url": BASE}).status_code == 409
    assert api.post("/connections", json={"name": "Z", "base_url": BASE, "slug": "box"}).status_code == 409
    assert (
        api.post("/connections", json={"name": "Z", "base_url": BASE, "slug": "Bad Slug"}).status_code == 422
    )
    assert api.post("/connections", json={"name": "Z", "base_url": "ftp://x/v1"}).status_code == 422
    assert (
        api.post("/connections", json={"name": "Z", "base_url": BASE, "models": ["has space"]}).status_code
        == 422
    )
    no_key = api.post("/connections", json={"name": "Local", "base_url": "http://localhost:11434/v1"}).json()
    assert no_key["has_api_key"] is False and no_key["key_hint"] is None


def test_update_keeps_replaces_or_clears_key(api, db_engine):
    c = create(api)
    body = {"name": "Renamed", "base_url": BASE, "models": ["tiny", "big"]}
    kept = api.put(f"/connections/{c['id']}", json=body).json()
    assert kept["key_hint"] == "sk-…abcd" and kept["models"] == ["tiny", "big"] and kept["slug"] == c["slug"]
    replaced = api.put(f"/connections/{c['id']}", json={**body, "api_key": "new-key-0000zzzz"}).json()
    assert replaced["key_hint"] == "new…zzzz"
    cleared = api.put(f"/connections/{c['id']}", json={**body, "clear_api_key": True}).json()
    assert cleared["has_api_key"] is False
    assert api.put("/connections/999", json=body).status_code == 404


def test_public_deployment_gates_connections(api):
    app.dependency_overrides[get_settings] = lambda: PUBLIC
    body = {"name": "X", "base_url": "https://8.8.8.8/v1", "models": ["m"]}
    assert api.post("/connections", json=body).status_code == 403
    assert api.post("/connections/test", json={"base_url": BASE}).status_code == 403
    code = {"X-Access-Code": "code"}
    assert api.post("/connections", json=body, headers=code).status_code == 201
    local = {**body, "name": "Y", "base_url": "http://localhost:11434/v1"}
    res = api.post("/connections", json=local, headers=code)
    assert res.status_code == 422 and "isn't allowed" in res.json()["detail"]
    assert api.get("/connections").status_code == 200  # listing is public (no secrets in it)


def test_presets(api):
    presets = api.get("/connections/presets").json()
    ids = {p["id"] for p in presets}
    assert {"ollama", "lmstudio", "vllm", "openai", "anthropic", "gemini", "custom"} <= ids
    assert all(p["base_url"].startswith(("http://", "https://")) for p in presets if p["id"] != "custom")


# ------------------------------------------------------------------------------------ probing
def test_test_endpoint_lists_models_chats_and_checks_tools(api, server):
    res = api.post("/connections/test", json={"base_url": BASE, "api_key": KEY, "model": "tiny"}).json()
    assert res["ok"] and res["models"] == ["big", "tiny"]
    assert res["chat_ok"] and res["chat_reply"] == "pong" and res["chat_latency_ms"] >= 0
    assert res["tools_ok"] is True and "calculator" in res["tools_detail"]
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in server.requests)


def test_test_endpoint_reports_bad_model_and_bad_key_without_leaking(api, server):
    res = api.post("/connections/test", json={"base_url": BASE, "api_key": KEY, "model": "nope"}).json()
    assert res["ok"] is False and res["chat_ok"] is False and "not found" in res["chat_error"]
    bad = "sk-wrong-key-99999999"
    res = api.post("/connections/test", json={"base_url": BASE, "api_key": bad, "model": "tiny"}).json()
    assert res["chat_ok"] is False and "Incorrect API key" in res["chat_error"]
    assert bad not in json.dumps(res) and "***" in res["chat_error"]


def test_saved_connection_test_records_result(api, server):
    c = create(api)
    res = api.post(f"/connections/{c['id']}/test").json()
    assert res["ok"] and res["model"] == "tiny"
    got = api.get(f"/connections/{c['id']}").json()
    assert got["discovered_models"] == ["big", "tiny"] and got["supports_tools"] is True
    assert got["last_check_ok"] and got["last_check_detail"].startswith("tiny:")
    server.tools = False
    api.post(f"/connections/{c['id']}/test", json={"model": "big"})
    assert api.get(f"/connections/{c['id']}").json()["supports_tools"] is False


# ------------------------------------------------------------------------------------ agents & routing
def test_agents_can_use_connections_and_delete_is_guarded(api):
    c = create(api)
    agent = {"name": "Mine", "model": "custom/my-endpoint/tiny", "tools": ["calculator"]}
    assert api.post("/agents", json=agent).status_code == 201
    assert api.post("/agents", json={**agent, "name": "X", "model": "custom/nope/tiny"}).status_code == 422
    assert api.post("/agents", json={**agent, "name": "X", "model": "custom/only-slug"}).status_code == 422
    assert api.get(f"/connections/{c['id']}").json()["agents"] == ["Mine"]
    res = api.delete(f"/connections/{c['id']}")
    assert res.status_code == 409 and "Mine" in res.json()["detail"]


def test_mock_mode_try_works_with_custom_models(api):
    create(api)
    aid = api.post("/agents", json={"name": "Mine", "model": "custom/my-endpoint/tiny"}).json()["id"]
    res = api.post(f"/agents/{aid}/try", json={"prompt": "What is 2+2?"})
    assert res.status_code == 200 and res.json()["status"] == "ok"  # simulated: mock profile for any id


def test_parse_custom():
    assert parse_custom("custom/box/llama3.2:8b") == ("box", "llama3.2:8b")
    assert parse_custom("custom/box/org/model") == ("box", "org/model")
    assert parse_custom("groq/openai/gpt-oss-20b") is None and parse_custom("custom/x") is None
    assert custom_model_id("box", "m") == "custom/box/m"


def test_router_dispatches_and_rebuilds_on_change(api, server, db_engine):
    c = create(api)
    bifrost = MockLLMClient(latency_scale=0)
    router = ModelRouter(bifrost, ConnectionRegistry(get_sessionmaker()))
    res = router.chat("custom/my-endpoint/tiny", [{"role": "user", "content": "hi"}])
    assert (
        res.content == "pong"
        and res.model == "custom/my-endpoint/tiny"
        and res.provider == "custom/my-endpoint"
    )
    assert json.loads(server.requests[-1].content)["model"] == "tiny"  # the bare model name is sent
    assert router.chat("groq/openai/gpt-oss-20b", [{"role": "user", "content": "hi"}]).provider == "groq"
    api.put(
        f"/connections/{c['id']}",
        json={"name": "N", "base_url": "https://other.example.com/v1", "models": ["tiny"]},
    )
    router.chat("custom/my-endpoint/tiny", [{"role": "user", "content": "hi"}])
    assert server.requests[-1].url.host == "other.example.com"
    assert "custom/my-endpoint/tiny" in router.list_models()
    with pytest.raises(LLMError, match="unknown model connection"):
        router.chat("custom/ghost/x", [{"role": "user", "content": "hi"}])


def test_live_benchmark_with_a_connection_agent_is_priced_from_the_connection(api, server, db_engine):
    create(api, price_input_per_m=1.0, price_output_per_m=2.0)
    aid = api.post(
        "/agents", json={"name": "Mine", "model": "custom/my-endpoint/tiny", "tools": ["calculator"]}
    )
    aid = aid.json()["id"]
    live = (
        ModelRouter(MockLLMClient(latency_scale=0), ConnectionRegistry(get_sessionmaker())),
        MockSearchClient(latency_scale=0),
    )
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=False, mock_latency_scale=0),
        clients=lambda is_live: live,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    with Session(db_engine) as db:
        sid = db.query(TaskSuite).filter_by(name="Math & Reasoning").one().id
        run = engine.create_run(db, suite_id=sid, agent_ids=[aid], repeats=1, fault_rate=0, live=True)
    engine.start(run.id)
    with Session(db_engine) as db:
        assert db.get(BenchmarkRun, run.id).status == "done"
        execs = db.query(TaskExecution).filter_by(run_id=run.id).all()
        assert execs and all(e.status == "ok" for e in execs)
        e = execs[0]
        expected = e.prompt_tokens * 1.0 / 1e6 + e.completion_tokens * 2.0 / 1e6
        assert e.cost_usd == pytest.approx(expected) and "price: connection" in e.score_detail


def test_pricebook_for_connection_without_prices(api):
    create(api)
    assert PriceBook(None).cost("custom/my-endpoint/tiny", 1000, 1000) == (0.0, "connection (no price set)")
