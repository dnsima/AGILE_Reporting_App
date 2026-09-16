"""Password hashing, JWT issuing/verification and API-key helpers.

JWTs are HS256 tokens built on the standard library (``hmac``/``hashlib``) to
keep the dependency surface small; bcrypt is used directly for password
hashing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from app.core.config import settings
from app.core.errors import AuthenticationError

ALGORITHM = "HS256"
BCRYPT_MAX_BYTES = 72


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def _password_bytes(password: str) -> bytes:
    """bcrypt silently ignores bytes past 72; pre-hash so long passwords work."""
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raw = base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_password_bytes(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(message: bytes) -> bytes:
    return hmac.new(settings.secret_key.encode("utf-8"), message, hashlib.sha256).digest()


def create_access_token(
    subject: str,
    claims: dict[str, Any] | None = None,
    ttl_minutes: int | None = None,
) -> str:
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_minutes
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    payload.update(claims or {})

    header = _b64encode(json.dumps({"alg": ALGORITHM, "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64encode(json.dumps(payload, separators=(",", ":"), default=str).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    signature = _b64encode(_sign(signing_input))
    return f"{header}.{body}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        header_b64, body_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise AuthenticationError("Malformed authentication token") from exc

    expected = _sign(f"{header_b64}.{body_b64}".encode("ascii"))
    try:
        provided = _b64decode(signature_b64)
    except Exception as exc:  # noqa: BLE001 - any decode failure is a bad token
        raise AuthenticationError("Malformed authentication token") from exc

    if not hmac.compare_digest(expected, provided):
        raise AuthenticationError("Invalid authentication token")

    try:
        payload = json.loads(_b64decode(body_b64))
    except Exception as exc:  # noqa: BLE001
        raise AuthenticationError("Malformed authentication token") from exc

    exp = payload.get("exp")
    if exp is None or datetime.now(timezone.utc).timestamp() > float(exp):
        raise AuthenticationError("Authentication token has expired")
    return payload


# --------------------------------------------------------------------------
# API keys (for external dashboard integrations)
# --------------------------------------------------------------------------
API_KEY_PREFIX = "agile"


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(full_key, prefix, sha256_hash)``.

    The full key is shown to the caller exactly once; only the hash is stored.
    """
    raw = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    full_key = f"{API_KEY_PREFIX}_{prefix}_{raw}"
    return full_key, prefix, hash_api_key(full_key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
