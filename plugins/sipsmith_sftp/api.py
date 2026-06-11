"""SFTP plugin REST API — mounted at /api/v1/plugins/sftp."""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.config import get_settings
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_sftp.files import delete_file, dir_usage_bytes, list_files, run_retention, safe_path
from sipsmith_sftp.models import AuthType, RetentionPolicy, SftpAccount, SftpPreset
from sipsmith_sftp.presets import PRESETS

log = logging.getLogger("sipsmith.sftp.api")

router = APIRouter(prefix="/plugins/sftp", tags=["sftp"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class AccountOut(BaseModel):
    id: int
    username: str
    display_name: str
    auth_type: AuthType
    preset: SftpPreset | None
    quota_mb: int | None
    enabled: bool

    model_config = {"from_attributes": True}


class CreateAccountRequest(BaseModel):
    username: str
    display_name: str
    auth_type: AuthType = AuthType.password
    password: str | None = None
    ssh_key: str | None = None
    preset: SftpPreset | None = None
    quota_mb: int | None = None


class PasswordChangeRequest(BaseModel):
    password: str


class KeySetRequest(BaseModel):
    keys_content: str


class FileDeleteRequest(BaseModel):
    path: str


class RetentionOut(BaseModel):
    keep_count: int | None
    keep_days: int | None
    enabled: bool

    model_config = {"from_attributes": True}


class RetentionRequest(BaseModel):
    keep_count: int | None = None
    keep_days: int | None = None
    enabled: bool = True


class ApplyPresetRequest(BaseModel):
    username: str
    password: str | None = None
    ssh_key: str | None = None
    auth_type: AuthType = AuthType.password


# ── Helpers ───────────────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=422,
            detail=(
                "Username must start with a lowercase letter and contain only "
                "a-z, 0-9, _, - (max 32 chars)"
            ),
        )


def _validate_auth(auth_type: AuthType, password: str | None, ssh_key: str | None) -> None:
    if auth_type in (AuthType.password, AuthType.both) and not password:
        raise HTTPException(status_code=422, detail="Password required for this auth type")
    if auth_type in (AuthType.key, AuthType.both) and not ssh_key:
        raise HTTPException(status_code=422, detail="SSH key required for this auth type")


async def _get_account(account_id: int, db: AsyncSession) -> SftpAccount:
    result = await db.execute(select(SftpAccount).where(SftpAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


# ── Account CRUD ──────────────────────────────────────────────────────────────


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[SftpAccount]:
    result = await db.execute(select(SftpAccount).order_by(SftpAccount.id))
    return list(result.scalars().all())


@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(
    body: CreateAccountRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SftpAccount:
    from sipsmith.agent.client import AgentClient

    _validate_username(body.username)
    _validate_auth(body.auth_type, body.password, body.ssh_key)

    existing = await db.execute(select(SftpAccount).where(SftpAccount.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    agent = AgentClient()
    try:
        await agent.sftp_add_account(body.username, "", body.auth_type.value)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to create system account: {exc}"
        ) from exc

    if body.password and body.auth_type in (AuthType.password, AuthType.both):
        await agent.sftp_set_password(body.username, body.password)

    if body.ssh_key and body.auth_type in (AuthType.key, AuthType.both):
        await agent.sftp_set_authorized_keys(body.username, body.ssh_key)

    account = SftpAccount(
        username=body.username,
        display_name=body.display_name,
        auth_type=body.auth_type,
        preset=body.preset,
        quota_mb=body.quota_mb,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    await AuditWriter(db).write(
        actor=user.username,
        action="sftp.account.create",
        resource=body.username,
        new_value={"auth_type": body.auth_type.value, "preset": str(body.preset)},
    )
    return account


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(
    account_id: int,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    account = await _get_account(account_id, db)
    agent = AgentClient()
    try:
        await agent.sftp_delete_account(account.username)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to remove system account: {exc}"
        ) from exc

    await db.delete(account)
    await db.commit()

    await AuditWriter(db).write(
        actor=user.username, action="sftp.account.delete", resource=account.username
    )


# ── Password / key management ─────────────────────────────────────────────────


@router.post("/accounts/{account_id}/password", status_code=204)
async def set_password(
    account_id: int,
    body: PasswordChangeRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    account = await _get_account(account_id, db)
    if account.auth_type == AuthType.key:
        raise HTTPException(status_code=400, detail="Account uses key-only auth")
    if not body.password:
        raise HTTPException(status_code=422, detail="Password must not be empty")

    agent = AgentClient()
    await agent.sftp_set_password(account.username, body.password)

    await AuditWriter(db).write(
        actor=user.username, action="sftp.account.set_password", resource=account.username
    )


@router.post("/accounts/{account_id}/key", status_code=204)
async def set_ssh_key(
    account_id: int,
    body: KeySetRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    account = await _get_account(account_id, db)
    if account.auth_type == AuthType.password:
        raise HTTPException(status_code=400, detail="Account uses password-only auth")

    agent = AgentClient()
    await agent.sftp_set_authorized_keys(account.username, body.keys_content)

    await AuditWriter(db).write(
        actor=user.username, action="sftp.account.set_key", resource=account.username
    )


# ── File browser ──────────────────────────────────────────────────────────────


@router.get("/accounts/{account_id}/files")
async def browse_files(
    account_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(default=""),
) -> list[dict]:
    account = await _get_account(account_id, db)
    try:
        return list_files(account.username, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/accounts/{account_id}/files", status_code=204)
async def remove_file(
    account_id: int,
    body: FileDeleteRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    account = await _get_account(account_id, db)
    try:
        delete_file(account.username, body.path)
    except (ValueError, IsADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await AuditWriter(db).write(
        actor=user.username,
        action="sftp.file.delete",
        resource=f"{account.username}/{body.path}",
    )


@router.get("/accounts/{account_id}/files/download")
async def download_file(
    account_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(...),
) -> FileResponse:
    account = await _get_account(account_id, db)
    try:
        full_path = safe_path(account.username, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not full_path.exists() or full_path.is_dir():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=str(full_path), filename=full_path.name)


@router.get("/accounts/{account_id}/usage")
async def get_usage(
    account_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    account = await _get_account(account_id, db)
    bytes_used = dir_usage_bytes(account.username)
    return {
        "bytes": bytes_used,
        "mb": round(bytes_used / 1024 / 1024, 2),
        "quota_mb": account.quota_mb,
    }


# ── Presets ───────────────────────────────────────────────────────────────────


@router.get("/presets")
async def list_presets(
    _user: Annotated[User, Depends(get_current_user)],
) -> list[dict]:
    settings = get_settings()
    fqdn = settings.server.fqdn
    return [
        {
            "id": p.id.value,
            "name": p.name,
            "description": p.description,
            "cucm_config": {
                k: v.replace("{fqdn}", fqdn).replace(
                    "{username}", f"<{p.default_username_prefix}…>"
                )
                for k, v in p.cucm_config.items()
            },
            "default_keep_count": p.default_keep_count,
        }
        for p in PRESETS.values()
    ]


@router.post("/presets/{preset_id}", response_model=AccountOut, status_code=201)
async def apply_preset(
    preset_id: SftpPreset,
    body: ApplyPresetRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SftpAccount:
    if preset_id not in PRESETS:
        raise HTTPException(status_code=404, detail="Unknown preset")
    preset_def = PRESETS[preset_id]

    create_body = CreateAccountRequest(
        username=body.username,
        display_name=preset_def.name,
        auth_type=body.auth_type,
        password=body.password,
        ssh_key=body.ssh_key,
        preset=preset_id,
        quota_mb=None,
    )
    account = await create_account(create_body, user, db)

    # Apply default retention if defined by preset
    if preset_def.default_keep_count:
        policy = RetentionPolicy(
            account_id=account.id,
            keep_count=preset_def.default_keep_count,
            enabled=True,
        )
        db.add(policy)
        await db.commit()

    return account


# ── Retention ─────────────────────────────────────────────────────────────────


@router.get("/accounts/{account_id}/retention", response_model=RetentionOut)
async def get_retention(
    account_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RetentionOut:
    account = await _get_account(account_id, db)
    if account.retention is None:
        return RetentionOut(keep_count=None, keep_days=None, enabled=False)
    return RetentionOut.model_validate(account.retention)


@router.put("/accounts/{account_id}/retention", response_model=RetentionOut)
async def set_retention(
    account_id: int,
    body: RetentionRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RetentionOut:
    account = await _get_account(account_id, db)

    if account.retention is None:
        policy = RetentionPolicy(
            account_id=account.id,
            keep_count=body.keep_count,
            keep_days=body.keep_days,
            enabled=body.enabled,
        )
        db.add(policy)
    else:
        account.retention.keep_count = body.keep_count
        account.retention.keep_days = body.keep_days
        account.retention.enabled = body.enabled

    await db.commit()
    await db.refresh(account)

    await AuditWriter(db).write(
        actor=user.username,
        action="sftp.retention.set",
        resource=account.username,
        new_value={"keep_count": body.keep_count, "keep_days": body.keep_days},
    )

    return RetentionOut.model_validate(account.retention)


@router.post("/accounts/{account_id}/retention/run")
async def run_retention_now(
    account_id: int,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    account = await _get_account(account_id, db)
    if account.retention is None or not account.retention.enabled:
        raise HTTPException(status_code=400, detail="No retention policy configured or not enabled")

    deleted = run_retention(
        account.username,
        account.retention.keep_count,
        account.retention.keep_days,
    )

    await AuditWriter(db).write(
        actor=user.username,
        action="sftp.retention.run",
        resource=account.username,
        new_value={"deleted_count": len(deleted), "files": deleted},
    )
    return {"deleted": deleted, "count": len(deleted)}
