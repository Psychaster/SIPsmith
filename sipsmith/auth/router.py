"""Auth API routes: login, logout, TOTP, token management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.auth.service import (
    create_access_token,
    generate_pat,
    generate_totp_secret,
    get_totp_uri,
    verify_password,
    verify_totp,
)
from sipsmith.config import get_settings
from sipsmith.database import get_db
from sipsmith.models.user import PersonalAccessToken, Role, User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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
    return TokenResponse(access_token=token)


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
