"""Smoke test for the deploy image, run INSIDE the container (internal ports are not published):

    docker exec <container> python /tmp/smoke_image.py

Checks: dashboard + backend up; public mock mode; seeded data; a full mock benchmark run on
SQLite; access-code rules (wrong code → 401, right code → live, admin actions need the code).
Expects the container to have been started with LIVE_ACCESS_CODE=smoke-test-code.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"
UI = "http://127.0.0.1:7860"
CODE = "smoke-test-code"
failures: list[str] = []


def call(method: str, path: str, body: dict | None = None, code: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API}{path}", method=method, data=json.dumps(body).encode() if body else None
    )
    req.add_header("content-type", "application/json")
    if code:
        req.add_header("X-Access-Code", code)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail if not ok else ''}")
    if not ok:
        failures.append(name)


def main() -> int:
    with urllib.request.urlopen(f"{UI}/_stcore/health", timeout=10) as res:
        check("dashboard health", res.status == 200)

    for _ in range(120):  # the container is "healthy" once the dashboard is up; wait for the API too
        try:
            status, h = call("GET", "/health")
            break
        except OSError:
            time.sleep(1)
    else:
        status, h = call("GET", "/health")
    check("backend health ok", status == 200 and h.get("status") == "ok", h)
    check(
        "public demo defaults to mock", h.get("mock_mode") is True and h.get("public_deployment") is True, h
    )

    _, agents = call("GET", "/agents")
    _, suites = call("GET", "/suites")
    check("6 agents seeded", len(agents) == 6, len(agents))
    builtins = [s["name"] for s in suites if s.get("builtin")]
    check("3 built-in suites seeded", len(builtins) == 3, [s["name"] for s in suites])

    math = next(s for s in suites if s["name"] == "Math & Reasoning")
    status, run = call(
        "POST", "/runs", {"suite_id": math["id"], "agent_ids": [1, 2], "repeats": 2, "fault_rate": 0.2}
    )
    check("start mock run", status == 202 and run.get("mock_mode") is True, run)
    for _ in range(180):
        _, run = call("GET", f"/runs/{run['id']}")
        if run["status"] not in ("pending", "running"):
            break
        time.sleep(1)
    check("mock run finished on SQLite", run["status"] == "done" and run["progress_done"] == 32, run)
    _, metrics = call("GET", f"/runs/{run['id']}/metrics")
    check("metrics computed", len(metrics.get("leaderboard", [])) == 2, metrics.get("leaderboard"))

    runs = call("GET", "/runs")[1]
    checks = call("GET", "/freshness/checks")[1]
    check(
        "demo data seeded (2 mock runs + example checks)",
        sum(r["suite_name"] in ("Math & Reasoning", "Current Events (time-sensitive)") for r in runs) >= 3
        and len(checks) >= 2,
        [(r["id"], r["suite_name"], r["status"]) for r in runs],
    )

    snaps = call("GET", "/freshbench/snapshots")[1]
    fb_run = next((r for r in runs if r["suite_name"].startswith("Freshness Bench")), None)
    for _ in range(240):  # the demo bench run executes after the two classic demo runs
        if fb_run is None or fb_run["status"] not in ("pending", "running"):
            break
        time.sleep(1)
        fb_run = call("GET", f"/runs/{fb_run['id']}")[1]
    curve = call("GET", f"/runs/{fb_run['id']}/knowledge-curve")[1] if fb_run else {}
    statuses = sorted((a.get("cutoff") or {}).get("status", "") for a in curve.get("agents", []))
    check(
        "Freshness Bench demo: snapshot, run, knowledge curve",
        bool(snaps) and snaps[0]["questions"] >= 36 and statuses == ["estimated", "no cutoff detected"],
        {"snapshots": [(x["name"], x["questions"]) for x in snaps], "curve": statuses},
    )

    status, fc = call(
        "POST", "/freshness/check", {"text": "The current UK PM is Rishi Sunak. Water boils at 100 °C."}
    )
    check(
        "stale-answer detector (mock)",
        status == 201
        and fc.get("verdict") == "stale"
        and fc.get("counts", {}).get("not_time_sensitive") == 1,
        fc,
    )

    status, suite = call("POST", "/suites", {"name": "Smoke suite", "tasks": []})
    task_status = call(
        "POST", f"/suites/{suite.get('id')}/tasks", {"key": "k", "prompt": "p?", "expected": "x"}
    )[0]
    builtin = next((x for x in call("GET", "/suites")[1] if x.get("builtin")), {})
    check(
        "custom suite + task without code; built-ins read-only",
        status == 201 and task_status == 201 and call("DELETE", f"/suites/{builtin.get('id')}")[0] == 403,
        (status, task_status),
    )

    conn = {
        "name": "Smoke conn",
        "base_url": "https://8.8.8.8/v1",
        "models": ["m"],
        "api_key": "sk-smoke-1234",
    }
    no_code = call("POST", "/connections", conn)[0]
    status, created = call("POST", "/connections", conn, code=CODE)
    private = call(
        "POST", "/connections", {**conn, "name": "P", "base_url": "http://localhost:11434/v1"}, code=CODE
    )
    check(
        "connections: code required, key never returned, private URLs blocked",
        no_code == 403
        and status == 201
        and "sk-smoke-1234" not in str(created)
        and created.get("key_hint") == "sk-…1234"
        and private[0] == 422,
        (no_code, status, private),
    )

    hosted = {
        "name": "Smoke Gemini",
        "base_url": "https://8.8.4.4/v1",
        "models": ["m"],
        "api_key": "AIza-smoke-0000",
        "provider": "gemini",
        "via_bifrost": True,
    }
    status, h = call("POST", "/connections", hosted, code=CODE)
    check(
        "hosted connection falls back to direct on the cloud image (no Bifrost config store/token)",
        status == 201 and h.get("route") == "direct" and bool(h.get("route_reason")),
        (status, h.get("route"), h.get("route_reason")),
    )

    check("wrong access code -> 401", call("GET", "/health", code="nope")[0] == 401)
    status, live = call("GET", "/health", code=CODE)
    check("right access code -> live mode", status == 200 and live.get("mock_mode") is False, live)
    check("delete without code -> 403", call("DELETE", f"/runs/{run['id']}")[0] == 403)
    check("delete with code -> 204", call("DELETE", f"/runs/{run['id']}", code=CODE)[0] == 204)

    print(f"\n  {'OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
