"""SIP Endpoint Emulator GUI routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.database import get_db
from sipsmith.models.user import User
from sipsmith_sip_emu.models import Endpoint, EndpointCall, Scenario, ScenarioRun
from sipsmith_sip_emu.worker.manager import get_manager

log = logging.getLogger("sipsmith.sip_emu.ui")

router = APIRouter(prefix="/plugins/sip-emu", tags=["sip-emu-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def emu_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))
    endpoints = [
        {
            "id": ep.id,
            "name": ep.name,
            "sip_user": ep.sip_user,
            "sip_domain": ep.sip_domain,
            "reg_state": ep.reg_state,
            "transport": ep.transport,
            "group_name": ep.group_name,
        }
        for ep in result.scalars().all()
    ]

    active_call_count = (
        await db.scalar(
            select(func.count(EndpointCall.id)).where(EndpointCall.call_state != "disconnected")
        )
        or 0
    )

    manager = get_manager()
    worker_status = manager.status if manager else "stopped"

    return _templates().TemplateResponse(
        request,
        "emu_index.html",
        {
            "request": request,
            "user": user,
            "endpoints": endpoints,
            "active_call_count": active_call_count,
            "worker_status": worker_status,
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def emu_dashboard(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    ep_result = await db.execute(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))
    endpoints = [
        {
            "id": ep.id,
            "name": ep.name,
            "sip_user": ep.sip_user,
            "sip_domain": ep.sip_domain,
            "reg_state": ep.reg_state,
        }
        for ep in ep_result.scalars().all()
    ]

    call_result = await db.execute(
        select(EndpointCall, Endpoint.name)
        .join(Endpoint, EndpointCall.endpoint_id == Endpoint.id)
        .where(EndpointCall.call_state != "disconnected")
        .order_by(desc(EndpointCall.started_at))
    )
    active_calls = [
        {
            "id": call.id,
            "endpoint_name": ep_name,
            "remote_uri": call.remote_uri,
            "direction": call.direction,
            "call_state": call.call_state,
            "started_at": (call.started_at.strftime("%H:%M:%S") if call.started_at else "—"),
            "audio_muted": call.audio_muted,
            "on_hold": call.on_hold,
        }
        for call, ep_name in call_result.all()
    ]

    return _templates().TemplateResponse(
        request,
        "emu_dashboard.html",
        {
            "request": request,
            "user": user,
            "endpoints": endpoints,
            "active_calls": active_calls,
        },
    )


@router.get("/scenarios", response_class=HTMLResponse)
async def emu_scenarios(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    sc_result = await db.execute(select(Scenario).order_by(Scenario.name))
    scenarios = [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "created_at": (s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "—"),
        }
        for s in sc_result.scalars().all()
    ]

    run_result = await db.execute(
        select(ScenarioRun).order_by(desc(ScenarioRun.created_at)).limit(10)
    )
    recent_runs = [
        {
            "id": r.id,
            "scenario_name": r.scenario_name,
            "status": r.status,
            "started_at": (r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "—"),
            "steps_total": r.steps_total,
            "steps_passed": r.steps_passed,
            "steps_failed": r.steps_failed,
        }
        for r in run_result.scalars().all()
    ]

    return _templates().TemplateResponse(
        request,
        "emu_scenarios.html",
        {
            "request": request,
            "user": user,
            "scenarios": scenarios,
            "recent_runs": recent_runs,
        },
    )
