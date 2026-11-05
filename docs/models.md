# Models: bring your own

Agents can use two kinds of models:

| Kind | Model id | How it is called |
|---|---|---|
| **Built-in** | `groq/…`, `openrouter/…`, `ollama/…` | Through the **Bifrost** gateway, using the keys in `.env` (see [bifrost.md](bifrost.md)) |
| **Your own** (a *model connection*) | `custom/<slug>/<model>` | **Directly**, by the backend, to the connection's OpenAI-compatible base URL |

A connection is any server that speaks the OpenAI chat API (`POST <base>/chat/completions`). That covers
almost every local runtime and most hosted providers. Add one on the **🔌 Models** page, or with
`POST /connections` ([api.md](api.md#model-connections)).

> v2.3 adds runtime registration of hosted providers *in Bifrost* (OpenAI, Anthropic, Gemini…), so their traffic
> also goes through the gateway. Until then, hosted providers connect through their OpenAI-compatible endpoints.

## Adding a connection

1. **Models** → **➕ Add a model connection** → pick a preset. It fills in the base URL.
2. Enter the **API key** if the server needs one. Local servers usually don't.
3. **🔍 Fetch models** lists what the server offers (`GET <base>/models`). Pick some, or type names.
4. **🧪 Test first model**: sends "Reply with exactly the word: pong", measures latency, and checks **tool
   calling** with the calculator tool.
5. **💾 Save connection**, then **🤖 New agent**. The Agents page opens with the model pre-selected.

Connection models appear in the Agents page's model picker marked 🔌, and work everywhere a built-in model
does: try-an-agent, benchmarks, Compare, Traces and Reports.

### Presets

| Preset | Base URL | Key |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | – |
| LM Studio | `http://localhost:1234/v1` | – |
| vLLM | `http://localhost:8000/v1` | optional |
| llama.cpp server | `http://localhost:8080/v1` | – |
| Jan | `http://localhost:1337/v1` | – |
| OpenAI | `https://api.openai.com/v1` | required |
| Anthropic (OpenAI-compatible) | `https://api.anthropic.com/v1` | required |
| Google Gemini (OpenAI-compatible) | `https://generativelanguage.googleapis.com/v1beta/openai` | required |
| Mistral · DeepSeek · xAI · Cerebras · Together · Fireworks · Groq · OpenRouter | their `/v1` endpoints | required |
| Custom | anything | optional |

The local presets' default ports collide with AgentPulse's own on the host: vLLM's 8000 is the backend and
llama.cpp's 8080 is Bifrost. If you run those servers, start them on another port and edit the URL.

### `localhost` and Docker

Inside the backend container, `localhost` is the container itself. Compose sets
`LOCALHOST_ALIAS=host.docker.internal` (plus an `extra_hosts` entry), so `http://localhost:11434/v1` is
rewritten to reach **your machine**, and the UI says so. If your server listens on `127.0.0.1` only, it may still
be unreachable from Docker. Start it on all interfaces: `OLLAMA_HOST=0.0.0.0`, or LM Studio's *Serve on local
network* option.

### Tool calling

Agents with tools need models that emit OpenAI-style `tool_calls`. The connection test checks this, and the
result is shown as 🛠️ tools ✓/✗. When you give a ✗ model tools, the Agents page warns you; such a model works
best in an agent without tools.

### Prices

Connections are $0 by default, which is right for local models. For a hosted provider, set prices per 1M
input and output tokens on the connection: benchmark costs then use them, with the price source shown as
`connection`.

## Security

| Concern | What AgentPulse does |
|---|---|
| **Stored API keys** | Encrypted at rest with Fernet (AES + HMAC) under a key derived from `APP_SECRET`. **Never returned by the API**: responses carry only `has_api_key` and `key_hint` (`sk-…a1b2`). A blank key on edit keeps the stored one; *Remove the stored key* clears it. |
| **Keys in errors** | Every error message from a connection is scrubbed of its key before it is logged, stored or shown. Account ids in provider errors (e.g. Groq's `org_…`) are redacted too. |
| **`APP_SECRET`** | Set it in `.env` for anything long-lived. Without it, a random secret is generated once (compose keeps it in the `appdata` volume). Render generates one per service. Keys saved under one secret can't be decrypted under another; re-enter them. |
| **Server-side request forgery** | Base URLs must be `http(s)`, with no credentials in the URL. On the **public deployment** the host is resolved and must be public: private, loopback, link-local (cloud metadata), reserved and multicast addresses, plus `*.internal`, `*.local` and `host.docker.internal`, are rejected. Locally, private addresses are allowed (that's where Ollama lives). Override with `ALLOW_PRIVATE_ENDPOINTS=true/false`. |
| **Public demo** | Listing connections is open (no secrets in it). Creating, editing, deleting and testing need the **access code**, because they make the server call a URL and store a key. |
| **Mock mode** | Agents on connection models run **simulated**, like every other model. Nothing is called until live mode. |

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `APP_SECRET` | *(generated)* | Encrypts stored connection API keys |
| `APP_SECRET_FILE` | temp directory; `/data/app_secret` in compose | Where a generated secret is kept |
| `LOCALHOST_ALIAS` | *(empty)*; `host.docker.internal` in compose | What `localhost` in a connection URL is rewritten to |
| `ALLOW_PRIVATE_ENDPOINTS` | allowed locally, blocked on the public demo | Network policy for connection URLs |

## How it works

`app/clients/router.py`:

- `ModelRouter` implements the same `LLMClient` interface as the Bifrost client. Model ids starting with
  `custom/<slug>/` go to that connection's `OpenAICompatibleClient`, which is sent the bare `<model>`; every
  other id goes to Bifrost.
- The agent runtime, benchmark engine, LLM judge and stale-answer detector therefore need no changes.
- Clients are cached per connection and rebuilt when the connection's `updated_at` changes. The URL policy is
  re-checked on every rebuild.

## Validated live (2026-11-05)

- A local **Ollama** entered as `http://localhost:11434/v1`: the URL was rewritten to `host.docker.internal`,
  all 6 models were discovered, `granite4:3b` answered "pong" in 8.5 s, and tool calling worked.
- An agent `custom/my-ollama/granite4:3b` with the calculator tool answered 17 × 23 + 4 = **395**.
- A live **Math & Reasoning** benchmark ran it next to `groq/openai/gpt-oss-20b` through Bifrost. Both scored
  100%; the local model had p50 7.6 s at $0 (price source `connection (no price set)`), against 1.0 s and
  $0.0005 for Groq.
