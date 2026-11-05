"""Thin HTTP wrapper around the AgentPulse backend API.

All calls return ``(data, error)`` so pages can show a friendly message instead of a stack trace.
"""

from __future__ import annotations

import os
from typing import Any

import requests

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
DEFAULT_TIMEOUT = float(os.getenv("FRONTEND_HTTP_TIMEOUT_S", "15"))
LONG_TIMEOUT = float(os.getenv("FRONTEND_LONG_TIMEOUT_S", "600"))  # agent runs can be slow (local models)


ACCESS_CODE_HEADER = "X-Access-Code"


def access_code() -> str | None:
    """The live-mode access code entered in the sidebar (if any)."""
    try:
        import streamlit as st

        return st.session_state.get("access_code") or None
    except Exception:  # outside a Streamlit session (e.g. plain scripts)
        return None


def _request(method: str, path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    code = kwargs.pop("code", None) or access_code()
    if code:
        kwargs["headers"] = {**kwargs.get("headers", {}), ACCESS_CODE_HEADER: code}
    try:
        res = requests.request(method, f"{BACKEND_URL}{path}", **kwargs)
    except requests.ConnectionError:
        return None, f"Cannot reach the backend at {BACKEND_URL}. Is it running?"
    except requests.Timeout:
        return None, f"The backend took longer than {kwargs['timeout']:.0f}s to respond."
    if not res.ok:
        return None, _format_error(res)
    if res.status_code == 204 or not res.content:
        return {}, None
    try:
        return res.json(), None
    except ValueError:
        return res.content, None


def _format_error(res: requests.Response) -> str:
    try:
        detail = res.json().get("detail", res.text)
    except ValueError:
        detail = res.text
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail
        )
    return f"Backend error {res.status_code}: {detail}"


def get(path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("GET", path, **kwargs)


def post(path: str, json: Any = None, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("POST", path, json=json, **kwargs)


def put(path: str, json: Any = None, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("PUT", path, json=json, **kwargs)


def delete(path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    return _request("DELETE", path, **kwargs)


def health(code: str | None = None) -> tuple[dict | None, str | None]:
    return get("/health", timeout=5, code=code)


def download(path: str, params: dict | None = None, timeout: float = 60) -> tuple[bytes | None, str | None]:
    """Raw response body (for report downloads), never JSON-decoded."""
    headers = {ACCESS_CODE_HEADER: code} if (code := access_code()) else {}
    try:
        res = requests.get(f"{BACKEND_URL}{path}", params=params, headers=headers, timeout=timeout)
    except requests.ConnectionError:
        return None, f"Cannot reach the backend at {BACKEND_URL}. Is it running?"
    except requests.Timeout:
        return None, f"The backend took longer than {timeout:.0f}s to respond."
    if not res.ok:
        return None, _format_error(res)
    return res.content, None


def post_text(path: str, text: str, params: dict | None = None) -> tuple[Any | None, str | None]:
    """POST a raw text body (file imports), JSON response."""
    return _request(
        "POST",
        path,
        params=params,
        data=text.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        timeout=60,
    )
