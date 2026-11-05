# Deployment (Render, auto-deploy)

AgentPulse deploys as **one free Docker web service on [Render](https://render.com)**, defined in
[`render.yaml`](../render.yaml). Every push to `main` is deployed automatically **after the GitHub CI checks pass**
(`autoDeployTrigger: checksPass`), so a red build never reaches the public URL.

```
push to main ─▶ GitHub CI (lint · tests · Docker stack + UI tests · deploy-image smoke test)
                  │ all checks green
                  ▼
             Render builds deploy/Dockerfile ─▶ health check /_stcore/health ─▶ switches traffic to the new version
```

## How the public demo behaves

| Visitor | Mode | What happens |
|---|---|---|
| Anyone | **Mock** (default) | The full product with simulated models and search. Costs nothing, can't drain your API credits. |
| Someone with the **access code** (sidebar → 🔒 *Unlock live mode*) | **Live** | Real models via Bifrost (Groq, OpenRouter) and live Tavily search, with your keys |

On the public deployment, editing or deleting agents, deleting suites or runs, and cancelling runs also need the
code, so visitors can't wipe the demo. Each benchmark run records the mode it ran in.

## What runs in the container

[`deploy/Dockerfile`](../deploy/Dockerfile) packs everything into one container (about 230–290 MB RAM, which fits
the free plan's 512 MB):

```
container (uid 1000)
├── bifrost      127.0.0.1:8080   static binary from the pinned official image; config: bifrost/config.cloud.json
├── uvicorn      127.0.0.1:8000   FastAPI backend (SQLite in /tmp, or Postgres via DATABASE_URL)
└── streamlit    0.0.0.0:$PORT    dashboard: the only public port (Render sets PORT)
```

[`deploy/start.sh`](../deploy/start.sh) is tuned for the free plan's **0.1 CPU**:

1. **Dashboard first.** Streamlit binds `$PORT` within seconds, so Render's port scan succeeds. Until the backend
   is ready, the page shows "Cannot reach the backend".
2. **Backend next.** If the dashboard or the backend dies, the container exits and Render restarts it.
3. **Bifrost last, and never fatal.** It runs in a restart loop, because only live mode needs it. It isn't started
   at all if no provider keys are set. Its SQLite stores are disabled in `bifrost/config.cloud.json` (all config
   comes from the file), which avoids `database is locked` crashes on a slow CPU.

Measured with Docker limited to 0.1 CPU / 512 MB: Bifrost ready in ~40 s, dashboard and backend in ~70 s, ~250 MB RAM. The cloud Bifrost config has **no Ollama provider**, so the local-model agent returns a
clean "provider not configured" error in live mode.

## One-time setup (about 10 minutes)

1. **Sign up** at https://dashboard.render.com with **GitHub**.
2. Go to **New + → Blueprint**, connect the `UTSAVS26/AgentPulse` repository (grant Render access if asked), and pick branch `main`.
   Render reads `render.yaml` and shows one service: **agentpulse** (free, Docker).
3. Render **prompts for the secret values** (they are `sync: false`, so they never live in git):

   | Key | Value |
   |---|---|
   | `LIVE_ACCESS_CODE` | A code you choose and share only with judges, e.g. `python -c "import secrets;print(secrets.token_urlsafe(12))"`. Leave empty for a mock-only demo. |
   | `GROQ_API_KEY` | From your `.env` |
   | `OPENROUTER_API_KEY` | From your `.env` |
   | `TAVILY_API_KEY` | From your `.env` |

4. Click **Apply**. The first build takes about 5–10 minutes. Your URL is shown on the service page,
   e.g. **`https://agentpulse.onrender.com`** (Render adds a suffix if that name is taken).

That's all. Nothing needs to be configured in GitHub for deploys.

### Optional: persistent data

The free disk is ephemeral, so SQLite data (runs, custom agents) resets on every deploy or restart. To keep it,
add **`DATABASE_URL`** in Render → service → **Environment**, pointing to a free Postgres (e.g. [Neon](https://neon.tech)).
`postgres://` and `postgresql://` URLs work as-is.

## Checking a deploy

- **Render dashboard → agentpulse → Events:** each deploy with its commit, status (*Build in progress → Deploy live*), and failures.
- **Logs** tab: the container output. A healthy start shows:
  ```
  [start] Bifrost is up
  [start] Backend is up
  [start] public demo: mock by default, live mode unlocked by access code
  ```
- **GitHub:** the commit's checks must be green first. Render waits for them before deploying.
- **The app:** open the URL. The sidebar shows **MOCK MODE**, and *Unlock live mode* with your code switches it to **LIVE MODE**.

## Changing secrets

Render → agentpulse → **Environment** → edit the value → **Save changes**. Render redeploys with the new value.
For example, change `LIVE_ACCESS_CODE` to revoke an old code.

## Test the deploy image locally (before pushing)

```bash
make image-smoke     # build deploy/Dockerfile, run it like the cloud does, 13 smoke checks
                     # leaves it running on :7860 with the TEST code "smoke-test-code" and NO real keys
make image-live      # (re)start it with your .env keys; unlock code "letmein"
make image-live LIVE_CODE=my-code
make image-stop      # stop and remove it
```

Only one container can use port 7860, and each `image-*` target replaces the previous one. The CI job
**"Deploy image (build + smoke test)"** runs `make image-smoke` on every push.

## Free plan limits

| Limit | Effect | Mitigation |
|---|---|---|
| Spins down after ~15 min without traffic | The next visit takes about 1 minute to wake up | Open the URL a few minutes before demoing |
| 0.1 CPU, 512 MB RAM | Page loads are slower than locally. Benchmarks are mostly network-bound, so they're fine. | — |
| Ephemeral disk | Data resets on deploy or restart | Set `DATABASE_URL` |
| No Ollama | The local-model agent errors in live mode | Use it locally |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Render never deploys after a push | The GitHub checks on that commit must all pass. If Render still waits, set **Settings → Auto-Deploy → On Commit**. |
| Build fails | Render → **Events** → the failed deploy → build log. Reproduce locally with `make image-smoke`. |
| Deploy fails the health check / "No open ports detected" | **Logs** tab: `[start] dashboard starting on port …` should appear within seconds. Reproduce Render's limits locally: `docker run --cpus=0.1 --memory=512m -e PORT=10000 -p 10000:10000 agentpulse-app` |
| Logs show Bifrost `error` lines: `governance plugin … config store`, `auth middleware requires config store`, `logging plugin not found` | Expected and harmless. The cloud config disables Bifrost's database (to avoid lock crashes on 0.1 CPU), and these optional features need it. AgentPulse uses none of them, keeps auth off, and records its own traces. Don't add an empty `governance` section: that makes Bifrost refuse to start. |
| Logs show `Bifrost exited … live mode unavailable` | Mock mode keeps working. Bifrost restarts automatically. If it keeps failing, check the provider keys. |
| *Invalid access code* when unlocking | `LIVE_ACCESS_CODE` isn't set or differs on Render. Locally, the container on :7860 is from a different `make image-*` run (`image-smoke` uses `smoke-test-code`, `image-live` uses `letmein`). |
| Live runs fail with `no keys found that support model` | The provider key is missing in Render → Environment |
