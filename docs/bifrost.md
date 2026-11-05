# Bifrost integration

[Bifrost](https://github.com/maximhq/bifrost) is the AI gateway between AgentPulse and every LLM.
Agents, the LLM judge and the claim extractor all call **one** OpenAI-compatible endpoint, and
Bifrost routes each request to the right provider.

```
backend ── POST /v1/chat/completions {"model": "groq/openai/gpt-oss-20b", ...} ──▶ Bifrost ──▶ Groq
                                     {"model": "openrouter/...:free", ...}                ──▶ OpenRouter
                                     {"model": "ollama/granite4:3b", ...}                 ──▶ Ollama (host)
```

Why it matters for benchmarking: every agent goes through the **identical request path**
(same client code, same gateway, same timeout), so differences in latency, tokens and errors come
from the model and agent, not from different SDKs.

## Model ids

`<provider>/<model>`, exactly as listed by `make models` (`GET /v1/models`) or by the backend's `GET /models`.

| Provider | Example | Key |
|---|---|---|
| Groq | `groq/openai/gpt-oss-20b`, `groq/qwen/qwen3.8-27b` | `GROQ_API_KEY` |
| OpenRouter | `openrouter/nvidia/nemotron-3.5-lightning:free` | `OPENROUTER_API_KEY` |
| Ollama | `ollama/granite4:3b`, `ollama/qwen3.5:9b` | none (local) |

## Configuration (`bifrost/config.json`)

The file is validated against Bifrost's JSON schema (`$schema: https://www.getbifrost.ai/schema`).
It is mounted read-only at `/app/data/config.json`, and Bifrost's runtime state lives in the `bifrost-data` volume.

| Setting | Value | Why |
|---|---|---|
| `source_of_truth` | `"config.json"` | The file always wins over state persisted in the volume, so behaviour is reproducible. |
| `client.enforce_auth_on_inference` | `false` | No virtual key needed. **Local/demo only**: enable it for shared deployments. |
| `providers.*.keys[].value` | `"env.GROQ_API_KEY"` … | Secrets come from the environment and never from the file. |
| `providers.*.keys[].models` | `["*"]` | The key may serve any model of that provider. |
| `providers.ollama.keys[].ollama_key_config.url` | `env.OLLAMA_URL` | Defaults to `http://host.docker.internal:11434`. |
| `providers.ollama.network_config.allow_private_network` | `true` | `host.docker.internal` resolves to a private IP, which Bifrost's SSRF guard blocks by default. |

### Adding a provider

1. Add a block under `providers` (see the [Bifrost provider docs](https://docs.getbifrost.ai/)) with `"value": "env.MY_KEY"`.
2. Pass `MY_KEY` to the `bifrost` service in `docker-compose.yml` and add it to `.env.example`.
3. Run `docker compose up -d --force-recreate --wait bifrost`, then `make models` to confirm.

## Backend client (`backend/app/clients/llm.py`)

`BifrostClient` uses plain `httpx` rather than an SDK, which gives full control over:

- **Timeouts:** `LLM_TIMEOUT_S` (default 60 s), with a 10 s connect timeout.
- **Error classification** into `LLMError(retryable=...)`:

  | Condition | `kind` | retryable |
  |---|---|---|
  | Read timeout | `timeout` | ✅ |
  | Connection refused / DNS | `connection` | ✅ |
  | HTTP 429 | `rate_limited` | ✅ |
  | HTTP 5xx | `provider_error` | ✅ |
  | HTTP 4xx (bad model, bad request) | `provider_error` | ❌ |
  | Empty `choices` / non-JSON | `empty_response` / `bad_response` | ✅ |

- **Bifrost metadata:** `extra_fields.provider` is captured on each response.
- **No hidden retries:** the agent runtime (Phase 2) owns retry policy, so every retry shows up in the execution trace.

Tool calls follow the OpenAI format. Malformed tool-call arguments are kept (`{"_raw": ...}`) instead of raising an error, so that a model's broken output becomes a scored, traceable failure rather than a crash.

## Verifying

```bash
make models                                   # every routable model id
make llm-test MODEL=groq/openai/gpt-oss-20b   # one prompt
make live-check                               # health + 3 providers + tool calling + Tavily
```

Expected `make live-check` output:

```
  PASS  bifrost: health                                      http://bifrost:8080 ok
  PASS  bifrost: list models                                 groq=11, ollama=6, openrouter=493
  PASS  chat: groq/openai/gpt-oss-20b                        'pong' · 120 tok · 255 ms · groq
  PASS  chat: openrouter/nvidia/nemotron-3.5-lightning:free  'pong' · 138 tok · 2260 ms · openrouter
  PASS  chat: ollama/granite4:3b                             'pong' · 39 tok · 6056 ms · ollama
  PASS  tool calling: groq/openai/gpt-oss-20b                calculator({'expression': '17 * 23'}) · 359 ms
  PASS  tavily: news search                                  3 results, newest 2026-07-24 · 2562 ms

  7/7 checks passed
```

Test other models with `docker compose exec backend python -m app.scripts.live_check --models <id> <id> --tool-model <id>`.
