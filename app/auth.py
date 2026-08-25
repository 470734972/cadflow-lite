from __future__ import annotations

import base64
import binascii
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
_revoked_sessions: set[str] = set()
_sessions_lock = threading.Lock()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_key() -> bytes:
    """Derive a stable signing key without storing the clear-text password."""
    configured = os.getenv("CADFLOW_CONFIG_SESSION_SECRET", "")
    password_hash = os.getenv("CADFLOW_CONFIG_PASSWORD_HASH", "")
    material = configured or password_hash
    return hashlib.sha256(("cadflow-config-session:" + material).encode("utf-8")).digest()


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
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{expires_at}.{secrets.token_urlsafe(24)}".encode("utf-8")
    signature = hmac.new(_session_key(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def valid_session(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        if token in _revoked_sessions:
            return False
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = _decode(payload_text)
        signature = _decode(signature_text)
        expires_text, nonce = payload.decode("utf-8").split(".", 1)
        expires_at = int(expires_text)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        return False
    if not nonce or expires_at <= int(time.time()):
        return False
    expected = hmac.new(_session_key(), payload, hashlib.sha256).digest()
    return hmac.compare_digest(signature, expected)


def revoke_session(token: str) -> None:
    if token:
        with _sessions_lock:
            _revoked_sessions.add(token)
