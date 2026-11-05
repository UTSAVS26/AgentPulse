"""Deploy image + Render Blueprint sanity checks (static; the image itself is smoke-tested in CI)."""

import json
import re
from pathlib import Path

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"


def test_every_dockerfile_copy_source_exists():
    sources = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"COPY\s+(?!--from)(?:--chown=\S+\s+)?(.+)\s+\S+$", line.strip())
        if m:
            sources += m.group(1).split()
    assert sources, "no COPY lines parsed"
    for src in sources:
        assert (ROOT / src).exists(), f"Dockerfile copies missing path: {src}"


def test_secrets_and_bulk_never_enter_the_image():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", ".git", ".venv", "backend/tests", "frontend/tests"):
        assert entry in ignore, entry
    copies = [
        ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if ln.strip().startswith("COPY")
    ]
    assert copies and not any(
        ".env" in ln or " . " in f"{ln} " for ln in copies
    ), "never COPY .env or the whole context"


def test_start_script_is_lf_and_uses_platform_port():
    raw = (ROOT / "deploy" / "start.sh").read_bytes()
    assert b"\r\n" not in raw
    assert b"${PORT:-7860}" in raw


def test_cloud_bifrost_config_has_no_local_providers_and_no_inline_secrets():
    cfg = json.loads((ROOT / "bifrost" / "config.cloud.json").read_text(encoding="utf-8"))
    assert set(cfg["providers"]) == {"groq", "openrouter"}
    assert all(k["value"].startswith("env.") for p in cfg["providers"].values() for k in p["keys"])


def test_render_blueprint():
    bp = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    (svc,) = bp["services"]
    assert svc["type"] == "web" and svc["runtime"] == "docker" and svc["plan"] == "free"
    assert svc["autoDeployTrigger"] == "checksPass"  # deploy only after GitHub CI passes
    assert svc["branch"] == "main"
    assert (ROOT / svc["dockerfilePath"]).resolve() == DOCKERFILE.resolve()
    assert svc["healthCheckPath"] == "/_stcore/health"
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["MOCK_MODE"]["value"] == "true"
    for secret in ("LIVE_ACCESS_CODE", "GROQ_API_KEY", "OPENROUTER_API_KEY", "TAVILY_API_KEY"):
        assert env[secret] == {"key": secret, "sync": False}, f"{secret} must be prompted, never committed"
    assert env["APP_SECRET"] == {"key": "APP_SECRET", "generateValue": True}  # encrypts stored model keys
    # An empty DATABASE_URL would override the image's SQLite default, so it must not be declared here.
    assert "DATABASE_URL" not in env


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:////tmp/x.db", "sqlite:////tmp/x.db"),
    ],
)
def test_database_url_normalised_for_hosted_postgres(raw, expected):
    assert Settings(database_url=raw).database_url == expected


def test_start_script_binds_port_first_and_bifrost_is_never_fatal():
    """Regression (Render free tier, 0.1 CPU): the dashboard must bind $PORT before anything slow
    starts, and a Bifrost crash must not stop the container (mock mode doesn't need it)."""
    script = (ROOT / "deploy" / "start.sh").read_text(encoding="utf-8")
    streamlit, uvicorn, bifrost = (
        script.index(s) for s in ("streamlit run", "uvicorn app.main", "bifrost -app-dir")
    )
    assert streamlit < uvicorn < bifrost
    wait_line = next(ln for ln in script.splitlines() if ln.startswith("wait -n"))
    assert "FRONTEND_PID" in wait_line and "BACKEND_PID" in wait_line and "BIFROST" not in wait_line
    assert "while true" in script  # supervised restart loop


def test_cloud_bifrost_uses_no_sqlite_stores():
    """Regression: Bifrost's SQLite stores hit 'database is locked' and crashed on 0.1 CPU."""
    cfg = json.loads((ROOT / "bifrost" / "config.cloud.json").read_text(encoding="utf-8"))
    assert cfg["config_store"] == {"enabled": False}
    assert cfg["logs_store"] == {"enabled": False}
