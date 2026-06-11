from __future__ import annotations

import logging
import socket
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
from sipsmith_ad.models import AdDomain, AdGroup, AdOU, AdSyncAccount, AdUser

log = logging.getLogger("sipsmith.ad.ui")

router = APIRouter(prefix="/plugins/ad", tags=["ad-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def ad_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    domain = None
    user_count = 0
    group_count = 0
    ou_count = 0
    try:
        domain = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
        user_count = await db.scalar(func.count(AdUser.id)) or 0
        group_count = await db.scalar(func.count(AdGroup.id)) or 0
        ou_count = await db.scalar(func.count(AdOU.id)) or 0
    except Exception:  # noqa: BLE001, S110
        pass

    appliance_ip = "127.0.0.1"
    try:
        from sipsmith.config import get_settings

        host = get_settings().server.host
        if host not in ("0.0.0.0", ""):  # noqa: S104
            appliance_ip = host
        else:
            appliance_ip = socket.gethostbyname(socket.getfqdn())
    except Exception:  # noqa: BLE001, S110
        pass

    return _templates().TemplateResponse(
        "ad_index.html",
        {
            "request": request,
            "user": user,
            "domain": domain,
            "user_count": user_count,
            "group_count": group_count,
            "ou_count": ou_count,
            "appliance_ip": appliance_ip,
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def ad_users(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    offset: int = 0,
    limit: int = 50,
) -> HTMLResponse:
    domain = None
    users: list[dict] = []
    groups: list[dict] = []
    ous: list[dict] = []
    total_users = 0
    try:
        domain = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
        total_users = await db.scalar(func.count(AdUser.id)) or 0
        u_result = await db.execute(
            select(AdUser).order_by(AdUser.sam_account).offset(offset).limit(limit)
        )
        users = [
            {
                "id": u.id,
                "sam_account": u.sam_account,
                "display_name": u.display_name,
                "telephone_number": u.telephone_number,
                "ip_phone": u.ip_phone,
                "ou_dn": u.ou_dn,
                "enabled": u.enabled,
            }
            for u in u_result.scalars().all()
        ]
        g_result = await db.execute(select(AdGroup).order_by(AdGroup.name))
        groups = [{"id": g.id, "name": g.name, "ou_dn": g.ou_dn} for g in g_result.scalars().all()]
        o_result = await db.execute(select(AdOU).order_by(AdOU.name))
        ous = [{"id": o.id, "name": o.name, "dn": o.dn} for o in o_result.scalars().all()]
    except Exception:  # noqa: BLE001, S110
        pass

    return _templates().TemplateResponse(
        "ad_users.html",
        {
            "request": request,
            "user": user,
            "domain": domain,
            "users": users,
            "groups": groups,
            "ous": ous,
            "total_users": total_users,
            "offset": offset,
            "limit": limit,
        },
    )


@router.get("/sync", response_class=HTMLResponse)
async def ad_sync(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    domain = None
    sync_accounts: list[dict] = []
    appliance_ip = "127.0.0.1"
    try:
        domain = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
        s_result = await db.execute(select(AdSyncAccount).order_by(AdSyncAccount.sam_account))
        sync_accounts = [
            {
                "id": s.id,
                "sam_account": s.sam_account,
                "display_name": s.display_name,
                "purpose": s.purpose,
            }
            for s in s_result.scalars().all()
        ]
        from sipsmith.config import get_settings

        host = get_settings().server.host
        if host not in ("0.0.0.0", ""):  # noqa: S104
            appliance_ip = host
        else:
            appliance_ip = socket.gethostbyname(socket.getfqdn())
    except Exception:  # noqa: BLE001, S110
        pass

    return _templates().TemplateResponse(
        "ad_sync.html",
        {
            "request": request,
            "user": user,
            "domain": domain,
            "sync_accounts": sync_accounts,
            "appliance_ip": appliance_ip,
        },
    )
