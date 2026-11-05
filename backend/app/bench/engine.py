"""Benchmark orchestration: run a task suite × agents × repeats, score and persist every execution.

Execution model
---------------
* A run executes in a background thread (``BenchmarkEngine.start``); the API returns immediately and
  clients poll ``GET /runs/{id}`` for progress.
* Agents run **in parallel** (one worker per agent); each agent processes its tasks **sequentially**,
  repeat-major (all tasks once, then again...). That keeps per-model rate limits manageable and gives
  every agent partial results early.
* Each execution is committed as soon as it finishes, so a crash or cancel never loses finished work.
* A failure inside one execution (even a bug) is recorded on that execution and the run continues.
* Fault injection is seeded from ``(run, agent, task, repeat)`` so a run is reproducible.
"""

from __future__ import annotations

import logging
import random
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agents.runtime import AgentSpec, RuntimeConfig, run_agent, save_execution
from app.bench.pricing import PriceBook
from app.bench.scoring import ScoreResult, render_expected, score_judge, score_output
from app.clients import LLMClient, MockLLMClient, SearchClient, clients_for
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.models import Agent, BenchmarkRun, Task, TaskExecution, TaskSuite

log = logging.getLogger(__name__)

MAX_EXECUTIONS_PER_RUN = 2000


class RunValidationError(ValueError):
    """Invalid run request (unknown suite/agents, empty suite, too large)."""


class InlineExecutor(Executor):
    """Runs submitted work synchronously; used in tests."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        fn(*args, **kwargs)


ClientFactory = Callable[[bool], tuple[LLMClient, SearchClient]]


@dataclass(frozen=True)
class _RunContext:
    """Clients and price book for one run (live or mock)."""

    llm: LLMClient
    search: SearchClient
    live: bool
    prices: PriceBook


class BenchmarkEngine:
    """Runs benchmarks. Each run is live or mock (decided per request and stored on the run);
    ``clients(live)`` returns the matching (LLM, search) pair."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        settings: Settings,
        clients: ClientFactory,
        prices: dict[bool, PriceBook],
        executor: Executor | None = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.clients = clients
        self.prices = prices  # keyed by live: {True: PriceBook(bifrost), False: PriceBook(None)}
        self.executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="bench-run")
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------ lifecycle
    def create_run(
        self,
        db: Session,
        *,
        suite_id: int,
        agent_ids: list[int],
        repeats: int,
        fault_rate: float,
        live: bool = False,
    ) -> BenchmarkRun:
        suite = db.get(TaskSuite, suite_id)
        if suite is None:
            raise RunValidationError(f"suite {suite_id} not found")
        if not suite.tasks:
            raise RunValidationError(f"suite '{suite.name}' has no tasks")
        agent_ids = list(dict.fromkeys(agent_ids))
        if not agent_ids:
            raise RunValidationError("select at least one agent")
        found = set(db.scalars(select(Agent.id).where(Agent.id.in_(agent_ids))))
        missing = [a for a in agent_ids if a not in found]
        if missing:
            raise RunValidationError(f"agents not found: {missing}")
        total = len(suite.tasks) * len(agent_ids) * repeats
        if total > MAX_EXECUTIONS_PER_RUN:
            raise RunValidationError(f"run too large: {total} executions (max {MAX_EXECUTIONS_PER_RUN})")
        run = BenchmarkRun(
            suite_id=suite_id,
            agent_ids=agent_ids,
            repeats=repeats,
            fault_rate=fault_rate,
            mock_mode=not live,
            status="pending",
            progress_total=total,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def start(self, run_id: int) -> None:
        self.executor.submit(self.execute, run_id)

    def cancel(self, db: Session, run_id: int) -> BenchmarkRun | None:
        run = db.get(BenchmarkRun, run_id)
        if run is None:
            return None
        if run.status in ("pending", "running"):
            with self._lock:
                self._cancelled.add(run_id)
            run.status = "cancelled"
            run.finished_at = datetime.now(UTC)
            db.commit()
        return run

    def _is_cancelled(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._cancelled

    # ------------------------------------------------------------------------------ execution
    def execute(self, run_id: int) -> None:
        """Run a benchmark to completion (blocking)."""
        with self.sessions() as db:
            run = db.get(BenchmarkRun, run_id)
            if run is None or run.status != "pending":
                return
            run.status = "running"
            run.started_at = datetime.now(UTC)
            db.commit()
            tasks = [
                {
                    "id": t.id,
                    "prompt": t.prompt,
                    "expected": t.expected,
                    "scorer": t.scorer,
                    "tolerance": t.tolerance,
                    "stale_answer": t.stale_answer,
                }
                for t in db.get(TaskSuite, run.suite_id).tasks
            ]
            agents = [
                (a.id, AgentSpec.from_model(a))
                for a in db.scalars(select(Agent).where(Agent.id.in_(run.agent_ids)))
            ]
            repeats, fault_rate, live = run.repeats, run.fault_rate, not run.mock_mode

        llm, search = self.clients(live)
        ctx = _RunContext(llm=llm, search=search, live=live, prices=self.prices[live])
        self._prime_mock(ctx.llm, tasks)
        try:
            with ThreadPoolExecutor(
                max_workers=max(1, len(agents)), thread_name_prefix="bench-agent"
            ) as pool:
                futures = [
                    pool.submit(
                        self._run_agent_tasks, ctx, run_id, agent_id, spec, tasks, repeats, fault_rate
                    )
                    for agent_id, spec in agents
                ]
                for f in futures:
                    f.result()
            final_status = "cancelled" if self._is_cancelled(run_id) else "done"
            error = None
        except Exception as exc:  # pragma: no cover - defensive; per-execution errors are caught below
            log.exception("Run %s failed", run_id)
            final_status, error = "failed", f"{type(exc).__name__}: {exc}"

        with self.sessions() as db:
            run = db.get(BenchmarkRun, run_id)
            if run.status != "cancelled":
                run.status = final_status
            run.error = error
            run.finished_at = run.finished_at or datetime.now(UTC)
            db.commit()
        with self._lock:
            self._cancelled.discard(run_id)

    @staticmethod
    def _prime_mock(llm: LLMClient, tasks: list[dict]) -> None:
        """In mock mode, teach the fake LLM the right (and stale) answers so scores are meaningful."""
        if isinstance(llm, MockLLMClient):
            llm.set_answer_key(
                {t["prompt"]: render_expected(t["expected"]).split("|")[0] for t in tasks},
                {t["prompt"]: t["stale_answer"] for t in tasks if t["stale_answer"]},
            )

    def _judge(self, ctx: _RunContext) -> Callable[[str, str, str], ScoreResult] | None:
        if not ctx.live:
            return None  # contains fallback, clearly labelled in score_detail

        def judge(output: str, expected: str, question: str) -> ScoreResult:
            return score_judge(output, expected, question, ctx.llm, self.settings.judge_model)

        return judge

    def _run_agent_tasks(
        self,
        ctx: _RunContext,
        run_id: int,
        agent_id: int,
        spec: AgentSpec,
        tasks: list[dict],
        repeats: int,
        fault_rate: float,
    ) -> None:
        config = RuntimeConfig(
            fault_rate=fault_rate,
            sleep_scale=1.0 if ctx.live else self.settings.mock_latency_scale,
        )
        judge = self._judge(ctx)
        for repeat in range(repeats):
            for task in tasks:
                if self._is_cancelled(run_id):
                    return
                self._run_one(ctx, run_id, agent_id, spec, task, repeat, config, judge)

    def _run_one(
        self,
        ctx: _RunContext,
        run_id: int,
        agent_id: int,
        spec: AgentSpec,
        task: dict,
        repeat: int,
        config: RuntimeConfig,
        judge: Callable[[str, str, str], ScoreResult] | None,
    ) -> None:
        rng = random.Random(f"{run_id}|{agent_id}|{task['id']}|{repeat}")
        with self.sessions() as db:
            try:
                result = run_agent(spec, task["prompt"], ctx.llm, ctx.search, config, rng)
                sr = score_output(
                    result.output,
                    scorer=task["scorer"],
                    expected=task["expected"],
                    tolerance=task["tolerance"],
                    question=task["prompt"],
                    judge=judge,
                )
                cost, price_source = ctx.prices.cost(
                    spec.model, result.prompt_tokens, result.completion_tokens
                )
                execution = save_execution(
                    db,
                    result,
                    agent_id=agent_id,
                    prompt=task["prompt"],
                    run_id=run_id,
                    task_id=task["id"],
                    repeat_idx=repeat,
                )
                execution.score = sr.score
                execution.passed = sr.passed
                execution.score_detail = f"{sr.detail} · price: {price_source}"
                execution.cost_usd = cost
                execution.recovered = result.had_failures and sr.passed
            except Exception as exc:  # a bug must not kill the whole run
                db.rollback()
                log.exception("Execution failed (run %s, agent %s, task %s)", run_id, agent_id, task["id"])
                db.add(
                    TaskExecution(
                        run_id=run_id,
                        agent_id=agent_id,
                        task_id=task["id"],
                        repeat_idx=repeat,
                        prompt=task["prompt"],
                        status="error",
                        score=0.0,
                        passed=False,
                        error=f"internal error: {type(exc).__name__}: {exc}",
                        score_detail=traceback.format_exc(limit=3)[-1000:],
                    )
                )
            db.execute(
                update(BenchmarkRun)
                .where(BenchmarkRun.id == run_id)
                .values(progress_done=BenchmarkRun.progress_done + 1)
            )
            db.commit()


def mark_interrupted_runs(db: Session) -> int:
    """Runs left pending/running by a previous process can never finish: mark them failed."""
    runs = list(db.scalars(select(BenchmarkRun).where(BenchmarkRun.status.in_(["pending", "running"]))))
    for run in runs:
        run.status = "failed"
        run.error = "interrupted: the backend restarted while this run was in progress"
        run.finished_at = datetime.now(UTC)
    db.commit()
    return len(runs)


@lru_cache
def get_bench_engine() -> BenchmarkEngine:
    s = get_settings()
    return BenchmarkEngine(
        sessions=get_sessionmaker(),
        settings=s,
        clients=clients_for,
        prices={True: PriceBook(s.bifrost_url), False: PriceBook(None)},
    )


def task_lookup(db: Session, suite_id: int) -> dict[int, dict]:
    return {
        t.id: {"key": t.key, "category": t.category, "time_sensitive": t.time_sensitive, "prompt": t.prompt}
        for t in db.scalars(select(Task).where(Task.suite_id == suite_id).order_by(Task.id))
    }
