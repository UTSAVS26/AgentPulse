"""ORM models: agents, task suites, benchmark runs, executions, traces and freshness checks.

Column types are portable (generic ``JSON``), so the same models run on PostgreSQL in
Docker and on SQLite in unit tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Agent(Base):
    """An agent = model + system prompt + tool set + step budget."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(200))  # Bifrost id, e.g. "groq/openai/gpt-oss-20b"
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    max_steps: Mapped[int] = mapped_column(Integer, default=6)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskSuite(Base):
    __tablename__ = "task_suites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tasks: Mapped[list[Task]] = relationship(
        back_populates="suite", cascade="all, delete-orphan", order_by="Task.id"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("task_suites.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))  # stable id within the suite, e.g. "capital-france"
    prompt: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text, default="")
    scorer: Mapped[str] = mapped_column(String(30), default="contains")  # exact|contains|numeric|regex|judge
    tolerance: Mapped[float] = mapped_column(Float, default=0.0)
    category: Mapped[str] = mapped_column(String(60), default="general")
    time_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)

    suite: Mapped[TaskSuite] = relationship(back_populates="tasks")


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("task_suites.id"), index=True)
    agent_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    repeats: Mapped[int] = mapped_column(Integer, default=1)
    fault_rate: Mapped[float] = mapped_column(Float, default=0.0)
    mock_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|failed
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    executions: Mapped[list[TaskExecution]] = relationship(back_populates="run", cascade="all, delete-orphan")


class TaskExecution(Base):
    """One agent attempting one task once (a single repeat)."""

    __tablename__ = "task_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True, nullable=True
    )  # NULL for ad-hoc "try this agent" executions
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    repeat_idx: Mapped[int] = mapped_column(Integer, default=0)
    prompt: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|error|max_steps
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    faults_injected: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    freshness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[BenchmarkRun | None] = relationship(back_populates="executions")
    trace: Mapped[list[TraceStep]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", order_by="TraceStep.idx"
    )


class TraceStep(Base):
    __tablename__ = "trace_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("task_executions.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))  # llm_call|tool_call|tool_result|error|retry|final
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    execution: Mapped[TaskExecution] = relationship(back_populates="trace")


class FreshnessCheck(Base):
    __tablename__ = "freshness_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_executions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    input_text: Mapped[str] = mapped_column(Text)
    freshness_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="done")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    claims: Mapped[list[Claim]] = relationship(
        back_populates="check", cascade="all, delete-orphan", order_by="Claim.id"
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("freshness_checks.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    time_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verdict: Mapped[str] = mapped_column(String(20), default="unverifiable")
    # verdict: current|outdated|contradicted|unverifiable|not_time_sensitive
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[str] = mapped_column(Text, default="")

    check: Mapped[FreshnessCheck] = relationship(back_populates="claims")
