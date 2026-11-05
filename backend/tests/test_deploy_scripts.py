"""Deployment helpers: Space bundle contents, URL derivation, skip-without-token, DB URL handling."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    out = tmp_path_factory.mktemp("space")
    return _load("build_space").build(out)


def test_space_bundle_has_everything_and_nothing_extra(bundle: Path):
    files = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()}
    for required in (
        "Dockerfile",
        "start.sh",
        "README.md",
        "bifrost/config.json",
        "backend/requirements.txt",
        "backend/app/main.py",
        "backend/app/seed/default_agents.yaml",
        "frontend/app.py",
        "frontend/pages/1_Agents.py",
        "frontend/requirements.txt",
    ):
        assert required in files, required
    assert not any("tests/" in f or "__pycache__" in f or f.endswith(".env") for f in files)


def test_space_bundle_shell_and_config(bundle: Path):
    assert b"\r\n" not in (bundle / "start.sh").read_bytes(), "start.sh must have LF line endings"
    cfg = json.loads((bundle / "bifrost" / "config.json").read_text(encoding="utf-8"))
    assert set(cfg["providers"]) == {"groq", "openrouter"}  # no Ollama in the cloud
    assert all(k["value"].startswith("env.") for p in cfg["providers"].values() for k in p["keys"])
    card = (bundle / "README.md").read_text(encoding="utf-8")
    assert card.startswith("---\n") and "sdk: docker" in card and "app_port: 7860" in card


@pytest.mark.parametrize(
    ("space", "url"),
    [
        ("UTSAVS26/agentpulse", "https://utsavs26-agentpulse.hf.space"),
        ("Some_User/Agent.Pulse", "https://some-user-agent-pulse.hf.space"),
    ],
)
def test_space_url(space, url):
    assert _load("deploy_space").space_url(space) == url


def test_deploy_skips_cleanly_without_token(monkeypatch, capsys):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_SPACE", raising=False)
    assert _load("deploy_space").main() == 0
    assert "skipping deployment" in capsys.readouterr().out


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
