"""User-defined benchmarks: suite/task CRUD, built-in protection, import/export, scorer preview."""

from __future__ import annotations

import csv
import io

import pytest
import yaml
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.main import app
from app.models import TaskExecution
from app.seed import seed_agents, seed_suites

TASK = {"key": "capital-peru", "prompt": "What is the capital of Peru?", "expected": "Lima"}


@pytest.fixture()
def api(client, db_engine):
    with Session(db_engine) as db:
        seed_agents(db)
        seed_suites(db)
    return client


def builtin_id(api, name="Factual QA"):
    return next(s["id"] for s in api.get("/suites").json() if s["name"] == name)


def new_suite(api, name="My suite", tasks=None):
    res = api.post("/suites", json={"name": name, "description": "d", "tasks": tasks or []})
    assert res.status_code == 201, res.text
    return res.json()


def test_builtin_suites_are_flagged_and_read_only(api):
    sid = builtin_id(api)
    suite = api.get(f"/suites/{sid}").json()
    assert suite["builtin"] is True and suite["runs"] == 0
    assert all(t["executions"] == 0 for t in suite["tasks"])
    tid = suite["tasks"][0]["id"]
    for res in (
        api.put(f"/suites/{sid}", json={"name": "x"}),
        api.post(f"/suites/{sid}/tasks", json=TASK),
        api.put(f"/suites/{sid}/tasks/{tid}", json=TASK),
        api.delete(f"/suites/{sid}/tasks/{tid}"),
        api.delete(f"/suites/{sid}"),
    ):
        assert res.status_code == 403 and "duplicate" in res.json()["detail"]


def test_create_empty_suite_then_add_edit_delete_tasks(api):
    suite = new_suite(api)
    assert suite["builtin"] is False and suite["task_count"] == 0
    sid = suite["id"]
    task = api.post(f"/suites/{sid}/tasks", json=TASK).json()
    assert task["key"] == "capital-peru" and task["scorer"] == "contains"
    assert api.post(f"/suites/{sid}/tasks", json=TASK).status_code == 409  # duplicate key
    edited = api.put(
        f"/suites/{sid}/tasks/{task['id']}", json={**TASK, "expected": "Lima|Ciudad de los Reyes"}
    )
    assert edited.status_code == 200 and edited.json()["expected"].startswith("Lima|")
    other = api.post(f"/suites/{sid}/tasks", json={**TASK, "key": "k2"}).json()
    assert api.put(f"/suites/{sid}/tasks/{other['id']}", json=TASK).status_code == 409  # key clash on rename
    assert api.delete(f"/suites/{sid}/tasks/{other['id']}").status_code == 204
    assert [t["key"] for t in api.get(f"/suites/{sid}").json()["tasks"]] == ["capital-peru"]
    assert api.put(f"/suites/{sid}/tasks/999", json=TASK).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {**TASK, "key": "Has Spaces"},
        {**TASK, "scorer": "vibes"},
        {**TASK, "prompt": ""},
        {**TASK, "tolerance": -1},
    ],
)
def test_task_validation(api, bad):
    sid = new_suite(api)["id"]
    assert api.post(f"/suites/{sid}/tasks", json=bad).status_code == 422


def test_rename_and_name_conflicts(api):
    a, b = new_suite(api, "A"), new_suite(api, "B")
    assert api.post("/suites", json={"name": " a "}).status_code == 409  # case-insensitive, trimmed
    assert api.put(f"/suites/{b['id']}", json={"name": "a"}).status_code == 409
    res = api.put(f"/suites/{a['id']}", json={"name": "A renamed", "description": "new"})
    assert res.status_code == 200 and res.json()["name"] == "A renamed"
    assert api.post("/suites", json={"name": "   "}).status_code == 422


def test_duplicate_builtin_gives_editable_copy(api):
    sid = builtin_id(api)
    src = api.get(f"/suites/{sid}").json()
    copy = api.post(f"/suites/{sid}/duplicate").json()
    assert copy["name"] == "Factual QA (copy)" and copy["builtin"] is False
    assert [t["key"] for t in copy["tasks"]] == [t["key"] for t in src["tasks"]]
    assert api.post(f"/suites/{sid}/duplicate").json()["name"] == "Factual QA (copy 2)"
    named = api.post(f"/suites/{sid}/duplicate", params={"name": "Mine"})
    assert named.json()["name"] == "Mine"
    assert api.post(f"/suites/{copy['id']}/tasks", json=TASK).status_code == 201


def test_used_tasks_and_suites_with_runs_are_protected(api, db_engine):
    suite = new_suite(api, tasks=[TASK, {**TASK, "key": "unused"}])
    used_id = suite["tasks"][0]["id"]
    with Session(db_engine) as db:
        db.add(TaskExecution(agent_id=1, task_id=used_id, prompt="p", output="Lima"))
        db.commit()
    assert api.get(f"/suites/{suite['id']}").json()["tasks"][0]["executions"] == 1
    res = api.delete(f"/suites/{suite['id']}/tasks/{used_id}")
    assert res.status_code == 409 and "past runs" in res.json()["detail"]
    run = api.post("/runs", json={"suite_id": suite["id"], "agent_ids": [1]}).json()
    assert api.get(f"/suites/{suite['id']}").json()["runs"] == 1
    assert api.delete(f"/suites/{suite['id']}").status_code == 409
    assert run["suite_name"] == "My suite"


def test_delete_custom_suite(api):
    sid = new_suite(api)["id"]
    assert api.delete(f"/suites/{sid}").status_code == 204
    assert api.get(f"/suites/{sid}").status_code == 404


def test_public_demo_lets_anyone_edit_custom_suites(api):
    app.dependency_overrides[get_settings] = lambda: Settings(mock_mode=True, live_access_code="code")
    sid = new_suite(api)["id"]
    assert api.post(f"/suites/{sid}/tasks", json=TASK).status_code == 201
    assert api.delete(f"/suites/{sid}").status_code == 204


def test_yaml_export_import_round_trip(api):
    sid = builtin_id(api, "Math & Reasoning")
    src = api.get(f"/suites/{sid}").json()
    res = api.get(f"/suites/{sid}/export", params={"format": "yaml"})
    assert res.status_code == 200 and 'filename="math-reasoning.yaml"' in res.headers["content-disposition"]
    doc = yaml.safe_load(res.text)
    assert doc["name"] == "Math & Reasoning" and len(doc["tasks"]) == src["task_count"]
    assert "time_sensitive" not in doc["tasks"][0]  # defaults omitted
    imported = api.post(
        "/suites/import",
        params={"format": "yaml", "name": "Math copy"},
        content=res.text,
        headers={"content-type": "text/plain"},
    )
    assert imported.status_code == 201, imported.text
    tasks = imported.json()["suite"]["tasks"]
    keys = ("key", "prompt", "expected", "scorer", "tolerance", "category", "time_sensitive", "stale_answer")
    assert [{k: t[k] for k in keys} for t in tasks] == [{k: t[k] for k in keys} for t in src["tasks"]]


def test_csv_export_import_round_trip(api):
    sid = builtin_id(api, "Current Events (time-sensitive)")
    src = api.get(f"/suites/{sid}").json()
    text = api.get(f"/suites/{sid}/export", params={"format": "csv"}).text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == src["task_count"] and rows[0]["time_sensitive"] == "True"
    res = api.post("/suites/import", params={"format": "csv", "name": "CE copy"}, content=text)
    assert res.status_code == 201, res.text
    tasks = res.json()["suite"]["tasks"]
    assert all(t["time_sensitive"] for t in tasks)
    assert tasks[0]["stale_answer"] == src["tasks"][0]["stale_answer"]


def test_csv_import_minimal_columns_warnings_and_errors(api):
    ok = "﻿key,prompt,notes\nq1,What is 2+2?,ignored\n\nq2,Capital of Peru?,\n"
    res = api.post("/suites/import", params={"format": "csv", "name": "Mini"}, content=ok)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["warnings"] == ["ignored unknown column 'notes'"]
    assert [t["key"] for t in body["suite"]["tasks"]] == ["q1", "q2"]
    assert body["suite"]["tasks"][0]["scorer"] == "contains"

    bad = "key,prompt,scorer\nGood-Key?,x,contains\nq2,,vibes\n"
    res = api.post("/suites/import", params={"format": "csv", "name": "Bad"}, content=bad)
    assert res.status_code == 422
    errors = res.json()["detail"]
    assert any(e.startswith("line 2: key") for e in errors) and any(e.startswith("line 3:") for e in errors)
    assert api.post("/suites/import", params={"format": "csv"}, content=ok).status_code == 422  # name needed
    no_cols = api.post("/suites/import", params={"format": "csv", "name": "X"}, content="a,b\n1,2\n")
    assert no_cols.status_code == 422


def test_yaml_import_errors(api):
    def imp(text, **params):
        return api.post("/suites/import", params={"format": "yaml", **params}, content=text)

    assert imp("name: [unclosed").status_code == 422
    assert imp("- just a list").status_code == 422
    res = imp("name: Y\ntasks:\n  - key: a\n    prompt: p\n  - key: a\n    prompt: q\n")
    assert res.status_code == 422 and "duplicate task keys" in str(res.json()["detail"])
    assert imp("name: Factual QA\ntasks: []\n").status_code == 409  # name taken
    assert imp("tasks: []\n", name="Override").status_code == 201
    assert imp("x" * 1_000_001).status_code == 413


@pytest.mark.parametrize(
    ("payload", "passed"),
    [
        ({"output": "The answer is 1628.89.", "scorer": "numeric", "expected": "1628.89"}, True),
        ({"output": "Probably Lima.", "scorer": "contains", "expected": "Lima|Cusco"}, True),
        ({"output": "It's Cusco", "scorer": "exact", "expected": "Lima"}, False),
        ({"output": "Lima", "scorer": "judge", "expected": "Lima"}, True),
        ({"output": "It's Lima.", "scorer": "jury", "expected": "Lima"}, True),
        ({"output": "Cusco", "scorer": "jury", "expected": "Lima"}, False),
    ],
)
def test_scoring_preview(api, payload, passed):
    res = api.post("/scoring/preview", json=payload)
    assert res.status_code == 200 and res.json()["passed"] is passed
    if payload["scorer"] in ("judge", "jury"):
        detail = res.json()["detail"]
        assert detail.startswith("preview only: in a benchmark run,") and "configured" not in detail


def test_scoring_preview_renders_date_placeholders(api):
    res = api.post(
        "/scoring/preview",
        json={"output": "It is 1999", "scorer": "contains", "expected": "{{current_year}}"},
    ).json()
    assert res["expected_rendered"].isdigit() and res["passed"] is False
    assert api.post("/scoring/preview", json={"output": "x", "scorer": "nope"}).status_code == 422


def test_seed_marks_existing_suites_builtin(db_engine):
    with Session(db_engine) as db:
        seed_suites(db)
        from app.models import TaskSuite

        assert all(s.builtin for s in db.query(TaskSuite))
