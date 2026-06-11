"""CTI plugin GUI routes — mounted at /plugins/cti."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.database import get_db
from sipsmith.models.user import User
from sipsmith_cti.manager import get_all_managers
from sipsmith_cti.models import CtiCluster, CtiDevice

log = logging.getLogger("sipsmith.cti.ui")

router = APIRouter(prefix="/plugins/cti", tags=["cti-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"

_SIDECAR_JAR = "/opt/sipsmith/lib/sipsmith-cti-sidecar.jar"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def cti_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    try:
        result = await db.execute(select(CtiCluster).order_by(CtiCluster.name))
        clusters = [
            {
                "id": c.id,
                "name": c.name,
                "cucm_host": c.cucm_host,
                "status": c.status,
                "cucm_version": c.cucm_version,
            }
            for c in result.scalars().all()
        ]
    except Exception:  # noqa: BLE001, S110
        clusters = []

    sidecar_available = Path(_SIDECAR_JAR).exists()

    return _templates().TemplateResponse(
        "cti_index.html",
        {
            "request": request,
            "user": user,
            "clusters": clusters,
            "sidecar_available": sidecar_available,
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def cti_dashboard(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    try:
        cluster_result = await db.execute(select(CtiCluster).order_by(CtiCluster.name))
        raw_clusters = cluster_result.scalars().all()

        device_result = await db.execute(
            select(CtiDevice).order_by(CtiDevice.cluster_id, CtiDevice.device_name)
        )
        raw_devices = device_result.scalars().all()

        # Group devices by cluster_id for template convenience
        devices_by_cluster: dict[int, list[dict]] = {}
        for d in raw_devices:
            devices_by_cluster.setdefault(d.cluster_id, []).append(
                {
                    "device_name": d.device_name,
                    "directory_number": d.directory_number,
                    "reg_state": d.reg_state,
                    "active_call_id": d.active_call_id,
                }
            )

        managers = get_all_managers()
        clusters = [
            {
                "id": c.id,
                "name": c.name,
                "cucm_host": c.cucm_host,
                "status": (managers[c.id].status if c.id in managers else c.status),
                "cucm_version": c.cucm_version,
                "devices": devices_by_cluster.get(c.id, []),
            }
            for c in raw_clusters
        ]
    except Exception:  # noqa: BLE001, S110
        clusters = []

    return _templates().TemplateResponse(
        "cti_dashboard.html",
        {
            "request": request,
            "user": user,
            "clusters": clusters,
        },
    )


@router.get("/clusters/{cluster_id}/setup")
async def cti_cluster_setup(
    cluster_id: int,
    user: Annotated[User, Depends(get_current_user)],
) -> RedirectResponse:
    # Cluster setup is handled by JS modals on the dashboard page.
    return RedirectResponse(url="/plugins/cti/dashboard", status_code=302)
