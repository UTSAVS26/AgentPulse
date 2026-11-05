"""Encryption of stored secrets (model-connection API keys) with Fernet (AES-128-CBC + HMAC).

The key is derived from ``APP_SECRET``. Without one, a random secret is generated on first use and kept in a
file (``APP_SECRET_FILE``, default: the system temp directory), with a warning: fine for a laptop or the
public demo (whose database is temporary anyway), but set ``APP_SECRET`` for anything long-lived, because keys
encrypted under one secret can't be decrypted under another.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import tempfile
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict[str, Fernet] = {}


class SecretError(Exception):
    """A stored secret can't be decrypted (usually: APP_SECRET changed)."""


def _secret_file() -> Path:
    configured = get_settings().app_secret_file
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "agentpulse" / "app_secret"


def _load_secret() -> str:
    configured = get_settings().app_secret
    if configured:
        return configured
    path = _secret_file()
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        pass
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    log.warning(
        "APP_SECRET is not set: generated one in %s. Set APP_SECRET to keep stored keys readable.", path
    )
    return value


def _fernet() -> Fernet:
    secret = _load_secret()
    with _lock:
        if secret not in _cache:
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
            _cache[secret] = Fernet(key)
        return _cache[secret]


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretError(
            "the stored API key can't be decrypted (APP_SECRET changed?): enter the key again"
        ) from exc


def mask(key: str) -> str:
    """A hint that identifies a key without revealing it: ``sk-…a1b2``."""
    key = key.strip()
    if len(key) <= 8:
        return "…" + key[-2:] if len(key) > 2 else "…"
    prefix = key[:3] if key[:3].isascii() else ""
    return f"{prefix}…{key[-4:]}"
