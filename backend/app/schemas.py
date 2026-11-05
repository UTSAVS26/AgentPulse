"""Pydantic request/response schemas for the public API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.tools import TOOLS
from app.bench.scoring import SCORERS


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
    tool_errors: int | None = 0
    recovered: bool
    score_detail: str | None = None
    error: str | None
    freshness_score: float | None
    freshness_verdict: str | None = Field(
        default=None, description="Stale-answer check of this answer: fresh | stale | wrong | unverified..."
    )
    created_at: datetime


class ExecutionDetailOut(ExecutionOut):
    agent_name: str
    model: str
    trace: list[TraceStepOut]
    freshness_check_id: int | None = Field(default=None, description="Latest stale-answer check, if any.")


# ------------------------------------------------------------------------------------ suites
class TaskIn(BaseModel):
    key: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$", examples=["arith-basic"])
    prompt: str = Field(min_length=1, max_length=8000)
    expected: str = Field(default="", max_length=4000, description="Use `|` for alternatives (contains).")
    scorer: str = Field(default="contains", description=f"One of {', '.join(SCORERS)}.")
    tolerance: float = Field(default=0.0, ge=0.0)
    category: str = Field(default="general", max_length=60)
    time_sensitive: bool = False
    stale_answer: str | None = Field(default=None, max_length=1000)

    @field_validator("scorer")
    @classmethod
    def _known_scorer(cls, v: str) -> str:
        if v not in SCORERS:
            raise ValueError(f"unknown scorer '{v}'; available: {list(SCORERS)}")
        return v


class TaskOut(TaskIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


class SuiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    tasks: list[TaskIn] = Field(min_length=1)

    @field_validator("tasks")
    @classmethod
    def _unique_keys(cls, v: list[TaskIn]) -> list[TaskIn]:
        keys = [t.key for t in v]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate task keys: {dupes}")
        return v


class SuiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    task_count: int
    categories: list[str]
    time_sensitive_count: int


class SuiteDetailOut(SuiteOut):
    tasks: list[TaskOut]


# ------------------------------------------------------------------------------------ runs
class RunCreate(BaseModel):
    suite_id: int
    agent_ids: list[int] = Field(min_length=1)
    repeats: int = Field(default=1, ge=1, le=10, description="Times each agent attempts each task.")
    fault_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Per-tool-call fault probability.")
    check_freshness: bool = Field(
        default=True,
        description="Run the stale-answer detector (Tavily) on answers to time-sensitive tasks.",
    )


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    suite_id: int
    suite_name: str
    agent_ids: list[int]
    agent_names: list[str]
    repeats: int
    fault_rate: float
    mock_mode: bool
    check_freshness: bool | None = None
    status: str
    progress_done: int
    progress_total: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_s: float | None


class RunExecutionOut(ExecutionOut):
    agent_name: str
    task_key: str
    category: str


# ------------------------------------------------------------------------------------ freshness
class FreshnessCheckRequest(BaseModel):
    """Check either a pasted text (optionally with the question it answers) or an execution's answer."""

    text: str | None = Field(
        default=None,
        max_length=6000,
        examples=["The current UK Prime Minister is Rishi Sunak. Water boils at 100 °C at sea level."],
    )
    question: str | None = Field(
        default=None, max_length=2000, description="The question that `text` answers."
    )
    execution_id: int | None = Field(
        default=None, description="Check this execution's answer (its prompt is used as the question)."
    )
    search_depth: Literal["basic", "advanced"] = Field(
        default="advanced", description="Tavily search depth (advanced = better recall, 2 credits)."
    )

    @model_validator(mode="after")
    def _one_source(self) -> FreshnessCheckRequest:
        has_text = bool(self.text and self.text.strip())
        if has_text == (self.execution_id is not None):
            raise ValueError("provide exactly one of `text` or `execution_id`")
        return self


class SourceOut(BaseModel):
    title: str = ""
    url: str = ""
    published_date: str | None = None
    snippet: str | None = None


class ClaimOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    text: str
    time_sensitive: bool
    query: str | None
    topic: str | None = None
    signals: list[str] | None = None
    verdict: str = Field(description="current | outdated | contradicted | unverifiable | not_time_sensitive")
    confidence: float
    explanation: str
    correction: str | None = None
    method: str | None = Field(
        default=None, description="llm | clock | skipped | search-error | no-sources | llm-error"
    )
    sources: list[SourceOut]


class FreshnessCheckSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    execution_id: int | None
    input_text: str
    question: str | None
    freshness_score: float | None = Field(
        description="current ÷ (current + outdated + contradicted) over time-sensitive claims; null if none."
    )
    verdict: str | None = Field(description="fresh | stale | wrong | unverified | timeless | no_claims")
    extraction: str | None
    mock_mode: bool | None
    source: str | None = Field(default=None, description="text | execution | benchmark (automatic)")
    latency_ms: float | None
    created_at: datetime
    counts: dict[str, int] = Field(default_factory=dict, description="Claims per verdict.")


class FreshnessCheckOut(FreshnessCheckSummary):
    claims: list[ClaimOut]
