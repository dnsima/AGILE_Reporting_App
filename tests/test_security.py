"""Password hashing, JWT issuing and API-key helpers."""

from __future__ import annotations

import pytest

from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)


def test_password_round_trip():
    hashed = hash_password("CorrectHorse1!")
    assert hashed != "CorrectHorse1!"
    assert verify_password("CorrectHorse1!", hashed)
    assert not verify_password("wrong", hashed)


def test_password_longer_than_bcrypt_limit_is_supported():
    """bcrypt truncates at 72 bytes; long passwords are pre-hashed, not cut."""
    long_password = "a" * 100 + "Z9!"
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed)
    assert not verify_password("a" * 100 + "Z9?", hashed)


def test_token_round_trip_carries_claims():
    token = create_access_token("42", {"role": "NPCU", "state_id": 7})
    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["role"] == "NPCU"
    assert payload["state_id"] == 7


def test_expired_token_is_rejected():
    token = create_access_token("1", ttl_minutes=-1)
    with pytest.raises(AuthenticationError, match="expired"):
        decode_access_token(token)


def test_tampered_token_is_rejected():
    token = create_access_token("1", {"role": "VIEWER"})
    header, body, signature = token.split(".")
    forged = f"{header}.{body}.{'a' * len(signature)}"
    with pytest.raises(AuthenticationError):
        decode_access_token(forged)


def test_malformed_token_is_rejected():
    with pytest.raises(AuthenticationError, match="Malformed"):
        decode_access_token("not-a-jwt")


def test_api_key_is_hashed_not_stored():
    full_key, prefix, key_hash = generate_api_key()
    assert full_key.startswith("agile_")
    assert prefix in full_key
    assert key_hash == hash_api_key(full_key)
    assert key_hash != full_key
