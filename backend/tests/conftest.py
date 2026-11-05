"""Test configuration: isolated SQLite database, mock mode and zero simulated latency.

Environment variables are set *before* any ``app`` import so cached settings, engine and
clients pick them up. Environment variables take precedence over a developer's ``.env``.
"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="agentpulse-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_LATENCY_SCALE"] = "0"
os.environ["MOCK_SEED"] = "42"
os.environ["SEED_ON_STARTUP"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, get_engine, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def db_engine():
    """Fresh schema for each test."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    init_db(engine)
    yield engine


@pytest.fixture()
def client(db_engine):
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
