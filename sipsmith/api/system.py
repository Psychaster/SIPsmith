"""System-level API: health, chrony, users, plugins, audit log."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import require_role
from sipsmith.auth.service import hash_password
from sipsmith.core.chrony import chronyc_tracking, time_sanity_ok
from sipsmith.database import get_db
from sipsmith.models.audit import AuditLog
from sipsmith.models.user import Role, User

router = APIRouter(prefix="/system", tags=["system"])


# ── Health ────────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str
    version: str
    fqdn: str
    time_ok: bool
    time_tracking: dict[str, str]


@router.get("/health", response_model=HealthResponse)
async def health():
    from sipsmith import __version__
    from sipsmith.config import get_settings

    settings = get_settings()
    tracking = chronyc_tracking()
    return HealthResponse(
        status="ok",
        version=__version__,
        fqdn=settings.server.fqdn,
        time_ok=time_sanity_ok(),
        time_tracking=tracking,
    )


# ── Chrony config ─────────────────────────────────────────────────────────


class ChronyConfigRequest(BaseModel):
    stratum: int
    allow_networks: list[str]


@router.get("/chrony")
async def get_chrony_config(
    _user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
):
    from sipsmith.config import get_settings

    s = get_settings()
    return {"stratum": s.chrony.stratum, "allow_networks": s.chrony.allow_networks}


@router.put("/chrony")
async def put_chrony_config(
    body: ChronyConfigRequest,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from sipsmith.agent.client import AgentClient
    from sipsmith.core.audit import AuditWriter
    from sipsmith.core.chrony import render_chrony_conf

    conf = render_chrony_conf(body.stratum, body.allow_networks)
    agent = AgentClient()
    await agent.write_config("/etc/chrony/chrony.conf", conf)
    await agent.systemctl("restart", "chrony")

    audit = AuditWriter(db)
    await audit.write(
        actor=user.username,
        action="system.chrony.update",
        new_value={"stratum": body.stratum, "allow_networks": body.allow_networks},
    )
    return {"detail": "chrony config applied"}


# ── Users ─────────────────────────────────────────────────────────────────


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    role: Role
    is_active: bool
    totp_enabled: bool

    model_config = {"from_attributes": True}


class CreateUserRequest(BaseModel):
    username: str
    email: str
    password: str
    role: Role = Role.readonly


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: CreateUserRequest,
    actor: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    actor: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    await db.delete(user)
    await db.commit()


# ── Audit log ─────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    id: int
    timestamp: datetime
    actor: str
    action: str
    resource: str | None
    old_value: str | None
    new_value: str | None
    ip_address: str | None
    result: str

    model_config = {"from_attributes": True}


@router.get("/audit", response_model=list[AuditEntry])
async def get_audit_log(
    _user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
):
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).offset(offset)
    )
    return result.scalars().all()
