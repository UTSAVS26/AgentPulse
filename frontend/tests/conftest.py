"""Shared fixtures for the dashboard tests.

``make ui-test`` runs against *your* running stack, so the session cleans up after itself: benchmark runs
and detector checks created during the session, and the test connections ("UI conn …") and suites
("UI suite …", "UI jury …", "Blank …") the tests add.
Nothing that existed before the session is touched.
"""

from __future__ import annotations

import os

import pytest
import requests

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")
TEST_CONNECTIONS = ("UI conn ",)
TEST_SUITES = ("UI suite ", "UI jury ", "Blank ")


def _get(path: str):
    try:
        res = requests.get(f"{BACKEND}{path}", timeout=10)
        return res.json() if res.ok else None
    except requests.RequestException:
        return None


@pytest.fixture(scope="session", autouse=True)
def clean_up_test_data():
    runs = _get("/runs?limit=200") or []
    watermark = max((r["id"] for r in runs), default=0)
    checks = _get("/freshness/checks?limit=200") or []
    check_watermark = max((c["id"] for c in checks), default=0)
    yield
    for run in _get("/runs?limit=200") or []:
        if run["id"] <= watermark:
            continue
        if run["status"] in ("pending", "running"):
            requests.post(f"{BACKEND}/runs/{run['id']}/cancel", timeout=10)
        requests.delete(f"{BACKEND}/runs/{run['id']}", timeout=10)
    for suite in _get("/suites") or []:
        if not suite["builtin"] and suite["name"].startswith(TEST_SUITES):
            requests.delete(f"{BACKEND}/suites/{suite['id']}", timeout=10)
    for check in _get("/freshness/checks?limit=200") or []:
        if (
            check["id"] > check_watermark
        ):  # detector, Guard and run checks the tests made (keeps History clean)
            requests.delete(f"{BACKEND}/freshness/checks/{check['id']}", timeout=10)
    for conn in _get("/connections") or []:
        if conn["name"].startswith(TEST_CONNECTIONS) and not conn["agents"]:
            requests.delete(f"{BACKEND}/connections/{conn['id']}", timeout=10)
