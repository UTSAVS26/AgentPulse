"""v2.4: how each agent's model is called (Bifrost or direct) shows up everywhere; keys never do."""

from __future__ import annotations

import csv
import io
import json

import pytest
from sqlalchemy.orm import Session

from app.bench.engine import BenchmarkEngine, InlineExecutor, get_bench_engine
from app.bench.pricing import PriceBook
from app.clients.mock_llm import MockLLMClient
from app.clients.mock_search import MockSearchClient
from app.config import Settings
from app.db import get_sessionmaker
from app.main import app
from app.models import ModelConnection, TaskSuite
from app.reports import route_label
from app.seed import seed_agents, seed_suites

KEY = "sk-display-SECRET-99998888"


@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    mock = (MockLLMClient(seed=5, latency_scale=0), MockSearchClient(latency_scale=0))
    engine = BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=Settings(mock_mode=True, mock_latency_scale=0),
        clients=lambda live: mock,
        prices={True: PriceBook(None), False: PriceBook(None)},
        executor=InlineExecutor(),
    )
    app.dependency_overrides[get_bench_engine] = lambda: engine
    return client


@pytest.fixture()
def custom_agent(api):
    conn = api.post(
        "/connections",
        json={"name": "Box", "base_url": "https://llm.example.com/v1", "models": ["tiny"], "api_key": KEY},
    ).json()
    agent = api.post("/agents", json={"name": "Mine", "model": f"custom/{conn['slug']}/tiny"}).json()
    return conn, agent


def run_with(api, db_engine, agent_ids):
    with Session(db_engine) as db:
        sid = db.query(TaskSuite).filter_by(name="Factual QA").one().id
    return api.post("/runs", json={"suite_id": sid, "agent_ids": agent_ids, "repeats": 1}).json()["id"]


def test_agents_list_and_detail_show_route(api, custom_agent):
    _, mine = custom_agent
    agents = {a["name"]: a for a in api.get("/agents").json()}
    assert agents["Mine"]["route"] == "direct" and agents["Mine"]["connection"] == "Box"
    builtin = agents["GPT-OSS 20B · tools"]
    assert builtin["route"] == "bifrost" and builtin["connection"] is None
    assert api.get(f"/agents/{mine['id']}").json()["connection"] == "Box"


def test_leaderboard_and_execution_carry_route(api, custom_agent, db_engine):
    _, mine = custom_agent
    rid = run_with(api, db_engine, [1, mine["id"]])
    board = {r["agent_name"]: r for r in api.get(f"/runs/{rid}/metrics").json()["leaderboard"]}
    assert board["Mine"]["route"] == "direct" and board["Mine"]["connection"] == "Box"
    assert (
        board["GPT-OSS 20B · tools"]["route"] == "bifrost"
        and board["GPT-OSS 20B · tools"]["provider"] == "groq"
    )
    ex = next(e for e in api.get(f"/runs/{rid}/executions").json() if e["agent_name"] == "Mine")
    detail = api.get(f"/executions/{ex['id']}").json()
    assert detail["route"] == "direct" and detail["connection"] == "Box"


def test_reports_show_route_and_never_the_key(api, custom_agent, db_engine):
    _, mine = custom_agent
    rid = run_with(api, db_engine, [1, mine["id"]])
    md = api.get(f"/reports/{rid}").text
    assert "| Route |" in md and "direct · Box" in md and "| Bifrost |" in md
    rows = {r["Agent"]: r for r in csv.DictReader(io.StringIO(api.get(f"/reports/{rid}?format=csv").text))}
    assert rows["Mine"]["Route"] == "direct" and rows["Mine"]["Connection"] == "Box"
    everything = md + api.get(f"/reports/{rid}?format=csv").text + api.get(f"/reports/{rid}?format=json").text
    assert KEY not in everything and "SECRET" not in everything


def test_deleted_connection_is_labelled(api, custom_agent, db_engine):
    conn, mine = custom_agent
    with Session(db_engine) as db:  # simulate a connection removed behind the agent's back
        db.delete(db.get(ModelConnection, conn["id"]))
        db.commit()
    agent = api.get(f"/agents/{mine['id']}").json()
    assert agent["route"] == "direct" and agent["connection"].endswith("(deleted)")


def test_health_summarises_connections(api, custom_agent, db_engine):
    with Session(db_engine) as db:
        row = db.query(ModelConnection).one()
        row.last_check_ok = False
        db.commit()
    api.post(
        "/connections", json={"name": "Other", "base_url": "https://other.example.com/v1", "models": ["m"]}
    )
    summary = api.get("/health").json()["connections"]
    assert summary == {"total": 2, "ok": 0, "failing": 1, "untested": 1, "via_bifrost": 0}
    assert api.get("/health").json()["status"] == "ok"  # a failing user endpoint isn't a degraded backend
    assert json.dumps(summary).count("SECRET") == 0


@pytest.mark.parametrize(
    ("route", "connection", "label"),
    [
        ("bifrost", None, "Bifrost"),
        ("direct", "Box", "direct · Box"),
        ("bifrost", "G", "Bifrost · G"),
        (None, None, "–"),
    ],
)
def test_route_label(route, connection, label):
    assert route_label(route, connection) == label
