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
    fallbacks: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Models Bifrost tries in order when this agent's model fails (`provider/model`).",
        examples=[["openrouter/openai/gpt-oss-20b"]],
    )

    @field_validator("fallbacks", mode="before")
    @classmethod
    def _valid_fallbacks(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, list):
            bad = [m for m in v if not isinstance(m, str) or "/" not in m.strip() or m.startswith("custom/")]
            if bad:
                raise ValueError(f"fallbacks must be Bifrost model ids like groq/openai/gpt-oss-20b: {bad}")
            return [m.strip() for m in v]
        return v

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
    route: str | None = Field(default=None, description="`bifrost` (through the gateway) or `direct`.")
    connection: str | None = Field(default=None, description="Model connection name, for your own models.")


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
    fallback_calls: int | None = Field(
        default=None, description="Model calls answered by a Bifrost fallback."
    )
    gateway_ms: float | None = Field(
        default=None, description="Bifrost's own time over all model calls (ms)."
    )
    jury: list[dict] | None = Field(
        default=None,
        description="`jury` scorer: one vote per judge, {model, passed, score, reason} or {model, error}.",
    )
    created_at: datetime


class ExecutionDetailOut(ExecutionOut):
    agent_name: str
    model: str
    route: str | None = None
    connection: str | None = None
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
    event_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Date of the event the question is about (YYYY-MM-DD): drives the knowledge curve.",
    )
    source_url: str | None = Field(default=None, max_length=2000)
    source_title: str | None = Field(default=None, max_length=500)

    @field_validator("scorer")
    @classmethod
    def _known_scorer(cls, v: str) -> str:
        if v not in SCORERS:
            raise ValueError(f"unknown scorer '{v}'; available: {list(SCORERS)}")
        return v


class TaskOut(TaskIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    executions: int = Field(
        default=0, description="Executions that used this task (a used task can't be deleted)."
    )


class SuiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    tasks: list[TaskIn] = Field(default_factory=list, description="May be empty: add tasks later.")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("tasks")
    @classmethod
    def _unique_keys(cls, v: list[TaskIn]) -> list[TaskIn]:
        keys = [t.key for t in v]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate task keys: {dupes}")
        return v


class SuiteUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class SuiteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    builtin: bool = Field(
        default=False, description="Built-in suites are read-only: duplicate one to edit it."
    )
    runs: int = Field(
        default=0, description="Benchmark runs of this suite (a suite with runs can't be deleted)."
    )
    task_count: int
    categories: list[str]
    time_sensitive_count: int
    kind: str | None = Field(default=None, description="`freshbench` for a Live Freshness Bench snapshot.")
    snapshot_date: str | None = Field(default=None, description="When a Freshness Bench snapshot was made.")


class SuiteDetailOut(SuiteOut):
    tasks: list[TaskOut]


class ImportResult(BaseModel):
    suite: SuiteDetailOut
    warnings: list[str] = Field(default_factory=list)


class ScorePreviewIn(BaseModel):
    output: str = Field(max_length=8000, examples=["The answer is 1628.89."])
    scorer: str = Field(default="contains")
    expected: str = Field(default="", max_length=4000, examples=["1628.89"])
    tolerance: float = Field(default=0.0, ge=0.0)

    @field_validator("scorer")
    @classmethod
    def _known_scorer(cls, v: str) -> str:
        if v not in SCORERS:
            raise ValueError(f"unknown scorer '{v}'; available: {list(SCORERS)}")
        return v


class ScorePreviewOut(BaseModel):
    score: float
    passed: bool
    detail: str
    expected_rendered: str = Field(description="Expected answer after date placeholders are filled in.")


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
    outage_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=0.9,
        description="Outage drill: share of model calls failed by a simulated provider outage (chaos route).",
    )
    drill_fallbacks: bool = Field(
        default=True,
        description="Outage drill: let Bifrost fallbacks rescue failed calls (the agent's, else its model).",
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
    outage_rate: float | None = None
    drill_fallbacks: bool | None = None
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
    source: str | None = Field(default=None, description="text | execution | benchmark (automatic) | guard")
    model: str | None = Field(
        default=None, description="Freshness Guard: the model whose answer was checked."
    )
    guard_action: str | None = Field(
        default=None, description="Freshness Guard: annotated | corrected | blocked."
    )
    caller: str | None = Field(
        default=None, description="Freshness Guard: who called (key:<name>, oidc:<user>)."
    )
    latency_ms: float | None
    created_at: datetime
    counts: dict[str, int] = Field(default_factory=dict, description="Claims per verdict.")


class FreshnessCheckOut(FreshnessCheckSummary):
    claims: list[ClaimOut]


# ------------------------------------------------------------------------------------ model connections
_SLUG = r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$"


def _clean_models(v: list[str]) -> list[str]:
    out = list(dict.fromkeys(m.strip() for m in v if m and m.strip()))
    bad = [m for m in out if len(m) > 200 or any(c.isspace() for c in m)]
    if bad:
        raise ValueError(f"model names can't contain spaces or exceed 200 characters: {bad}")
    if len(out) > 100:
        raise ValueError("at most 100 models per connection")
    return out


class ConnectionBase(BaseModel):
    name: str = Field(min_length=1, max_length=80, examples=["My LM Studio"])
    preset: str | None = Field(
        default=None, description="Preset id from GET /connections/presets (informational)."
    )
    base_url: str = Field(
        min_length=8,
        max_length=500,
        description="API root that contains `/chat/completions`, e.g. `http://localhost:11434/v1`.",
        examples=["http://localhost:11434/v1"],
    )
    models: list[str] = Field(
        default_factory=list,
        description="Model names agents can use (`custom/<slug>/<model>`).",
        examples=[["llama3.2"]],
    )
    price_input_per_m: float | None = Field(
        default=None, ge=0, description="USD per 1M input tokens (optional)."
    )
    price_output_per_m: float | None = Field(
        default=None, ge=0, description="USD per 1M output tokens (optional)."
    )
    provider: str | None = Field(
        default=None,
        max_length=40,
        description="Bifrost provider name for hosted APIs (`openai`, `anthropic`, `gemini`, `mistral`, …).",
    )
    via_bifrost: bool = Field(
        default=False,
        description="Register this provider + key in Bifrost at runtime so calls go through the gateway "
        "(falls back to direct calls when that isn't possible; see `route_reason`).",
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("models")
    @classmethod
    def _models(cls, v: list[str]) -> list[str]:
        return _clean_models(v)


class ConnectionCreate(ConnectionBase):
    slug: str | None = Field(
        default=None,
        pattern=_SLUG,
        description="Id used in model ids (`custom/<slug>/<model>`); derived from the name if omitted. "
        "Can't be changed later.",
    )
    api_key: str | None = Field(
        default=None, max_length=1000, description="Optional. Stored encrypted, never returned."
    )


class ConnectionUpdate(ConnectionBase):
    api_key: str | None = Field(
        default=None, max_length=1000, description="New key; omit or null to keep the current one."
    )
    clear_api_key: bool = Field(default=False, description="Remove the stored key.")


class ConnectionOut(BaseModel):
    id: int
    name: str
    slug: str
    kind: str
    route: str = Field(description="`bifrost` (registered in the gateway) or `direct`.")
    provider: str | None = None
    route_reason: str | None = Field(default=None, description="Why this route was chosen.")
    bifrost_key_status: str | None = Field(default=None, description="Bifrost's own check of the key.")
    preset: str | None
    base_url: str
    has_api_key: bool
    key_hint: str | None
    models: list[str]
    model_ids: list[str] = Field(description="Ids to use as an agent's model.")
    discovered_models: list[str] | None
    supports_tools: bool | None
    price_input_per_m: float | None
    price_output_per_m: float | None
    last_check_ok: bool | None
    last_check_detail: str | None
    last_check_at: datetime | None
    agents: list[str] = Field(default_factory=list, description="Agents using this connection.")
    created_at: datetime
    updated_at: datetime


class ConnectionTestIn(BaseModel):
    """Check an endpoint before saving it (or a saved connection with `POST /connections/{id}/test`)."""

    base_url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(
        default=None, max_length=200, description="Model to try; omit to only list models."
    )


class ConnectionTestModel(BaseModel):
    model: str | None = Field(default=None, max_length=200)


class ConnectionTestOut(BaseModel):
    ok: bool = Field(
        description="True when the endpoint answered (models listed, or the test chat succeeded)."
    )
    base_url: str = Field(description="Normalised URL actually called.")
    hint: str | None = None
    models: list[str] | None = Field(
        default=None, description="From `GET <base>/models`, if the server lists them."
    )
    models_error: str | None = None
    model: str | None = None
    chat_ok: bool | None = None
    chat_latency_ms: float | None = None
    chat_reply: str | None = None
    chat_error: str | None = None
    tools_ok: bool | None = Field(
        default=None, description="The model produced a tool call (needed for tools)."
    )
    tools_detail: str | None = None


class PresetOut(BaseModel):
    id: str
    label: str
    group: str
    base_url: str
    needs_key: bool
    model_hint: str
    bifrost_provider: str | None
    via_bifrost: bool = Field(
        default=False, description="Can be registered in Bifrost (a native provider with no built-in key)."
    )
