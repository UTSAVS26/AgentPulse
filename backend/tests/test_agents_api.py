from sqlalchemy.orm import Session

from app.models import Agent
from app.seed import seed_agents

AGENT = {
    "name": "Test agent",
    "model": "groq/openai/gpt-oss-20b",
    "tools": ["calculator", "web_search"],
    "system_prompt": "Be precise.",
    "temperature": 0.0,
    "max_steps": 5,
}


def test_crud_lifecycle(client):
    created = client.post("/agents", json=AGENT)
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["id"] and agent["tools"] == ["calculator", "web_search"]

    assert [a["name"] for a in client.get("/agents").json()] == ["Test agent"]
    assert client.get(f"/agents/{agent['id']}").json()["model"] == AGENT["model"]

    updated = client.put(f"/agents/{agent['id']}", json={**AGENT, "max_steps": 2, "tools": []})
    assert updated.status_code == 200
    assert updated.json()["max_steps"] == 2 and updated.json()["tools"] == []

    assert client.delete(f"/agents/{agent['id']}").status_code == 204
    assert client.get(f"/agents/{agent['id']}").status_code == 404


def test_duplicate_name_is_409(client):
    assert client.post("/agents", json=AGENT).status_code == 201
    res = client.post("/agents", json=AGENT)
    assert res.status_code == 409 and "already exists" in res.json()["detail"]


def test_validation_errors_are_422(client):
    cases = [
        {**AGENT, "tools": ["rm_rf"]},
        {**AGENT, "model": "no-provider-prefix"},
        {**AGENT, "max_steps": 0},
        {**AGENT, "temperature": 3},
        {**AGENT, "name": ""},
    ]
    for body in cases:
        assert client.post("/agents", json=body).status_code == 422, body


def test_missing_agent_is_404(client):
    assert client.put("/agents/999", json=AGENT).status_code == 404
    assert client.delete("/agents/999").status_code == 404
    assert client.post("/agents/999/try", json={"prompt": "hi"}).status_code == 404
    assert client.get("/executions/999").status_code == 404


def test_tools_catalogue(client):
    names = [t["name"] for t in client.get("/tools").json()]
    assert names == ["web_search", "calculator", "current_date"]


def test_try_agent_returns_output_and_trace_and_is_persisted(client):
    agent_id = client.post("/agents", json=AGENT).json()["id"]
    res = client.post(f"/agents/{agent_id}/try", json={"prompt": "What is 17 * 23 + 4?", "seed": 1})
    assert res.status_code == 200, res.text
    ex = res.json()
    assert ex["status"] in {"ok", "error", "max_steps"}
    assert ex["agent_name"] == "Test agent" and ex["model"] == AGENT["model"]
    assert ex["trace"] and ex["trace"][0]["type"] in {"llm_call", "error"}
    assert [s["idx"] for s in ex["trace"]] == list(range(len(ex["trace"])))

    again = client.get(f"/executions/{ex['id']}").json()
    assert again["trace"] == ex["trace"]
    history = client.get(f"/agents/{agent_id}/executions").json()
    assert [h["id"] for h in history] == [ex["id"]]


def test_try_agent_with_full_fault_rate_records_faults(client):
    agent_id = client.post("/agents", json=AGENT).json()["id"]
    ex = client.post(
        f"/agents/{agent_id}/try", json={"prompt": "What is 6 * 7?", "fault_rate": 1.0, "seed": 2}
    ).json()
    tool_results = [s for s in ex["trace"] if s["type"] == "tool_result"]
    assert all(s["payload"].get("fault") for s in tool_results)
    assert ex["faults_injected"] == len(tool_results)


def test_seed_agents_is_idempotent_and_preserves_edits(db_engine):
    with Session(db_engine) as db:
        assert seed_agents(db) == 6
        first = db.query(Agent).order_by(Agent.id).first()
        first.max_steps = 2
        db.commit()
        assert seed_agents(db) == 0
        assert db.query(Agent).count() == 6
        assert db.get(Agent, first.id).max_steps == 2
