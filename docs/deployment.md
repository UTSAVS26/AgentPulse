# Deployment (Hugging Face Spaces, auto-deploy)

Every push to `main` that passes CI is deployed automatically to a public **Hugging Face Space**:

```
push to main ─▶ CI (lint · tests · Docker stack + UI tests · Space image smoke test)
                  │ all green
                  ▼
             Deploy workflow ─▶ build dist/space ─▶ sync secrets ─▶ upload ─▶ wait for RUNNING ─▶ check URL
```

## How the public demo behaves

| Visitor | Mode | What happens |
|---|---|---|
| Anyone | **Mock** (default) | The full product with simulated models and search. Costs nothing, can't drain your API credits. |
| Someone with the **access code** (sidebar → 🔒 *Unlock live mode*) | **Live** | Real models via Bifrost (Groq, OpenRouter) and live Tavily search, with your keys |

On the public deployment, **editing or deleting agents, deleting suites or runs, and cancelling runs also need the code**,
so visitors can't wipe the demo. Creating agents and starting mock runs is open to everyone.

Each benchmark run records the mode it ran in, and the run list shows `mock` or `live`.

## Architecture of the Space

Hugging Face runs one container, so [`deploy/space/`](../deploy/space/) packs all three services into it:

```
container (uid 1000)
├── bifrost      127.0.0.1:8080   static binary copied from the pinned official image; config: bifrost/config.space.json
├── uvicorn      127.0.0.1:8000   FastAPI backend (SQLite in /tmp, or Postgres via DATABASE_URL)
└── streamlit    0.0.0.0:7860     dashboard: the only public port
```

`start.sh` starts them in order, waits for each to be healthy, and exits if any of them dies so the platform
restarts the container. The cloud Bifrost config has **no Ollama provider**, so the local-Ollama agent returns a
clean "provider not configured" error in live mode.

## One-time setup (about 10 minutes)

1. **Hugging Face account and token**
   - Sign up at https://huggingface.co.
   - Go to **Settings → Access Tokens → Create new token**, type **Write**, and copy it.
2. **GitHub repository secrets**: repo → **Settings → Secrets and variables → Actions → New repository secret**:

   | Secret | Required | Value |
   |---|---|---|
   | `HF_TOKEN` | ✅ | The write token from step 1 |
   | `HF_SPACE` | ✅ | `<your-hf-username>/agentpulse` (the Space is created on first deploy) |
   | `LIVE_ACCESS_CODE` | for live mode | A long random code you share only with judges (e.g. from `python -c "import secrets;print(secrets.token_urlsafe(12))"`) |
   | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `TAVILY_API_KEY` | for live mode | Your keys. Synced to the Space as **secrets**, never uploaded as files. |
   | `DATABASE_URL` | optional | A Postgres URL (e.g. a free [Neon](https://neon.tech) database) so data survives Space restarts. `postgres://` and `postgresql://` URLs are accepted as-is. |

3. **Deploy:** push to `main`, or go to **Actions → Deploy (Hugging Face Space) → Run workflow**.
   The first build takes about 5–8 minutes. The workflow summary shows the URL:
   `https://<hf-username>-agentpulse.hf.space` (also at `https://huggingface.co/spaces/<hf-username>/agentpulse`).

Without `HF_TOKEN`/`HF_SPACE`, the deploy workflow logs a notice and does nothing, so forks don't fail.

## Test the Space image locally (before pushing)

```bash
make space-smoke     # build dist/space, run it like Hugging Face (port 7860, uid 1000), 12 smoke checks
                     # leaves it running with the TEST code "smoke-test-code" and NO real keys
make space-live      # (re)start the image with your .env keys; unlock code "letmein"
make space-live SPACE_CODE=my-code   # ...or choose the code
make space-stop      # stop and remove it
```

Open http://localhost:7860, then use the sidebar's **🔒 Unlock live mode** with the code that the command printed.
Only one container can use port 7860, and each `space-*` target replaces the previous one. If the code is
rejected, check which target you ran last.

The CI job **"Hugging Face Space image"** runs `make space-smoke` on every push, so a broken image never reaches
the deploy step.

## Limits of the free Space

| Limit | Effect | Mitigation |
|---|---|---|
| Sleeps after ~48 h without visitors | First visit after sleeping takes about 1 min to wake | Open the URL a few minutes before demoing |
| Ephemeral disk | SQLite data (runs, custom agents) resets on restart or redeploy | Set `DATABASE_URL` to a free Postgres |
| 2 vCPU / 16 GB | Mock runs are fast; live runs are bound by provider rate limits, not the Space | — |
| No Ollama | The local-model agent errors in live mode | Use it locally |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Deploy job: `Space failed with stage BUILD_ERROR` | Open the Space → **Logs → Build**. Reproduce locally with `make space-smoke`. |
| Deploy job: `401` / `403` from Hugging Face | `HF_TOKEN` must be a **Write** token belonging to the owner of `HF_SPACE` |
| "Unlock live mode" says *Invalid access code* | On the Space: the `LIVE_ACCESS_CODE` secret isn't set, or differs from what you typed. Re-run the deploy after changing secrets. Locally: the container on port 7860 is from a different `make space-*` run (`space-smoke` uses `smoke-test-code`, `space-live` uses `letmein`). |
| Live runs fail with `no keys found that support model` | The provider key secret is missing. Add it and re-run the deploy, which re-syncs secrets and restarts the Space. |
