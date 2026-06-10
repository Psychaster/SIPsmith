"""Password hashing, JWT signing, and PAT generation."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jose import JWTError, jwt

from sipsmith.config import get_settings

_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False


def create_access_token(subject: str, expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(days=settings.security.token_expiry_days)
    expire = datetime.now(UTC) + expires_delta
    payload = {"sub": subject, "exp": expire, "iat": datetime.now(UTC)}
    return jwt.encode(payload, settings.security.secret_key, algorithm="HS256")


def decode_access_token(token: str) -> str | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.security.secret_key, algorithms=["HS256"])
        return payload.get("sub")
    except JWTError:
        return None


def generate_pat() -> tuple[str, str]:
    """Return (raw_token, sha256_hash). Store only the hash."""
    raw = secrets.token_urlsafe(40)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_pat(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def get_totp_uri(secret: str, username: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(username, issuer_name="SIPsmith")
