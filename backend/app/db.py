"""Database engine, session factory and schema initialisation."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, inspect, text
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
            add_missing_columns(engine)
            return
        except Exception as exc:  # pragma: no cover - only hit when the DB is down
            if attempt == attempts:
                raise
            log.warning("Database not ready (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(delay_s)


def add_missing_columns(engine: Engine) -> list[str]:
    """Lightweight forward-only migration: add nullable columns that exist in the models but not
    in the database (``create_all`` never alters existing tables). Returns the columns added.

    Good enough for this project's additive schema changes; anything more complex would need Alembic.
    """
    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or not column.nullable:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}'))
                added.append(f"{table.name}.{column.name}")
    if added:
        log.info("Added missing columns: %s", ", ".join(added))
    return added


def ping_db(engine: Engine | None = None) -> tuple[bool, str]:
    """Return ``(ok, detail)`` for a trivial round-trip query."""
    try:
        with (engine or get_engine()).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
