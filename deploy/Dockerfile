# AgentPulse - single-container image for cloud hosting (Render free web service).
#
# Runs three processes (see deploy/start.sh): Bifrost gateway (127.0.0.1:8080), FastAPI backend
# (127.0.0.1:8000) and the Streamlit dashboard on $PORT (the only public port).
# Build context: the repository root (see render.yaml and `make image-build`).

# Bifrost ships a statically linked binary; reuse it from the official image (pinned by digest).
FROM maximhq/bifrost@sha256:a1c90607dcfd13c1fb2f9476ceb3bb24407a50ade7fdf3e61a59fb42b09e5c57 AS bifrost

FROM python:3.12-slim

# WEB_SEARCH_VIA=rest: Bifrost runs without a config store here (SQLite locks on tiny instances) and its MCP
# gateway needs one, so web search goes straight to Tavily's REST API. Outage drills still work.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/user \
    PORT=7860 \
    MOCK_MODE=true \
    MOCK_LATENCY_SCALE=0.25 \
    DATABASE_URL=sqlite:////tmp/agentpulse/agentpulse.db \
    BIFROST_URL=http://127.0.0.1:8080 \
    BACKEND_URL=http://127.0.0.1:8000 \
    LLM_TIMEOUT_S=120 \
    SEARCH_TIMEOUT_S=20 \
    WEB_SEARCH_VIA=rest

COPY --from=bifrost /app/main /usr/local/bin/bifrost

# Run as an unprivileged user.
RUN useradd -m -u 1000 user
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
COPY frontend/requirements.txt frontend/requirements.txt
RUN pip install -r backend/requirements.txt -r frontend/requirements.txt

# Only what the app needs: no tests, no .env (also excluded by .dockerignore).
COPY --chown=user backend/app backend/app
COPY --chown=user frontend/Home.py frontend/api.py frontend/ui.py frontend/charts.py frontend/suite_edit.py frontend/
COPY --chown=user frontend/pages frontend/pages
COPY --chown=user frontend/.streamlit frontend/.streamlit
COPY --chown=user bifrost/config.cloud.json bifrost/config.json
COPY --chown=user deploy/start.sh start.sh
RUN sed -i 's/\r$//' start.sh  # tolerate CRLF checkouts on Windows

USER user
EXPOSE 7860
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/_stcore/health', timeout=3).status == 200 else 1)"

CMD ["bash", "start.sh"]
