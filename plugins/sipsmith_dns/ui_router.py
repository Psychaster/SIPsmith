"""DNS plugin GUI routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.database import get_db
from sipsmith.models.user import User
from sipsmith_dns.models import DnsRecord, DnsSettings, DnsZone
from sipsmith_dns.presets import PRESET_CATALOG

router = APIRouter(prefix="/plugins/dns", tags=["dns-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


async def _get_settings(db: AsyncSession) -> DnsSettings:
    result = await db.execute(select(DnsSettings).where(DnsSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        s = DnsSettings(id=1)
        db.add(s)
        await db.flush()
    return s


@router.get("", response_class=HTMLResponse)
async def dns_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    zone_count = await db.scalar(func.count(DnsZone.id)) or 0
    record_count = await db.scalar(func.count(DnsRecord.id)) or 0
    return _templates().TemplateResponse(
        "dns_index.html",
        {
            "request": request,
            "user": user,
            "zone_count": zone_count,
            "record_count": record_count,
        },
    )


@router.get("/zones", response_class=HTMLResponse)
async def dns_zones(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    zones_result = await db.execute(select(DnsZone).order_by(DnsZone.name))
    zones = zones_result.scalars().all()
    zone_rows = []
    for z in zones:
        cnt = await db.scalar(func.count(DnsRecord.id).where(DnsRecord.zone_id == z.id)) or 0
        zone_rows.append({"zone": z, "record_count": cnt})
    return _templates().TemplateResponse(
        "dns_zones.html",
        {"request": request, "user": user, "zone_rows": zone_rows},
    )


@router.get("/zones/{zone_id}", response_class=HTMLResponse)
async def dns_zone_detail(
    zone_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from fastapi import HTTPException

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)

    recs_result = await db.execute(
        select(DnsRecord)
        .where(DnsRecord.zone_id == zone_id)
        .order_by(DnsRecord.record_type, DnsRecord.name)
    )
    records = recs_result.scalars().all()

    return _templates().TemplateResponse(
        "dns_zone.html",
        {
            "request": request,
            "user": user,
            "zone": z,
            "records": records,
            "preset_catalog": PRESET_CATALOG,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def dns_settings_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    import json

    s = await _get_settings(db)
    forwarders = json.loads(s.forwarders or "[]")
    allow_recursion = json.loads(s.allow_recursion or '["any"]')
    return _templates().TemplateResponse(
        "dns_settings.html",
        {
            "request": request,
            "user": user,
            "settings": s,
            "forwarders": forwarders,
            "allow_recursion": allow_recursion,
        },
    )
