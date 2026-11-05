# Agents

AgentPulse benchmarks **agents**, not just models. An agent is:

| Part | Field | Example |
|---|---|---|
| Model (routed through Bifrost) | `model` | `groq/openai/gpt-oss-20b` |
| Instructions | `system_prompt` | "Use web_search for anything that may have changed…" |
| Tools | `tools` | `["web_search", "calculator", "current_date"]` |
| Step budget | `max_steps` | `6` model calls per task |
| Sampling | `temperature` | `0.0` |

Two agents on the same model with different prompts or tools are different agents, and AgentPulse
measures them separately. The seeded **"GPT-OSS 120B · no tools (baseline)"** agent exists to show
exactly what tools add.

## Default agents

Seeded from [`backend/app/seed/default_agents.yaml`](../backend/app/seed/default_agents.yaml) on first start.
Seeding is idempotent: agents whose name already exists are never modified, so your edits survive restarts.
Set `SEED_ON_STARTUP=false` to disable it.

| Agent | Model | Tools | Notes |
|---|---|---|---|
| GPT-OSS 20B · tools | `groq/openai/gpt-oss-20b` | all 3 | Fast, cheap |
| GPT-OSS 120B · tools | `groq/openai/gpt-oss-120b` | all 3 | Strongest Groq model |
| Qwen3.8 27B · tools | `groq/qwen/qwen3.8-27b` | all 3 | Fastest in testing (~0.5 s per task) |
| GPT-OSS 120B · no tools (baseline) | `groq/openai/gpt-oss-120b` | none | Shows what tools add, e.g. it can't know recent facts |
| Nemotron Lightning · tools (OpenRouter free) | `openrouter/nvidia/nemotron-3.5-lightning:free` | all 3 | Free tier; can return 429s |
| Granite4 3B · tools (local Ollama) | `ollama/granite4:3b` | all 3 | $0, local; 15–30 s per task on CPU |

All six were verified live on 2026-11-05 to complete tool-calling tasks through Bifrost.

## Tools

Defined in [`backend/app/agents/tools.py`](../backend/app/agents/tools.py), exposed to models in OpenAI function-calling format.

| Tool | Arguments | Backed by | Notes |
|---|---|---|---|
| `web_search` | `query` | Tavily (or mock search) | Returns a summary plus up to 4 sources with publication dates |
| `calculator` | `expression` | Safe AST evaluator (no `eval`) | `+ - * / // % **` (`^` accepted), parentheses, `sqrt abs round min max log log10 exp`, `pi`, `e`, thousands separators. Rejects names, attributes, huge exponents and division by zero. |
| `current_date` | none | System clock (UTC) | e.g. "Today is Thursday, 2026-11-05 …" |

A failing tool returns `ERROR: <reason>` to the model instead of crashing, so the model can retry or change approach, just like with a real flaky API.

To add a tool, add a `Tool(...)` entry to `TOOLS` with a JSON-schema `parameters` block and a function `(args, ctx) -> str` that raises `ToolError` on failure. It then appears in `GET /tools` and the dashboard automatically.

## Runtime loop

[`backend/app/agents/runtime.py`](../backend/app/agents/runtime.py), `run_agent(spec, prompt, llm, search, config, rng)`:

```
messages = [system, user]
repeat up to max_steps:
    call model via Bifrost  ── retryable error (timeout/429/5xx)? → trace error + retry with backoff (500 ms, 1 s, …)
                            ── non-retryable or retries exhausted? → status = error, stop
    no tool calls?          → final answer, status = ok, stop
    for each tool call:
        with probability fault_rate → inject a fault instead of running the tool
        else run the tool (errors become "ERROR: …" messages)
        append the result as a `tool` message
status = max_steps (no final answer within budget)
```

Every model call, tool call, tool result, error and retry is recorded as a **trace step** with tokens and latency.

### Fault injection (reliability testing)

`fault_rate` (0–1) is the probability that **each tool call** is replaced by a fault:

| Fault | What the model sees | Simulated latency |
|---|---|---|
| `timeout` | `ERROR: tool timed out after 5s (injected fault)` | 5,000 ms |
| `http_500` | `ERROR: upstream service returned HTTP 500 (injected fault)` | 50–400 ms |
| `empty` | `ERROR: tool returned an empty response (injected fault)` | 50–400 ms |
| `malformed` | Truncated JSON with null bytes (no error marker, so it's harder to detect) | 50–400 ms |

Faults are drawn from a seeded RNG, so passing the same `seed` reproduces the same faults.

### Execution metrics

| Field | Meaning |
|---|---|
| `status` | `ok` (final answer), `error` (LLM failure), `max_steps` (budget exhausted) |
| `steps` | Number of model calls |
| `tool_calls` | Number of tool calls requested |
| `faults_injected` | Faults injected into this execution |
| `retries` | LLM call retries after transient errors |
| `recovered` | A failure happened (fault, tool error or LLM retry), yet the agent still produced an answer. Phase 3 tightens this to "…and the answer was correct". |
| `prompt_tokens` / `completion_tokens` | Sum over all model calls (as reported by the provider via Bifrost) |
| `latency_ms` | Sum of model latencies, tool latencies, simulated fault latency and retry backoff |

## Using it

**Dashboard → Agents page**
- The table lists all agents.
- **Try an agent:** pick an agent, prompt and fault rate, then run. You get the answer, metrics and a step-by-step trace. Injected faults are highlighted, and failed steps open automatically.
- **Create / Edit / Delete:** the model dropdown lists every model Bifrost can route to. Use "Custom model id…" for anything else.

**API**: see [api.md](api.md#agents).
