"""Per-request mock/live mode and the access code (public deployment behaviour)."""

import pytest
from fastapi import HTTPException

from app.clients import BifrostClient, MockLLMClient
from app.config import Settings, get_settings
from app.main import app
from app.mode import live_available, resolve_live

LOCAL_MOCK = Settings(mock_mode=True)
LOCAL_LIVE = Settings(mock_mode=False)
PUBLIC = Settings(mock_mode=True, live_access_code="s3cret-code")


@pytest.mark.parametrize(
    ("settings", "code", "live"),
    [
        (LOCAL_LIVE, None, True),
        (LOCAL_LIVE, "anything", True),  # local live ignores codes
        (LOCAL_MOCK, None, False),
        (PUBLIC, None, False),
        (PUBLIC, "", False),
        (PUBLIC, "s3cret-code", True),
    ],
)
def test_resolve_live(settings, code, live):
    assert resolve_live(settings, code) is live


@pytest.mark.parametrize(("settings", "code"), [(PUBLIC, "wrong"), (LOCAL_MOCK, "anything")])
def test_wrong_or_unsupported_code_is_401(settings, code):
    with pytest.raises(HTTPException) as exc:
        resolve_live(settings, code)
    assert exc.value.status_code == 401


def test_live_available():
    assert live_available(LOCAL_LIVE) and live_available(PUBLIC) and not live_available(LOCAL_MOCK)


@pytest.fixture()
def public(client):
    app.dependency_overrides[get_settings] = lambda: PUBLIC
    return client


def test_public_health_is_mock_without_code_and_live_with_code(public, monkeypatch):
    monkeypatch.setattr(BifrostClient, "ping", lambda self: (True, "ok"))
    anon = public.get("/health").json()
    assert anon["mock_mode"] is True and anon["live_available"] is True and anon["public_deployment"] is True
    assert anon["components"]["llm"]["detail"] == "mock"

    unlocked = public.get("/health", headers={"X-Access-Code": "s3cret-code"}).json()
    assert unlocked["mock_mode"] is False
    assert unlocked["components"]["llm"]["detail"] == "ok"

    assert public.get("/health", headers={"X-Access-Code": "nope"}).status_code == 401


def test_public_run_records_mode_from_request(public):
    from app.bench.engine import get_bench_engine

    created = []

    class FakeEngine:
        def create_run(self, db, **kw):
            created.append(kw["live"])
            raise __import__("app.bench.engine", fromlist=["RunValidationError"]).RunValidationError("stop")

    app.dependency_overrides[get_bench_engine] = lambda: FakeEngine()
    body = {"suite_id": 1, "agent_ids": [1]}
    public.post("/runs", json=body)
    public.post("/runs", json=body, headers={"X-Access-Code": "s3cret-code"})
    assert created == [False, True]


def test_public_destructive_actions_need_code(public):
    agent = public.post(
        "/agents", json={"name": "visitor agent", "model": "groq/openai/gpt-oss-20b"}
    )  # creating is allowed for everyone
    assert agent.status_code == 201
    aid = agent.json()["id"]
    body = {"name": "renamed", "model": "groq/openai/gpt-oss-20b"}

    assert public.put(f"/agents/{aid}", json=body).status_code == 403
    assert public.delete(f"/agents/{aid}").status_code == 403
    assert public.delete("/runs/1").status_code == 403
    assert public.post("/runs/1/cancel").status_code == 403
    assert public.delete("/suites/1").status_code == 403

    admin = {"X-Access-Code": "s3cret-code"}
    assert public.put(f"/agents/{aid}", json=body, headers=admin).status_code == 200
    assert public.delete(f"/agents/{aid}", headers=admin).status_code == 204


def test_local_deployments_need_no_code_for_admin(client):
    aid = client.post("/agents", json={"name": "x", "model": "groq/openai/gpt-oss-20b"}).json()["id"]
    assert client.delete(f"/agents/{aid}").status_code == 204


def test_mock_and_live_clients_are_distinct():
    from app.clients import clients_for

    assert isinstance(clients_for(False)[0], MockLLMClient)
    assert isinstance(clients_for(True)[0], BifrostClient)


def test_engine_live_run_uses_live_clients_and_mock_run_uses_mock(db_engine):
    from sqlalchemy.orm import Session

    from app.bench.engine import BenchmarkEngine, InlineExecutor
    from app.bench.pricing import PriceBook
    from app.clients.mock_search import MockSearchClient
    from app.db import get_sessionmaker
    from app.models import BenchmarkRun
    from app.seed import seed_agents, seed_suites

    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    asked = []
    fakes = (MockLLMClient(seed=1, latency_scale=0), MockSearchClient(latency_scale=0))

    def clients(live):
        asked.append(live)
        return fakes

    eng = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=clients,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    with Session(db_engine) as db:
        r_live = eng.create_run(db, suite_id=2, agent_ids=[1], repeats=1, fault_rate=0, live=True).id
        r_mock = eng.create_run(db, suite_id=2, agent_ids=[1], repeats=1, fault_rate=0, live=False).id
    eng.start(r_live)
    eng.start(r_mock)
    assert asked == [True, False]
    with Session(db_engine) as db:
        assert db.get(BenchmarkRun, r_live).mock_mode is False
        assert db.get(BenchmarkRun, r_mock).mock_mode is True
