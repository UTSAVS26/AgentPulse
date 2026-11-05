#!/usr/bin/env bash
# Start Bifrost, the FastAPI backend and the Streamlit dashboard in one container.
# If any process exits, the container exits so the platform restarts it.
set -uo pipefail

log() { echo "[start] $*"; }

wait_for() {  # wait_for <name> <url> <seconds>
  local name=$1 url=$2 timeout=$3
  for _ in $(seq 1 "$timeout"); do
    if python -c "import urllib.request,sys; urllib.request.urlopen('$url', timeout=2)" 2>/dev/null; then
      log "$name is up"; return 0
    fi
    sleep 1
  done
  log "WARNING: $name not healthy after ${timeout}s"; return 1
}

mkdir -p /tmp/agentpulse /tmp/bifrost
cp /app/bifrost/config.json /tmp/bifrost/config.json

# 1) Bifrost gateway (internal only). Mock mode works even if it fails to start.
bifrost -app-dir /tmp/bifrost -port 8080 -host 127.0.0.1 -log-level warn -log-style json &
BIFROST_PID=$!
wait_for "Bifrost" "http://127.0.0.1:8080/health" 60 || true

# 2) Backend API (internal only).
(cd /app/backend && exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info) &
BACKEND_PID=$!
wait_for "Backend" "http://127.0.0.1:8000/health" 90 || true

# 3) Dashboard: the public port (7860 on Hugging Face Spaces).
(cd /app/frontend && exec streamlit run app.py \
    --server.port="${PORT:-7860}" --server.address=0.0.0.0 --server.headless=true \
    --server.enableCORS=false --server.enableXsrfProtection=false \
    --browser.gatherUsageStats=false) &
FRONTEND_PID=$!

if [ -n "${LIVE_ACCESS_CODE:-}" ]; then
  log "public demo: mock by default, live mode unlocked by access code"
else
  log "LIVE_ACCESS_CODE not set: mock mode only"
fi

wait -n "$BIFROST_PID" "$BACKEND_PID" "$FRONTEND_PID"
STATUS=$?
log "a process exited (status $STATUS); stopping container"
kill "$BIFROST_PID" "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
exit "$STATUS"
