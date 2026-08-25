from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
try:
    SESSION_TTL_SECONDS = max(300, int(os.getenv("CADFLOW_CONFIG_SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS)))
except ValueError:
    SESSION_TTL_SECONDS = DEFAULT_SESSION_TTL_SECONDS
SESSION_COOKIE = "cadflow_config_session"
_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        salt = _decode(salt_text)
        expected = _decode(digest_text)
    except (AttributeError, ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    with _sessions_lock:
        expires_at = _sessions.get(token)
        if expires_at is None:
            return False
        if expires_at <= now:
            _sessions.pop(token, None)
            return False
        return True


def revoke_session(token: str) -> None:
    if token:
        with _sessions_lock:
            _sessions.pop(token, None)
