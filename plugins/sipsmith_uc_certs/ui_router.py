"""UC Cert Orchestrator GUI routes."""

from __future__ import annotations

import json
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
from sipsmith_uc_certs.models import UcCertPlan, UcCertPlanItem, UcCluster, UcNode

router = APIRouter(prefix="/plugins/uc-certs", tags=["uc-certs-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])
    t.env.filters["fromjson"] = json.loads
    return t


@router.get("", response_class=HTMLResponse)
async def uc_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(UcCluster).order_by(UcCluster.name))
    clusters = result.scalars().all()
    cluster_rows = []
    for c in clusters:
        cnt = await db.scalar(func.count(UcNode.id).where(UcNode.cluster_id == c.id)) or 0
        cluster_rows.append(
            {
                "id": c.id,
                "name": c.name,
                "product": c.product,
                "version": c.version,
                "node_count": cnt,
            }
        )

    return _templates().TemplateResponse(
        "uc_index.html",
        {"request": request, "user": user, "clusters": cluster_rows},
    )


@router.get("/clusters/{cluster_id}", response_class=HTMLResponse)
async def uc_cluster(
    cluster_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from fastapi import HTTPException

    cluster = await db.get(UcCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404)

    nodes_result = await db.execute(
        select(UcNode).where(UcNode.cluster_id == cluster_id).order_by(UcNode.sort_order)
    )
    nodes = nodes_result.scalars().all()

    plans_result = await db.execute(
        select(UcCertPlan)
        .where(UcCertPlan.cluster_id == cluster_id)
        .order_by(UcCertPlan.generated_at.desc())
    )
    plans_raw = plans_result.scalars().all()
    plan_rows = []
    for p in plans_raw:
        cnt = (
            await db.scalar(func.count(UcCertPlanItem.id).where(UcCertPlanItem.plan_id == p.id))
            or 0
        )
        plan_rows.append(
            {
                "id": p.id,
                "status": p.status,
                "generated_at": p.generated_at,
                "item_count": cnt,
                "cluster_id": p.cluster_id,
            }
        )

    return _templates().TemplateResponse(
        "uc_cluster.html",
        {
            "request": request,
            "user": user,
            "cluster": cluster,
            "domains": json.loads(cluster.domains or "[]"),
            "toggles": json.loads(cluster.toggles or "{}"),
            "nodes": nodes,
            "plans": plan_rows,
        },
    )


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
async def uc_plan(
    plan_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    from fastapi import HTTPException

    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)

    cluster = await db.get(UcCluster, plan.cluster_id)

    items_result = await db.execute(
        select(UcCertPlanItem)
        .where(UcCertPlanItem.plan_id == plan_id)
        .order_by(UcCertPlanItem.restart_priority)
    )
    items = items_result.scalars().all()

    dns_report = None
    if plan.dns_preflight:
        dns_report = json.loads(plan.dns_preflight)

    return _templates().TemplateResponse(
        "uc_plan.html",
        {
            "request": request,
            "user": user,
            "plan": plan,
            "cluster": cluster,
            "items": items,
            "dns_report": dns_report,
        },
    )
