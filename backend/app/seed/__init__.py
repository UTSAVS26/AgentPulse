"""Idempotent seeding of demo data (default agents; task suites from Phase 3)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent
from app.schemas import AgentCreate

log = logging.getLogger(__name__)
SEED_DIR = Path(__file__).parent


def seed_agents(db: Session, path: Path = SEED_DIR / "default_agents.yaml") -> int:
    """Insert agents from YAML whose names don't exist yet. Returns the number inserted.

    Existing agents are never modified, so user edits survive restarts.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    existing = set(db.scalars(select(Agent.name)))
    added = 0
    for raw in data.get("agents", []):
        spec = AgentCreate.model_validate(raw)  # same validation as the API
        if spec.name in existing:
            continue
        db.add(Agent(**spec.model_dump()))
        added += 1
    db.commit()
    if added:
        log.info("Seeded %d default agents", added)
    return added
