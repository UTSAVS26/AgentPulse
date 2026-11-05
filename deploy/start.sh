#!/usr/bin/env bash
# Start the dashboard, the backend and the Bifrost gateway in one container.
#
# Order matters on small cloud instances (Render free = 0.1 CPU):
#   1. Streamlit binds $PORT immediately, so the platform's port scan succeeds even while the
#      rest is still starting (the dashboard shows "Cannot reach the backend" until it's up).
#   2. The backend (FastAPI) is required; if it or the dashboard dies, the container exits
#      so the platform restarts it.
#   3. Bifrost is only needed for LIVE mode. It runs in a supervised restart loop and can never
#      take the container down; it isn't started at all when no provider keys are configured.
set -uo pipefail

log() { echo "[start] $*"; }

mkdir -p /tmp/agentpulse /tmp/bifrost
cp /app/bifrost/config.json /tmp/bifrost/config.json

# 1) Dashboard: the public port ($PORT, set by Render; 7860 by default).
(cd /app/frontend && exec streamlit run app.py \
    --server.port="${PORT:-7860}" --server.address=0.0.0.0 --server.headless=true \
    --server.enableCORS=false --server.enableXsrfProtection=false \
    --browser.gatherUsageStats=false) &
FRONTEND_PID=$!
log "dashboard starting on port ${PORT:-7860}"

# 2) Backend API (internal only).
(cd /app/backend && exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning) &
BACKEND_PID=$!

# 3) Bifrost gateway (internal only), supervised and non-fatal.
supervise_bifrost() {
  local attempt=0
  while true; do
    attempt=$((attempt + 1))
    log "starting Bifrost (attempt $attempt)"
    bifrost -app-dir /tmp/bifrost -port 8080 -host 127.0.0.1 -log-level warn -log-style json
    log "Bifrost exited (status $?); live mode unavailable until it restarts"
    sleep $((attempt < 6 ? attempt * 5 : 30))
  done
}
if [ -n "${GROQ_API_KEY:-}${OPENROUTER_API_KEY:-}" ]; then
  supervise_bifrost &
else
  log "no provider API keys set: Bifrost not started (mock mode only)"
fi

if [ -n "${LIVE_ACCESS_CODE:-}" ]; then
  log "public demo: mock by default, live mode unlocked by access code"
else
  log "LIVE_ACCESS_CODE not set: mock mode only"
fi

# Report readiness in the logs (informational only).
(
  for _ in $(seq 1 300); do
    if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
      log "Backend is up"; break
    fi
    sleep 2
  done
) &

wait -n "$FRONTEND_PID" "$BACKEND_PID"
STATUS=$?
log "a required process exited (status $STATUS); stopping container"
kill "$FRONTEND_PID" "$BACKEND_PID" 2>/dev/null || true
exit "$STATUS"
