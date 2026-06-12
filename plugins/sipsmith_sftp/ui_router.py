"""SFTP plugin GUI routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.config import get_settings
from sipsmith.database import get_db
from sipsmith.models.user import User
from sipsmith_sftp.models import SftpAccount
from sipsmith_sftp.presets import PRESETS

router = APIRouter(prefix="/plugins/sftp", tags=["sftp-ui"])

_here = Path(__file__).parent
# Resolve core templates dir relative to this file's location inside plugins/
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    """Return Jinja2Templates with plugin templates + core templates merged."""
    plugin_dir = _here / "ui" / "templates"
    return Jinja2Templates(directory=[str(plugin_dir), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def sftp_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(SftpAccount).order_by(SftpAccount.id))
    accounts = result.scalars().all()
    return _templates().TemplateResponse(
        request,
        "sftp_index.html",
        {"request": request, "user": user, "accounts": accounts},
    )


@router.get("/presets", response_class=HTMLResponse)
async def sftp_presets(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    settings = get_settings()
    fqdn = settings.server.fqdn
    presets_data = [
        {
            "id": p.id.value,
            "name": p.name,
            "description": p.description,
            "default_username_prefix": p.default_username_prefix,
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
    return _templates().TemplateResponse(
        request,
        "sftp_presets.html",
        {"request": request, "user": user, "presets": presets_data},
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
async def sftp_account_detail(
    account_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(SftpAccount).where(SftpAccount.id == account_id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404)
    return _templates().TemplateResponse(
        request,
        "sftp_account.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "retention": account.retention,
        },
    )
