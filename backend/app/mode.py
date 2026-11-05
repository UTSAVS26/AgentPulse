"""Per-request mock/live mode and the access code that unlocks live mode.

Rules
-----
* ``MOCK_MODE=false``  → every request is **live** (local development with your own keys).
* ``MOCK_MODE=true`` and no ``LIVE_ACCESS_CODE`` → every request is **mock** (offline demo).
* ``MOCK_MODE=true`` and ``LIVE_ACCESS_CODE`` set → **public deployment**: requests are mock
  unless they carry ``X-Access-Code: <code>``, which unlocks live mode (real Bifrost + Tavily).
  A wrong code is rejected with 401 so the user knows. In this setup, destructive operations
  (editing/deleting agents, deleting suites/runs, cancelling runs) also require the code, so
  anonymous visitors can't wipe the shared demo.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from app.clients import LLMClient, SearchClient, clients_for
from app.config import Settings, get_settings

ACCESS_CODE_HEADER = "X-Access-Code"


@dataclass(frozen=True)
class Mode:
    live: bool
    llm: LLMClient
    search: SearchClient

    @property
    def mock(self) -> bool:
        return not self.live


def live_available(settings: Settings) -> bool:
    return (not settings.mock_mode) or bool(settings.live_access_code)


def is_public_deployment(settings: Settings) -> bool:
    return settings.mock_mode and bool(settings.live_access_code)


def _code_is_valid(settings: Settings, code: str) -> bool:
    return bool(settings.live_access_code) and hmac.compare_digest(
        code.encode("utf-8"), settings.live_access_code.encode("utf-8")
    )


def resolve_live(settings: Settings, code: str | None) -> bool:
    """Decide live vs mock for a request; raises 401 for a wrong/unsupported code."""
    if not settings.mock_mode:
        return True
    if not code:
        return False
    if not settings.live_access_code:
        raise HTTPException(status_code=401, detail="live mode is not enabled on this deployment")
    if not _code_is_valid(settings, code):
        raise HTTPException(status_code=401, detail="invalid access code")
    return True


def get_mode(
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
    settings: Settings = Depends(get_settings),
) -> Mode:
    """FastAPI dependency: the mode (and clients) for this request."""
    live = resolve_live(settings, x_access_code)
    llm, search = clients_for(live)
    return Mode(live=live, llm=llm, search=search)


def require_admin(
    x_access_code: str | None = Header(default=None, alias=ACCESS_CODE_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard for destructive endpoints: only enforced on public deployments."""
    if is_public_deployment(settings) and not (x_access_code and _code_is_valid(settings, x_access_code)):
        raise HTTPException(
            status_code=403, detail="this action needs the access code on the public demo deployment"
        )
