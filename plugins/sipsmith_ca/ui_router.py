"""CA plugin GUI routes."""

from __future__ import annotations

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
from sipsmith_ca.models import CaCertificate

router = APIRouter(prefix="/plugins/ca", tags=["ca-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def ca_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from sipsmith_ca.crypto import is_initialized

    initialized = is_initialized()
    expiry_soon: list[CaCertificate] = []
    if initialized:
        import datetime

        cutoff = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=90)
        result = await db.execute(
            select(CaCertificate)
            .where(
                ~CaCertificate.revoked,
                ~CaCertificate.is_ca,
                CaCertificate.not_after <= cutoff,
            )
            .order_by(CaCertificate.not_after)
            .limit(10)
        )
        expiry_soon = list(result.scalars().all())

    return _templates().TemplateResponse(
        request,
        "ca_index.html",
        {
            "request": request,
            "user": user,
            "initialized": initialized,
            "expiry_soon": expiry_soon,
        },
    )


@router.get("/setup", response_class=HTMLResponse)
async def ca_setup_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    return _templates().TemplateResponse(request, "ca_setup.html", {"request": request, "user": user})


@router.get("/sign", response_class=HTMLResponse)
async def ca_sign_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    from sipsmith_ca.crypto import PROFILES

    return _templates().TemplateResponse(
        request,
        "ca_sign.html",
        {"request": request, "user": user, "profiles": PROFILES},
    )


@router.get("/certs", response_class=HTMLResponse)
async def ca_certs_page(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(CaCertificate).order_by(CaCertificate.not_after))
    certs = list(result.scalars().all())
    return _templates().TemplateResponse(
        request,
        "ca_certs.html",
        {"request": request, "user": user, "certs": certs},
    )


@router.get("/certs/{cert_id}", response_class=HTMLResponse)
async def ca_cert_detail(
    cert_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from fastapi import HTTPException

    result = await db.execute(select(CaCertificate).where(CaCertificate.id == cert_id))
    cert = result.scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404)

    import json

    sans = json.loads(cert.san_json or "[]")
    return _templates().TemplateResponse(
        request,
        "ca_cert_detail.html",
        {"request": request, "user": user, "cert": cert, "sans": sans},
    )
