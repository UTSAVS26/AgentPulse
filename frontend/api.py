"""Thin HTTP wrapper around the AgentPulse backend API.

All calls return ``(data, error)`` so pages can show a friendly message instead of a stack trace.
"""

from __future__ import annotations

import os
from typing import Any

import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DEFAULT_TIMEOUT = float(os.getenv("FRONTEND_HTTP_TIMEOUT_S", "15"))


def _request(method: str, path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    try:
        res = requests.request(method, f"{BACKEND_URL}{path}", **kwargs)
    except requests.ConnectionError:
        return None, f"Cannot reach the backend at {BACKEND_URL}. Is it running?"
    except requests.Timeout:
        return None, f"The backend took longer than {kwargs['timeout']}s to respond."
    if not res.ok:
        try:
            detail = res.json().get("detail", res.text)
        except ValueError:
            detail = res.text
        return None, f"Backend error {res.status_code}: {detail}"
    try:
        return res.json(), None
    except ValueError:
        return res.content, None


def get(path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("GET", path, **kwargs)


def post(path: str, json: Any = None, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("POST", path, json=json, **kwargs)


def health() -> tuple[dict | None, str | None]:
    return get("/health", timeout=5)
