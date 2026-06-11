"""xAPI plugin REST API — mounted at /api/v1/plugins/xapi."""

from __future__ import annotations

import datetime
import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_xapi.models import XapiCommandLog, XapiDevice
from sipsmith_xapi.xapi_client import XapiClient

log = logging.getLogger("sipsmith.xapi.api")

router = APIRouter(prefix="/plugins/xapi", tags=["xapi"])


# ── Pydantic schemas ─────────────────────────────────────────────────────────


class DeviceCreate(BaseModel):
    name: str
    ip_address: str
    username: str = "admin"
    password: str = ""
    transport: str = "https"
    poll_enabled: bool = True


class DeviceUpdate(BaseModel):
    name: str | None = None
    ip_address: str | None = None
    username: str | None = None
    password: str | None = None
    transport: str | None = None
    poll_enabled: bool | None = None


class DialRequest(BaseModel):
    number: str
    protocol: str = "SIP"
    call_rate: int = 768


class ConfigSetRequest(BaseModel):
    path: str
    value: str


class MacroRequest(BaseModel):
    name: str
    action: str = "activate"  # "activate" | "deactivate"


class DTMFRequest(BaseModel):
    digits: str
    call_id: int | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _device_dict(d: XapiDevice) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "ip_address": d.ip_address,
        "username": d.username,
        "transport": d.transport,
        "poll_enabled": d.poll_enabled,
        "model": d.model,
        "sw_version": d.sw_version,
        "sip_uri": d.sip_uri,
        "reg_state": d.reg_state,
        "last_seen": d.last_seen.isoformat() if d.last_seen else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _client(device: XapiDevice) -> XapiClient:
    return XapiClient(
        ip=device.ip_address,
        username=device.username,
        password=device.password or "",
        transport=device.transport,
    )


async def _get_device_or_404(device_id: int, db: AsyncSession) -> XapiDevice:
    device = await db.get(XapiDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


async def _log_command(
    db: AsyncSession,
    device_id: int,
    command_path: str,
    params: dict | None,
    result: dict | None,
    ok: bool,
) -> None:
    entry = XapiCommandLog(
        device_id=device_id,
        command_path=command_path,
        params_json=json.dumps(params) if params is not None else None,
        result_json=json.dumps(result) if result is not None else None,
        ok=ok,
        ts=datetime.datetime.now(datetime.UTC),
    )
    db.add(entry)
    await db.commit()


async def _refresh_device(device: XapiDevice, db: AsyncSession) -> None:
    """Pull system info from device and update DB row. Best-effort — catches all errors."""
    try:
        info = await _client(device).get_system_info()
        device.model = info.get("model") or device.model
        device.sw_version = info.get("sw_version") or device.sw_version
        device.sip_uri = info.get("sip_uri") or device.sip_uri
        device.reg_state = info.get("reg_state") or "unknown"
        device.last_seen = datetime.datetime.now(datetime.UTC)
        await db.commit()
    except (httpx.HTTPError, Exception):  # noqa: BLE001
        log.debug("xAPI poll failed for device %s (%s)", device.name, device.ip_address)


# ── Device CRUD ──────────────────────────────────────────────────────────────


@router.get("/devices")
async def list_devices(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(select(XapiDevice).order_by(XapiDevice.name))
    return [_device_dict(d) for d in result.scalars().all()]


@router.post("/devices", status_code=201)
async def create_device(
    body: DeviceCreate,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    existing = await db.scalar(select(XapiDevice.id).where(XapiDevice.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail=f"Device name {body.name!r} already exists")

    device = XapiDevice(
        name=body.name,
        ip_address=body.ip_address,
        username=body.username,
        password=body.password or None,
        transport=body.transport,
        poll_enabled=body.poll_enabled,
        reg_state="unknown",
    )
    db.add(device)
    await db.flush()

    # Attempt immediate poll — device may be offline; never block creation
    try:
        info = await _client(device).get_system_info()
        device.model = info.get("model") or None
        device.sw_version = info.get("sw_version") or None
        device.sip_uri = info.get("sip_uri") or None
        device.reg_state = info.get("reg_state") or "unknown"
        device.last_seen = datetime.datetime.now(datetime.UTC)
    except (httpx.HTTPError, Exception):  # noqa: BLE001
        log.debug("Initial poll failed for new device %s — saved with reg_state=unknown", body.name)

    await AuditWriter(db).write(
        actor=user.username,
        action="xapi.create_device",
        resource=body.name,
        new_value={"ip_address": body.ip_address, "transport": body.transport},
    )
    await db.commit()
    return _device_dict(device)


@router.get("/devices/{device_id}")
async def get_device(
    device_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    active_calls = await _client(device).get_active_calls()
    result = _device_dict(device)
    result["active_calls"] = active_calls
    return result


@router.put("/devices/{device_id}")
async def update_device(
    device_id: int,
    body: DeviceUpdate,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    old = _device_dict(device)

    if body.name is not None:
        device.name = body.name
    if body.ip_address is not None:
        device.ip_address = body.ip_address
    if body.username is not None:
        device.username = body.username
    if body.password is not None:
        device.password = body.password or None
    if body.transport is not None:
        device.transport = body.transport
    if body.poll_enabled is not None:
        device.poll_enabled = body.poll_enabled

    await AuditWriter(db).write(
        actor=user.username,
        action="xapi.update_device",
        resource=device.name,
        old_value=old,
        new_value=body.model_dump(exclude_none=True),
    )
    await db.commit()
    return _device_dict(device)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    device = await _get_device_or_404(device_id, db)
    name = device.name
    await db.delete(device)
    await AuditWriter(db).write(
        actor=user.username,
        action="xapi.delete_device",
        resource=name,
    )
    await db.commit()


# ── Poll ─────────────────────────────────────────────────────────────────────


@router.post("/devices/{device_id}/poll")
async def poll_device(
    device_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    try:
        info = await _client(device).get_system_info()
        device.model = info.get("model") or device.model
        device.sw_version = info.get("sw_version") or device.sw_version
        device.sip_uri = info.get("sip_uri") or device.sip_uri
        device.reg_state = info.get("reg_state") or "unknown"
        device.last_seen = datetime.datetime.now(datetime.UTC)
    except (httpx.HTTPError, Exception):  # noqa: BLE001
        device.reg_state = "unknown"
        log.debug("Poll failed for device %s", device.name)
    await db.commit()
    return _device_dict(device)


@router.post("/devices/poll-all")
async def poll_all_devices(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Refresh all poll-enabled devices serially. Returns updated/error counts."""
    result = await db.execute(
        select(XapiDevice).where(XapiDevice.poll_enabled.is_(True)).order_by(XapiDevice.name)
    )
    devices = result.scalars().all()
    updated = 0
    errors = 0
    for device in devices:
        try:
            info = await _client(device).get_system_info()
            device.model = info.get("model") or device.model
            device.sw_version = info.get("sw_version") or device.sw_version
            device.sip_uri = info.get("sip_uri") or device.sip_uri
            device.reg_state = info.get("reg_state") or "unknown"
            device.last_seen = datetime.datetime.now(datetime.UTC)
            updated += 1
        except Exception:  # noqa: BLE001
            log.debug("poll-all: failed to reach device %s (%s)", device.name, device.ip_address)
            errors += 1
    await db.commit()
    return {"updated": updated, "errors": errors}


# ── Call control ─────────────────────────────────────────────────────────────


@router.post("/devices/{device_id}/dial")
async def dial(
    device_id: int,
    body: DialRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    params = {"number": body.number, "protocol": body.protocol, "call_rate": body.call_rate}
    ok = True
    result: dict = {}
    try:
        result = await _client(device).dial(body.number, body.protocol, body.call_rate)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, "Dial", params, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/hangup")
async def hangup(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: int | None = None,
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    try:
        result = await _client(device).hangup(call_id)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(
        db, device_id, "Call/Disconnect", {"call_id": call_id} if call_id else None, result, ok
    )
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/hold")
async def hold(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: int = Query(...),
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    try:
        result = await _client(device).hold(call_id)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, "Call/Hold", {"call_id": call_id}, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/resume")
async def resume(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    call_id: int = Query(...),
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    try:
        result = await _client(device).resume(call_id)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, "Call/Resume", {"call_id": call_id}, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/dtmf")
async def dtmf(
    device_id: int,
    body: DTMFRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    params = {"digits": body.digits, "call_id": body.call_id}
    ok = True
    result: dict = {}
    try:
        result = await _client(device).dtmf(body.digits, body.call_id)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, "Call/DTMFSend", params, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/volume")
async def volume(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: int = Query(..., ge=0, le=100),
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    try:
        result = await _client(device).volume_set(level)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, "Audio/Volume/Set", {"level": level}, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


@router.post("/devices/{device_id}/standby")
async def standby(
    device_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
    active: bool = Query(...),
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    command_path = "Standby/Activate" if active else "Standby/Deactivate"
    try:
        if active:
            result = await _client(device).standby_activate()
        else:
            result = await _client(device).standby_deactivate()
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(db, device_id, command_path, {"active": active}, result, ok)
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


# ── Live status / calls ──────────────────────────────────────────────────────


@router.get("/devices/{device_id}/calls")
async def get_active_calls(
    device_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    device = await _get_device_or_404(device_id, db)
    return await _client(device).get_active_calls()


@router.get("/devices/{device_id}/status")
async def get_device_status(
    device_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    try:
        return await _client(device).status()
    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Configuration ────────────────────────────────────────────────────────────


@router.get("/devices/{device_id}/config")
async def get_config(
    device_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    path: str = Query(...),
) -> dict:
    device = await _get_device_or_404(device_id, db)
    try:
        return await _client(device).config_get(path)
    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/devices/{device_id}/config")
async def set_config(
    device_id: int,
    body: ConfigSetRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    ok = True
    result: dict = {}
    try:
        result = await _client(device).config_set(body.path, body.value)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(
        db, device_id, f"configuration/{body.path}", {"value": body.value}, result, ok
    )
    await AuditWriter(db).write(
        actor=user.username,
        action="xapi.set_config",
        resource=f"{device.name}/{body.path}",
        new_value={"path": body.path, "value": body.value},
    )
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


# ── Macros ───────────────────────────────────────────────────────────────────


@router.post("/devices/{device_id}/macro")
async def macro(
    device_id: int,
    body: MacroRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    device = await _get_device_or_404(device_id, db)
    if body.action not in ("activate", "deactivate"):
        raise HTTPException(status_code=422, detail="action must be 'activate' or 'deactivate'")

    ok = True
    result: dict = {}
    command_path = (
        "Macros/Macro/Activate" if body.action == "activate" else "Macros/Macro/Deactivate"
    )
    try:
        if body.action == "activate":
            result = await _client(device).macro_activate(body.name)
        else:
            result = await _client(device).macro_deactivate(body.name)
    except Exception as exc:  # noqa: BLE001
        ok = False
        result = {"error": str(exc)}
    await _log_command(
        db, device_id, command_path, {"name": body.name, "action": body.action}, result, ok
    )
    if not ok:
        raise HTTPException(status_code=502, detail=result.get("error", "xAPI error"))
    return result


# ── Command log ──────────────────────────────────────────────────────────────


@router.get("/command-log")
async def get_command_log(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    device_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    q = select(XapiCommandLog).order_by(XapiCommandLog.ts.desc()).limit(limit).offset(offset)
    if device_id is not None:
        q = q.where(XapiCommandLog.device_id == device_id)
    result = await db.execute(q)
    return [
        {
            "id": row.id,
            "device_id": row.device_id,
            "command_path": row.command_path,
            "params_json": row.params_json,
            "result_json": row.result_json,
            "ok": row.ok,
            "ts": row.ts.isoformat() if row.ts else None,
        }
        for row in result.scalars().all()
    ]
