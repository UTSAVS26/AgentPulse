"""Agents CRUD, tool catalogue, ad-hoc "try this agent" runs and execution traces."""

from __future__ import annotations

import random

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.runtime import AgentSpec, RuntimeConfig, run_agent, save_execution
from app.agents.tools import TOOLS
from app.clients.router import CUSTOM_PREFIX, parse_custom
from app.config import Settings, get_settings
from app.db import get_db
from app.mode import Mode, get_mode, require_admin
from app.models import Agent, FreshnessCheck, ModelConnection, TaskExecution
from app.schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    ExecutionDetailOut,
    ToolOut,
    TryAgentRequest,
)

router = APIRouter(tags=["agents"])


def _get_agent(db: Session, agent_id: int) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    return agent


def _commit_or_conflict(db: Session, name: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"an agent named '{name}' already exists") from exc


def _check_model(db: Session, model: str) -> None:
    """``custom/<slug>/<model>`` must name an existing connection (Bifrost ids are checked at call time)."""
    parsed = parse_custom(model)
    if model.startswith(CUSTOM_PREFIX) and parsed is None:
        raise HTTPException(
            status_code=422, detail="custom models look like custom/<connection-slug>/<model>"
        )
    if parsed and db.scalar(select(ModelConnection.id).where(ModelConnection.slug == parsed[0])) is None:
        raise HTTPException(
            status_code=422, detail=f"no model connection '{parsed[0]}': add it on the Models page first"
        )


def execution_detail(db: Session, execution: TaskExecution) -> ExecutionDetailOut:
    agent = db.get(Agent, execution.agent_id)
    return ExecutionDetailOut.model_validate(
        {
            **{c: getattr(execution, c) for c in ExecutionDetailOut.model_fields if hasattr(execution, c)},
            "agent_name": agent.name if agent else "(deleted)",
            "model": agent.model if agent else "",
            "trace": execution.trace,
            "freshness_check_id": db.scalar(
                select(FreshnessCheck.id)
                .where(FreshnessCheck.execution_id == execution.id)
                .order_by(FreshnessCheck.id.desc())
                .limit(1)
            ),
        }
    )


@router.get("/tools", response_model=list[ToolOut], summary="List tools agents can use")
def list_tools() -> list[ToolOut]:
    return [ToolOut(name=t.name, description=t.description, parameters=t.parameters) for t in TOOLS.values()]


@router.get("/agents", response_model=list[AgentOut], summary="List agents")
def list_agents(db: Session = Depends(get_db)) -> list[Agent]:
    return list(db.scalars(select(Agent).order_by(Agent.id)))


@router.post("/agents", response_model=AgentOut, status_code=201, summary="Create an agent")
def create_agent(body: AgentCreate, db: Session = Depends(get_db)) -> Agent:
    _check_model(db, body.model)
    agent = Agent(**body.model_dump())
    db.add(agent)
    _commit_or_conflict(db, body.name)
    db.refresh(agent)
    return agent


@router.get("/agents/{agent_id}", response_model=AgentOut, summary="Get one agent")
def get_agent(agent_id: int, db: Session = Depends(get_db)) -> Agent:
    return _get_agent(db, agent_id)


@router.put(
    "/agents/{agent_id}",
    response_model=AgentOut,
    summary="Replace an agent's configuration",
    dependencies=[Depends(require_admin)],
)
def update_agent(agent_id: int, body: AgentUpdate, db: Session = Depends(get_db)) -> Agent:
    agent = _get_agent(db, agent_id)
    _check_model(db, body.model)
    for key, value in body.model_dump().items():
        setattr(agent, key, value)
    _commit_or_conflict(db, body.name)
    db.refresh(agent)
    return agent


@router.delete(
    "/agents/{agent_id}",
    status_code=204,
    summary="Delete an agent and its executions",
    dependencies=[Depends(require_admin)],
)
def delete_agent(agent_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get_agent(db, agent_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/agents/{agent_id}/try",
    response_model=ExecutionDetailOut,
    summary="Run an agent once on a prompt",
    description=(
        "Executes the agent's tool-calling loop on a single prompt and returns the answer with its full "
        "trace. Useful for debugging an agent before benchmarking it. Saved as an execution with no run."
    ),
)
def try_agent(
    agent_id: int,
    body: TryAgentRequest,
    db: Session = Depends(get_db),
    mode: Mode = Depends(get_mode),
    settings: Settings = Depends(get_settings),
) -> ExecutionDetailOut:
    agent = _get_agent(db, agent_id)
    config = RuntimeConfig(
        fault_rate=body.fault_rate,
        sleep_scale=settings.mock_latency_scale if mode.mock else 1.0,
    )
    result = run_agent(
        AgentSpec.from_model(agent),
        body.prompt,
        mode.llm,
        mode.search,
        config=config,
        rng=random.Random(body.seed),
    )
    execution = save_execution(db, result, agent_id=agent.id, prompt=body.prompt)
    db.commit()
    db.refresh(execution)
    return execution_detail(db, execution)


@router.get(
    "/executions/{execution_id}",
    response_model=ExecutionDetailOut,
    tags=["executions"],
    summary="Get an execution with its full trace",
)
def get_execution(execution_id: int, db: Session = Depends(get_db)) -> ExecutionDetailOut:
    execution = db.get(TaskExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"execution {execution_id} not found")
    return execution_detail(db, execution)


@router.get(
    "/agents/{agent_id}/executions",
    response_model=list[ExecutionDetailOut],
    summary="Recent executions of an agent (newest first)",
)
def agent_executions(
    agent_id: int, limit: int = 20, db: Session = Depends(get_db)
) -> list[ExecutionDetailOut]:
    _get_agent(db, agent_id)
    rows = db.scalars(
        select(TaskExecution)
        .where(TaskExecution.agent_id == agent_id)
        .order_by(TaskExecution.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    return [execution_detail(db, e) for e in rows]
