from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from mileaf.config import Settings

password_hash = PasswordHash.recommended()

PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class InvalidTokenError(Exception):
    pass


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return password_hash.verify(password, hashed)


def _encode_token(
    subject: str, token_type: str, expires_delta: timedelta, settings: Settings
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str, settings: Settings) -> str:
    return _encode_token(
        subject, "access", timedelta(minutes=settings.access_token_minutes), settings
    )


def create_refresh_token(subject: str, settings: Settings) -> str:
    return _encode_token(subject, "refresh", timedelta(days=settings.refresh_token_days), settings)


def decode_token(token: str, expected_type: str, settings: Settings) -> str:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("invalid or expired token") from exc

    if payload.get("type") != expected_type:
        raise InvalidTokenError("wrong token type")

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidTokenError("missing subject")
    return subject


def generate_agent_token() -> str:
    return f"mlf_agent_{secrets.token_urlsafe(32)}"


def hash_agent_token(token: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), token.encode(), hashlib.sha256).hexdigest()


def generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(10))


def hash_pairing_code(code: str, pepper: str) -> str:
    normalized = code.strip().upper().replace("-", "")
    return hashlib.sha256(f"{pepper}:{normalized}".encode()).hexdigest()
