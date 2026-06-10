"""Auth subsystem tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from sipsmith.auth.service import (
    create_access_token,
    decode_access_token,
    generate_pat,
    generate_totp_secret,
    hash_password,
    hash_pat,
    verify_password,
    verify_totp,
)
from sipsmith.models.user import User

# ── Unit: password hashing ────────────────────────────────────────────────


def test_password_hash_and_verify():
    hashed = hash_password("hunter2")
    assert verify_password("hunter2", hashed)
    assert not verify_password("wrong", hashed)


def test_password_hashes_are_unique():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # argon2 uses random salt


# ── Unit: JWT tokens ──────────────────────────────────────────────────────


def test_token_roundtrip():
    token = create_access_token("alice")
    assert decode_access_token(token) == "alice"


def test_invalid_token_returns_none():
    assert decode_access_token("not.a.valid.jwt") is None


def test_tampered_token_returns_none():
    token = create_access_token("alice")
    # Flip a character in the signature
    tampered = token[:-3] + "xxx"
    assert decode_access_token(tampered) is None


# ── Unit: PAT ─────────────────────────────────────────────────────────────


def test_pat_hash_consistency():
    raw, hashed = generate_pat()
    assert hash_pat(raw) == hashed
    assert len(raw) > 20
    assert len(hashed) == 64  # SHA-256 hex


# ── Unit: TOTP ────────────────────────────────────────────────────────────


def test_totp_verify():
    import pyotp

    secret = generate_totp_secret()
    totp = pyotp.TOTP(secret)
    code = totp.now()
    assert verify_totp(secret, code)
    assert not verify_totp(secret, "000000")


# ── Integration: login endpoint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "testpass123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "nobody", "password": "anything"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_endpoint(client: AsyncClient, admin_user: User, admin_token: str):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_me_unauthenticated(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


# ── Integration: PAT creation and use ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_use_pat(client: AsyncClient, admin_user: User, admin_token: str):
    # Create a PAT
    resp = await client.post(
        "/api/v1/auth/tokens",
        json={"name": "test-token"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    raw_token = resp.json()["token"]
    token_id = resp.json()["id"]

    # Use the PAT to access /me
    resp2 = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["username"] == "admin"

    # Delete the PAT
    resp3 = await client.delete(
        f"/api/v1/auth/tokens/{token_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp3.status_code == 204

    # PAT no longer works
    resp4 = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert resp4.status_code == 401
