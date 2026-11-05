from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import (
    Agent,
    BenchmarkRun,
    Claim,
    FreshnessCheck,
    Task,
    TaskExecution,
    TaskSuite,
    TraceStep,
)

EXPECTED_TABLES = {
    "agents",
    "task_suites",
    "tasks",
    "benchmark_runs",
    "task_executions",
    "trace_steps",
    "freshness_checks",
    "claims",
}


def test_init_db_creates_all_tables(db_engine):
    assert EXPECTED_TABLES <= set(inspect(db_engine).get_table_names())


def test_object_graph_roundtrip(db_engine):
    with Session(db_engine) as s:
        agent = Agent(name="a1", model="groq/openai/gpt-oss-20b", tools=["web_search", "calculator"])
        suite = TaskSuite(name="s1", tasks=[Task(key="t1", prompt="2+2?", expected="4", scorer="numeric")])
        s.add_all([agent, suite])
        s.flush()
        run = BenchmarkRun(suite_id=suite.id, agent_ids=[agent.id], repeats=2, fault_rate=0.2)
        s.add(run)
        s.flush()
        execution = TaskExecution(
            run_id=run.id,
            agent_id=agent.id,
            task_id=suite.tasks[0].id,
            output="4",
            trace=[
                TraceStep(idx=1, type="final", payload={"content": "4"}),
                TraceStep(idx=0, type="llm_call", payload={"messages": 2}),
            ],
        )
        check = FreshnessCheck(
            input_text="The UK PM is Keir Starmer.",
            claims=[
                Claim(text="UK PM is Keir Starmer", sources=[{"url": "https://e.x", "date": "2026-07-24"}])
            ],
        )
        s.add_all([execution, check])
        s.commit()
        execution_id, claim_id, agent_id = execution.id, check.claims[0].id, agent.id

    with Session(db_engine) as s:
        loaded = s.get(TaskExecution, execution_id)
        assert [t.type for t in loaded.trace] == ["llm_call", "final"]  # ordered by idx
        assert loaded.run.repeats == 2
        assert s.get(Agent, agent_id).tools == ["web_search", "calculator"]
        assert s.get(Claim, claim_id).sources == [{"url": "https://e.x", "date": "2026-07-24"}]


def test_deleting_run_cascades_to_executions_and_traces(db_engine):
    with Session(db_engine) as s:
        agent = Agent(name="a2", model="ollama/granite4:3b")
        suite = TaskSuite(name="s2")
        s.add_all([agent, suite])
        s.flush()
        run = BenchmarkRun(suite_id=suite.id, agent_ids=[agent.id])
        run.executions.append(
            TaskExecution(agent_id=agent.id, trace=[TraceStep(idx=0, type="llm_call", payload={})])
        )
        s.add(run)
        s.commit()

        s.delete(run)
        s.commit()
        assert s.query(TaskExecution).count() == 0
        assert s.query(TraceStep).count() == 0
        assert s.query(Agent).count() == 1  # agents are not owned by runs
