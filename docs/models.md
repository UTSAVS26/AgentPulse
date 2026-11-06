# Models: bring your own

Agents can use two kinds of models:

| Kind | Model id | How it is called |
|---|---|---|
| **Built-in** | `groq/…`, `openrouter/…`, `ollama/…` | Through the **Bifrost** gateway, using the keys in `.env` (see [bifrost.md](bifrost.md)) |
| **Your own** (a *model connection*) | `custom/<slug>/<model>` | **Through Bifrost** for hosted providers it supports natively (registered at runtime with your key), otherwise **directly** to the connection's OpenAI-compatible base URL |

A connection is any server that speaks the OpenAI chat API (`POST <base>/chat/completions`). That covers
almost every local runtime and most hosted providers. Add one on the **🔌 Models** page, or with
`POST /connections` ([api.md](api.md#model-connections)).

## Routes: through Bifrost or direct

Every connection shows its **route** (🛰️ via Bifrost or ↗️ direct) and **why**:

| Connection | Route | Why |
|---|---|---|
| OpenAI, Anthropic, Gemini, Mistral, DeepSeek, xAI, Cerebras, Cohere, Perplexity, with *Route through the Bifrost gateway* ticked (the default) | 🛰️ **Bifrost** | The provider and your key are **registered in Bifrost at runtime**, so calls take the same gateway path as the built-in models (fair comparisons, one audit trail, catalogue pricing). |
| Self-hosted (Ollama, LM Studio, vLLM, llama.cpp…) or a custom URL | ↗️ direct | An arbitrary `base_url` can only be set through Bifrost's API with an admin session |
| Groq or OpenRouter with your own key | ↗️ direct | Bifrost already holds keys for them and **load-balances across all keys of a provider**, so yours would be mixed with the built-in key |
| A second connection to a provider that is already routed through Bifrost | ↗️ direct | Same reason: the two keys would be shared |
| No `BIFROST_SETUP_TOKEN`, Bifrost without a config store (the free Render deployment), or registration failed | ↗️ direct | The connection still works, through the provider's OpenAI-compatible endpoint |

On save, Bifrost **checks the key itself**, and the result is shown on the connection (e.g. "Bifrost key check:
API key not valid"). Agents always use `custom/<slug>/<model>`, so a connection can switch route without
breaking any agent. Bifrost-routed calls are sent to Bifrost as `<provider>/<model>`.

**Bifrost forgets runtime registrations when it restarts** (its `config.json` is the source of truth), so
AgentPulse's database is the source of truth instead:

- testing a connection re-registers it;
- when Bifrost answers "unknown provider", the call **re-registers the connection and retries once**
  (self-healing).

Deleting a connection, or switching it to direct, removes its key from Bifrost, and the provider too once it
has no keys left. Providers from `config.json` are never touched. Details: [bifrost.md](bifrost.md#runtime-provider-registration).

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
| `BIFROST_SETUP_TOKEN` | *(empty)* | Shared by Bifrost and the backend: unlocks Bifrost's management API for runtime registration. Empty = hosted connections are called directly. |
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

## Validated live (2026-11-06): a real Gemini key through Bifrost

With a real key, a Gemini connection was registered in Bifrost at runtime ("key accepted by Bifrost"):

- Bifrost listed the **61 models** the key can use.
- `gemini-flash-lite-latest` answered "pong" in 2.5 s **through Bifrost**, and tool calling worked
  (`calculator(17 * 23)`).

Found and fixed during that test:

- **Google lists models as `models/<name>`, but Bifrost expects bare names** (`models/…` → 404). The prefix is
  now stripped on the Bifrost route.
- **A saved connection's test used the 120 s run timeout** instead of the test's 45 s: a slow model kept the
  button busy for 2 minutes. It now gives up at 45 s.

Retired or overloaded models return the provider's own error (404 "no longer available", 503 "high demand").
**🔍 Fetch models** lists what your key can use, and the Models page points you to it when a model name is
rejected.

## Validated live (2026-11-06): routing through Bifrost

Against the real Bifrost (pinned image):

1. A Gemini connection was registered at runtime. Bifrost's key check came back ("API key not valid", a
   dummy key) and was shown on the connection.
2. An agent's call went through Bifrost to Google: the error is Google's own.
3. After restarting Bifrost the provider was gone. The next call **re-registered it and retried**, and
   reached Google again.
4. Deleting the connection removed the key and the provider from Bifrost.
5. A Groq connection with a user key stayed direct, with the reason shown.

The success path (a chat through Bifrost on a key added at runtime) was verified with a Groq key during the
spike.

## Validated live (2026-11-05)

- A local **Ollama** entered as `http://localhost:11434/v1`: the URL was rewritten to `host.docker.internal`,
  all 6 models were discovered, `granite4:3b` answered "pong" in 8.5 s, and tool calling worked.
- An agent `custom/my-ollama/granite4:3b` with the calculator tool answered 17 × 23 + 4 = **395**.
- A live **Math & Reasoning** benchmark ran it next to `groq/openai/gpt-oss-20b` through Bifrost. Both scored
  100%; the local model had p50 7.6 s at $0 (price source `connection (no price set)`), against 1.0 s and
  $0.0005 for Groq.
