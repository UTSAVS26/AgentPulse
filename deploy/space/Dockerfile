# AgentPulse - single-container image for Hugging Face Spaces (Docker SDK).
#
# Runs three processes (see start.sh): Bifrost gateway (127.0.0.1:8080), FastAPI backend
# (127.0.0.1:8000) and the Streamlit dashboard (0.0.0.0:7860, the only public port).
# Built from the directory assembled by scripts/build_space.py, not from the repo root.

# Bifrost ships a statically linked binary; reuse it from the official image (pinned by digest).
FROM maximhq/bifrost@sha256:a1c90607dcfd13c1fb2f9476ceb3bb24407a50ade7fdf3e61a59fb42b09e5c57 AS bifrost

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/user \
    MOCK_MODE=true \
    MOCK_LATENCY_SCALE=0.25 \
    DATABASE_URL=sqlite:////tmp/agentpulse/agentpulse.db \
    BIFROST_URL=http://127.0.0.1:8080 \
    BACKEND_URL=http://127.0.0.1:8000 \
    LLM_TIMEOUT_S=120 \
    SEARCH_TIMEOUT_S=20

COPY --from=bifrost /app/main /usr/local/bin/bifrost

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
COPY frontend/requirements.txt frontend/requirements.txt
RUN pip install -r backend/requirements.txt -r frontend/requirements.txt

COPY --chown=user . .
USER user

EXPOSE 7860
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/_stcore/health', timeout=3).status == 200 else 1)"

CMD ["bash", "start.sh"]
