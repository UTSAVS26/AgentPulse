# API reference

Base URL: `http://localhost:8000`. The interactive OpenAPI docs at **http://localhost:8000/docs** have every
schema and a "Try it out" button. This page is a quick overview with `curl` examples (Git Bash / macOS / Linux syntax).

## System

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status of `database`, `llm` (Bifrost or mock), `search` (Tavily or mock). `status` is `ok` or `degraded`. |
| GET | `/models` | Every `provider/model` id Bifrost can route to (the mock's list in mock mode). 502 if Bifrost is unreachable. |

```bash
curl -s localhost:8000/health
curl -s localhost:8000/models
```

## Agents

| Method | Path | Description |
|---|---|---|
| GET | `/tools` | Tool catalogue (name, description, JSON-schema parameters) |
| GET | `/agents` | List agents |
| POST | `/agents` | Create an agent → `201` (`409` if the name exists, `422` on validation error) |
| GET | `/agents/{id}` | Get one agent (`404` if missing) |
| PUT | `/agents/{id}` | Replace an agent's configuration |
| DELETE | `/agents/{id}` | Delete an agent and its executions → `204` |
| POST | `/agents/{id}/try` | Run the agent once on a prompt; returns the execution with its full trace |
| GET | `/agents/{id}/executions?limit=20` | Recent executions of an agent, newest first |
| GET | `/executions/{id}` | One execution with its full trace |

Validation rules: `model` must look like `provider/model`, `tools` must be known tool names, `max_steps` is 1–20, `temperature` is 0–2.

```bash
# create
curl -s localhost:8000/agents -H 'content-type: application/json' -d '{
  "name": "My Qwen agent",
  "model": "groq/qwen/qwen3.8-27b",
  "tools": ["calculator", "web_search"],
  "system_prompt": "Be precise. Use tools when helpful.",
  "temperature": 0,
  "max_steps": 5
}'

# try it, with 50% of tool calls replaced by faults (reproducible with seed)
curl -s localhost:8000/agents/1/try -H 'content-type: application/json' \
  -d '{"prompt": "What is 17 * 23 + 4?", "fault_rate": 0.5, "seed": 7}'
```

Example `/try` response (abridged, shape of a live run of "GPT-OSS 20B · tools" through Bifrost):

```json
{
  "id": 12, "agent_name": "GPT-OSS 20B · tools", "model": "groq/openai/gpt-oss-20b",
  "status": "ok", "output": "17 × 23 + 4 equals 395.",
  "prompt_tokens": 612, "completion_tokens": 131, "latency_ms": 1290.4,
  "steps": 2, "tool_calls": 1, "faults_injected": 0, "retries": 0, "recovered": false,
  "trace": [
    {"idx": 0, "type": "llm_call",    "payload": {"tool_calls": [{"name": "calculator", "arguments": {"expression": "17 * 23 + 4"}}]}},
    {"idx": 1, "type": "tool_call",   "payload": {"name": "calculator", "arguments": {"expression": "17 * 23 + 4"}}},
    {"idx": 2, "type": "tool_result", "payload": {"name": "calculator", "ok": true, "content": "17 * 23 + 4 = 395"}},
    {"idx": 3, "type": "llm_call",    "payload": {"content": "17 × 23 + 4 equals 395."}},
    {"idx": 4, "type": "final",       "payload": {"content": "17 × 23 + 4 equals 395."}}
  ]
}
```

Trace step types: `llm_call`, `tool_call`, `tool_result` (with `fault` when injected), `error`, `retry`, `final`.
See [agents.md](agents.md) for the runtime semantics.

## Coming in later phases

Benchmark suites and runs (Phase 3), stale-answer detection (Phase 5), reports (Phase 6).
