"""Records Landing GUI routes."""

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
from sipsmith_records.models import CallJourney, CallRecord, RecordSource
from sipsmith_records.parsers.q850 import CAUSE_CODES

log = logging.getLogger("sipsmith.records.ui")

router = APIRouter(prefix="/plugins/records", tags=["records-ui"])

_here = Path(__file__).parent
_core_templates_dir = Path(__file__).parent.parent.parent / "sipsmith" / "ui" / "templates"


def _templates() -> Jinja2Templates:
    return Jinja2Templates(directory=[str(_here / "ui" / "templates"), str(_core_templates_dir)])


@router.get("", response_class=HTMLResponse)
async def records_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    result = await db.execute(select(RecordSource).order_by(RecordSource.name))
    sources = result.scalars().all()
    source_rows = []
    for s in sources:
        count = await db.scalar(func.count(CallRecord.id).where(CallRecord.source_id == s.id)) or 0
        source_rows.append(
            {
                "id": s.id,
                "name": s.name,
                "source_type": s.source_type,
                "cluster_name": s.cluster_name,
                "status": s.status,
                "record_count": count,
                "last_ingested_at": (
                    s.last_ingested_at.strftime("%Y-%m-%d %H:%M") if s.last_ingested_at else "—"
                ),
            }
        )
    total_records = await db.scalar(func.count(CallRecord.id)) or 0
    total_journeys = await db.scalar(func.count(CallJourney.id)) or 0
    return _templates().TemplateResponse(
        "records_index.html",
        {
            "request": request,
            "user": user,
            "sources": source_rows,
            "total_records": total_records,
            "total_journeys": total_journeys,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def records_search(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    calling: str = "",
    called: str = "",
    source_id: str = "",
    cause_code: str = "",
    page: int = 1,
) -> HTMLResponse:
    limit = 50
    offset = (page - 1) * limit

    q = select(CallRecord).order_by(desc(CallRecord.start_time))
    if calling:
        q = q.where(CallRecord.calling_dn.ilike(f"%{calling}%"))
    if called:
        q = q.where(CallRecord.called_dn.ilike(f"%{called}%"))
    if source_id:
        try:
            q = q.where(CallRecord.source_id == int(source_id))
        except ValueError:
            pass
    if cause_code:
        try:
            q = q.where(CallRecord.cause_code == int(cause_code))
        except ValueError:
            pass

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0
    result = await db.execute(q.limit(limit).offset(offset))
    records = result.scalars().all()

    src_result = await db.execute(select(RecordSource).order_by(RecordSource.name))
    sources = src_result.scalars().all()

    return _templates().TemplateResponse(
        "records_search.html",
        {
            "request": request,
            "user": user,
            "records": records,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": (total + limit - 1) // limit if total else 1,
            "sources": sources,
            "filters": {
                "calling": calling,
                "called": called,
                "source_id": source_id,
                "cause_code": cause_code,
            },
        },
    )


@router.get("/journeys/{journey_id}", response_class=HTMLResponse)
async def records_journey(
    journey_id: int,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    journey = await db.get(CallJourney, journey_id)
    if journey is None:
        return HTMLResponse("<p>Journey not found</p>", status_code=404)

    result = await db.execute(
        select(CallRecord)
        .where(CallRecord.journey_id == journey_id)
        .order_by(CallRecord.start_time)
    )
    legs = result.scalars().all()

    cause_label = None
    if journey.cause_code is not None:
        cause_label = CAUSE_CODES.get(journey.cause_code, {}).get("label", "Unknown")

    return _templates().TemplateResponse(
        "records_journey.html",
        {
            "request": request,
            "user": user,
            "journey": journey,
            "legs": legs,
            "cause_label": cause_label,
        },
    )
