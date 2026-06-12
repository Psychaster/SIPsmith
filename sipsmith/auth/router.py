"""Auth API routes: login, logout, TOTP, token management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.auth.service import (
    create_access_token,
    generate_pat,
    generate_totp_secret,
    get_totp_uri,
    hash_password,
    verify_password,
    verify_totp,
)
from sipsmith.config import get_settings
from sipsmith.database import get_db
from sipsmith.models.user import PersonalAccessToken, Role, User

BOOTSTRAP_TOKEN_PATH = Path("/etc/sipsmith/.bootstrap_token")

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: Role
    totp_enabled: bool

    model_config = {"from_attributes": True}


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.username == body.username, User.is_active))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if user.totp_enabled:
        if not body.totp_code or not verify_totp(user.totp_secret, body.totp_code):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="TOTP required")

    user.last_login = datetime.now(UTC)
    await db.commit()

    settings = get_settings()
    token = create_access_token(
        user.username,
        timedelta(seconds=settings.security.session_max_age),
    )
    response.set_cookie(
        key="sipsmith_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.security.session_max_age,
    )
    return TokenResponse(
        access_token=token,
        must_change_password=getattr(user, "must_change_password", False),
    )


# ── First-run setup + forced password change ─────────────────────────────


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: str
    password: str = Field(min_length=8)
    token: str | None = None  # bootstrap token (optional; verified if file exists)


@router.post("/setup", response_model=TokenResponse, status_code=201)
async def setup_initial_admin(
    body: SetupRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create the first admin user. Only callable when the users table is empty.

    Wired to the first-run card rendered on /login when zero users exist.
    Consumes (and invalidates) the bootstrap token at
    /etc/sipsmith/.bootstrap_token if present."""
    # Refuse if any user already exists.
    user_count = await db.scalar(select(func.count(User.id))) or 0
    if user_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Setup already complete"
        )

    # If a bootstrap token file exists, the caller must supply a matching token.
    # This blocks a same-network attacker from racing to the setup endpoint after
    # an install. Operators without filesystem access on the box can run the
    # installer (which prints the token) and paste it into the setup card.
    if BOOTSTRAP_TOKEN_PATH.exists():
        try:
            expected = BOOTSTRAP_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            expected = ""
        if not body.token or body.token.strip() != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bootstrap token required (see installer output)",
            )

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=Role.admin,
        is_active=True,
        must_change_password=True,  # force a password change on first login
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Invalidate the bootstrap token so this endpoint can never run again.
    if BOOTSTRAP_TOKEN_PATH.exists():
        try:
            BOOTSTRAP_TOKEN_PATH.unlink()
        except OSError:
            pass

    # Auto-login so the user lands straight on /account/change-password.
    settings = get_settings()
    token = create_access_token(
        user.username, timedelta(seconds=settings.security.session_max_age)
    )
    response.set_cookie(
        key="sipsmith_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.security.session_max_age,
    )
    return TokenResponse(access_token=token, must_change_password=True)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Verify current password, set the new one, and clear must_change_password.

    Backs the form on /account/change-password. The session cookie is preserved,
    so on success the page redirects straight to /dashboard."""
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from current",
        )
    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    await db.commit()
    return {"detail": "password updated"}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("sipsmith_session")
    return {"detail": "logged out"}


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(get_current_user)]):
    return user


@router.post("/totp/enable")
async def enable_totp(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if user.totp_enabled:
        raise HTTPException(status_code=400, detail="TOTP already enabled")
    secret = generate_totp_secret()
    user.totp_secret = secret
    await db.commit()
    return {"uri": get_totp_uri(secret, user.username), "secret": secret}


@router.post("/totp/confirm")
async def confirm_totp(
    code: Annotated[str, Form()],
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if user.totp_secret is None:
        raise HTTPException(status_code=400, detail="Call /totp/enable first")
    if not verify_totp(user.totp_secret, code):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")
    user.totp_enabled = True
    await db.commit()
    return {"detail": "TOTP enabled"}


@router.post("/totp/disable")
async def disable_totp(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user.totp_secret = None
    user.totp_enabled = False
    await db.commit()
    return {"detail": "TOTP disabled"}


class CreateTokenRequest(BaseModel):
    name: str
    expires_days: int | None = None


class TokenCreatedResponse(BaseModel):
    id: int
    name: str
    token: str  # only returned once


@router.post("/tokens", response_model=TokenCreatedResponse)
async def create_token(
    body: CreateTokenRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw, hashed = generate_pat()
    expires_at = None
    if body.expires_days:
        expires_at = datetime.now(UTC) + timedelta(days=body.expires_days)
    pat = PersonalAccessToken(
        user_id=user.id,
        name=body.name,
        token_hash=hashed,
        expires_at=expires_at,
    )
    db.add(pat)
    await db.commit()
    await db.refresh(pat)
    return TokenCreatedResponse(id=pat.id, name=pat.name, token=raw)


@router.delete("/tokens/{token_id}", status_code=204)
async def delete_token(
    token_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(PersonalAccessToken).where(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user.id,
        )
    )
    pat = result.scalar_one_or_none()
    if pat is None:
        raise HTTPException(status_code=404, detail="Token not found")
    await db.delete(pat)
    await db.commit()
