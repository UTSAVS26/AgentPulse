"""Pydantic request/response schemas for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.tools import TOOLS


# ------------------------------------------------------------------------------------ agents
class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["Groq GPT-OSS 20B + tools"])
    description: str = Field(default="", max_length=2000)
    model: str = Field(
        min_length=3,
        max_length=200,
        pattern=r"^[A-Za-z0-9_.-]+/.+$",
        description="Bifrost model id: `provider/model`.",
        examples=["groq/openai/gpt-oss-20b"],
    )
    system_prompt: str = Field(default="", max_length=8000)
    tools: list[str] = Field(default_factory=list, examples=[["web_search", "calculator", "current_date"]])
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_steps: int = Field(default=6, ge=1, le=20, description="Maximum model calls per task.")

    @field_validator("tools")
    @classmethod
    def _known_tools(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in TOOLS]
        if unknown:
            raise ValueError(f"unknown tools {unknown}; available: {sorted(TOOLS)}")
        return list(dict.fromkeys(v))  # dedupe, keep order

    @field_validator("name", "model")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AgentCreate(AgentBase):
    pass


class AgentUpdate(AgentBase):
    pass


class AgentOut(AgentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ToolOut(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


# ------------------------------------------------------------------------------------ executions
class TryAgentRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000, examples=["What is 17 * 23 + 4?"])
    fault_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Probability that each tool call is replaced by a fault."
    )
    seed: int | None = Field(default=None, description="Seed for fault injection (reproducible runs).")


class TraceStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    idx: int
    type: str
    payload: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


class ExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None
    agent_id: int
    task_id: int | None
    repeat_idx: int
    prompt: str
    output: str
    status: str
    score: float | None
    passed: bool | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    steps: int
    tool_calls: int
    faults_injected: int
    retries: int
    recovered: bool
    error: str | None
    freshness_score: float | None
    created_at: datetime


class ExecutionDetailOut(ExecutionOut):
    agent_name: str
    model: str
    trace: list[TraceStepOut]
