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

    status, h = call("GET", "/health")
    check("backend health ok", status == 200 and h.get("status") == "ok", h)
    check(
        "public demo defaults to mock", h.get("mock_mode") is True and h.get("public_deployment") is True, h
    )

    _, agents = call("GET", "/agents")
    _, suites = call("GET", "/suites")
    check("6 agents seeded", len(agents) == 6, len(agents))
    check("3 suites seeded", len(suites) == 3, len(suites))

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

    check("wrong access code -> 401", call("GET", "/health", code="nope")[0] == 401)
    status, live = call("GET", "/health", code=CODE)
    check("right access code -> live mode", status == 200 and live.get("mock_mode") is False, live)
    check("delete without code -> 403", call("DELETE", f"/runs/{run['id']}")[0] == 403)
    check("delete with code -> 204", call("DELETE", f"/runs/{run['id']}", code=CODE)[0] == 204)

    print(f"\n  {'OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
