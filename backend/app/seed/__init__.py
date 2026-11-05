"""Idempotent seeding of demo data (default agents; task suites from Phase 3)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Agent, Task, TaskSuite
from app.schemas import AgentCreate, SuiteCreate

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


def seed_suites(db: Session, directory: Path = SEED_DIR / "suites") -> int:
    """Create or update the built-in suites from ``seed/suites/*.yaml``. Returns tasks added/updated.

    Built-in suites are owned by the YAML: tasks are matched by ``key``; new keys are added and
    existing tasks are updated (e.g. when a time-sensitive expected answer changes). Tasks are never
    deleted, so historical runs keep their references.
    """
    changed = 0
    for path in sorted(directory.glob("*.yaml")):
        spec = SuiteCreate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        suite = db.scalar(select(TaskSuite).where(TaskSuite.name == spec.name))
        if suite is None:
            suite = TaskSuite(name=spec.name, description=spec.description)
            db.add(suite)
            db.flush()
        suite.description = spec.description
        existing = {t.key: t for t in suite.tasks}
        for t in spec.tasks:
            fields = t.model_dump()
            task = existing.get(t.key)
            if task is None:
                suite.tasks.append(Task(**fields))
                changed += 1
            elif any(getattr(task, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(task, k, v)
                changed += 1
    db.commit()
    if changed:
        log.info("Seeded/updated %d suite tasks", changed)
    return changed
