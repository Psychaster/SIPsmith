"""xAPI plugin GUI routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.database import get_db
from sipsmith.models.user import User
from sipsmith_xapi.models import XapiCommandLog, XapiDevice
from sipsmith_xapi.xapi_client import XapiClient

log = logging.getLogger("sipsmith.xapi.ui")

router = APIRouter(prefix="/plugins/xapi", tags=["xapi-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


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


@router.get("", response_class=HTMLResponse)
async def xapi_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    devices: list[dict] = []
    total_devices = 0
    connected_count = 0
    try:
        result = await db.execute(select(XapiDevice).order_by(XapiDevice.name))
        rows = result.scalars().all()
        devices = [_device_dict(d) for d in rows]
        total_devices = len(devices)
        connected_count = sum(1 for d in rows if d.reg_state == "Connected")
    except Exception:  # noqa: BLE001
        log.debug("xapi_index: failed to load devices")
    return _templates().TemplateResponse(
        "xapi_index.html",
        {
            "request": request,
            "user": user,
            "devices": devices,
            "total_devices": total_devices,
            "connected_count": connected_count,
        },
    )


@router.get("/devices/{device_id}", response_class=HTMLResponse)
async def xapi_device_detail(
    device_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    device: dict = {}
    recent_commands: list[dict] = []
    active_calls: list[dict] = []
    try:
        row = await db.get(XapiDevice, device_id)
        if row is not None:
            device = _device_dict(row)
            logs_result = await db.execute(
                select(XapiCommandLog)
                .where(XapiCommandLog.device_id == device_id)
                .order_by(XapiCommandLog.ts.desc())
                .limit(10)
            )
            recent_commands = [
                {
                    "id": entry.id,
                    "command_path": entry.command_path,
                    "params_json": entry.params_json,
                    "result_json": entry.result_json,
                    "ok": entry.ok,
                    "ts": entry.ts.isoformat() if entry.ts else None,
                }
                for entry in logs_result.scalars().all()
            ]
            try:
                client = XapiClient(
                    ip=row.ip_address,
                    username=row.username,
                    password=row.password or "",
                    transport=row.transport,
                )
                active_calls = await client.get_active_calls()
            except Exception:  # noqa: BLE001
                active_calls = []
    except Exception:  # noqa: BLE001
        log.debug("xapi_device_detail: failed to load device %s", device_id)
    return _templates().TemplateResponse(
        "xapi_device.html",
        {
            "request": request,
            "user": user,
            "device": device,
            "recent_commands": recent_commands,
            "active_calls": active_calls,
        },
    )
