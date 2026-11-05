"""Database engine, session factory and schema initialisation."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine (created lazily from settings)."""
    url = get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine | None = None, attempts: int = 10, delay_s: float = 1.5) -> None:
    """Create all tables, retrying while the database is still starting up."""
    from app import models  # noqa: F401  (register models on Base.metadata)

    engine = engine or get_engine()
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # pragma: no cover - only hit when the DB is down
            if attempt == attempts:
                raise
            log.warning("Database not ready (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(delay_s)


def ping_db(engine: Engine | None = None) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a trivial round-trip query."""
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
