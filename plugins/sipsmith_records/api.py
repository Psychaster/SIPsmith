"""Records Landing REST API — mounted at /api/v1/plugins/records."""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_records.models import CallJourney, CallRecord, DrfSet, RecordSource, SourceType
from sipsmith_records.parsers.q850 import CAUSE_CODES

log = logging.getLogger("sipsmith.records.api")

router = APIRouter(prefix="/plugins/records", tags=["records"])

# ── Sources ───────────────────────────────────────────────────────────────────


class SourceCreateRequest(BaseModel):
    name: str
    source_type: str
    cluster_name: str | None = None
    sftp_account: str | None = None
    sftp_path: str | None = None
    retention_days: int = 90


class SourcePatchRequest(BaseModel):
    name: str | None = None
    cluster_name: str | None = None
    sftp_account: str | None = None
    sftp_path: str | None = None
    retention_days: int | None = None
    status: str | None = None


@router.get("/sources")
async def list_sources(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(select(RecordSource).order_by(RecordSource.name))
    sources = result.scalars().all()
    out = []
    for s in sources:
        rec_count = (
            await db.scalar(select(func.count(CallRecord.id)).where(CallRecord.source_id == s.id)) or 0
        )
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "source_type": s.source_type,
                "cluster_name": s.cluster_name,
                "sftp_account": s.sftp_account,
                "sftp_path": s.sftp_path,
                "receiver_token": s.receiver_token,
                "retention_days": s.retention_days,
                "status": s.status,
                "last_ingested_at": s.last_ingested_at.isoformat() if s.last_ingested_at else None,
                "record_count": rec_count,
            }
        )
    return out


@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def create_source(
    req: SourceCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    if req.source_type not in [t.value for t in SourceType]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown source_type: {req.source_type}",
        )
    token = None
    if req.source_type == SourceType.cms_cdr:
        token = secrets.token_urlsafe(32)

    src = RecordSource(
        name=req.name,
        source_type=req.source_type,
        cluster_name=req.cluster_name,
        sftp_account=req.sftp_account,
        sftp_path=req.sftp_path,
        receiver_token=token,
        retention_days=req.retention_days,
    )
    db.add(src)
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Source name already exists"
        ) from exc
    await db.refresh(src)
    await AuditWriter(db).write(
        user_id=user.id, action="records.source.create", detail=f"name={src.name}"
    )
    return {"id": src.id, "receiver_token": src.receiver_token}


@router.patch("/sources/{source_id}")
async def patch_source(
    source_id: int,
    req: SourcePatchRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    src = await db.get(RecordSource, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if req.name is not None:
        src.name = req.name
    if req.cluster_name is not None:
        src.cluster_name = req.cluster_name
    if req.sftp_account is not None:
        src.sftp_account = req.sftp_account
    if req.sftp_path is not None:
        src.sftp_path = req.sftp_path
    if req.retention_days is not None:
        src.retention_days = req.retention_days
    if req.status is not None:
        src.status = req.status
    await db.commit()
    await AuditWriter(db).write(
        user_id=user.id, action="records.source.patch", detail=f"id={source_id}"
    )
    return {"ok": True}


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    src = await db.get(RecordSource, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    await db.delete(src)
    await db.commit()
    await AuditWriter(db).write(
        user_id=user.id, action="records.source.delete", detail=f"id={source_id}"
    )


@router.post("/sources/{source_id}/rotate-token")
async def rotate_token(
    source_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    src = await db.get(RecordSource, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if src.source_type != SourceType.cms_cdr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only CMS CDR sources have tokens"
        )
    src.receiver_token = secrets.token_urlsafe(32)
    await db.commit()
    await AuditWriter(db).write(
        user_id=user.id, action="records.source.rotate_token", detail=f"id={source_id}"
    )
    return {"receiver_token": src.receiver_token}


@router.post("/sources/{source_id}/trigger-ingest")
async def trigger_ingest(
    source_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    src = await db.get(RecordSource, source_id)
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if src.source_type not in (SourceType.cucm_cdr, SourceType.cucm_drf):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Manual ingest only available for SFTP sources",
        )
    from sipsmith_records.receivers.cucm_sftp import ingest_source

    count = await ingest_source(src, db)
    return {"inserted": count}


# ── Search ────────────────────────────────────────────────────────────────────


@router.get("/search")
async def search_records(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    calling: str | None = None,
    called: str | None = None,
    source_id: int | None = None,
    cause_code: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    q = select(CallRecord).order_by(desc(CallRecord.start_time))
    if calling:
        q = q.where(CallRecord.calling_dn.ilike(f"%{calling}%"))
    if called:
        q = q.where(CallRecord.called_dn.ilike(f"%{called}%"))
    if source_id is not None:
        q = q.where(CallRecord.source_id == source_id)
    if cause_code is not None:
        q = q.where(CallRecord.cause_code == cause_code)

    total_result = await db.scalar(select(func.count()).select_from(q.subquery()))
    result = await db.execute(q.limit(limit).offset(offset))
    records = result.scalars().all()

    return {
        "total": total_result or 0,
        "offset": offset,
        "limit": limit,
        "records": [_record_dict(r) for r in records],
    }


def _record_dict(r: CallRecord) -> dict:
    return {
        "id": r.id,
        "source_id": r.source_id,
        "global_call_id": r.global_call_id,
        "sip_call_id": r.sip_call_id,
        "calling_dn": r.calling_dn,
        "called_dn": r.called_dn,
        "calling_device": r.calling_device,
        "called_device": r.called_device,
        "start_time": r.start_time.isoformat() if r.start_time else None,
        "end_time": r.end_time.isoformat() if r.end_time else None,
        "duration_sec": r.duration_sec,
        "cause_code": r.cause_code,
        "cause_label": r.cause_label,
        "mos": r.mos,
        "journey_id": r.journey_id,
        "raw_source": r.raw_source,
    }


# ── Analytics ─────────────────────────────────────────────────────────────────


@router.get("/analytics/cause-codes")
async def cause_code_breakdown(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    source_id: int | None = None,
) -> list[dict]:
    q = select(CallRecord.cause_code, func.count(CallRecord.id).label("count")).group_by(
        CallRecord.cause_code
    )
    if source_id is not None:
        q = q.where(CallRecord.source_id == source_id)
    result = await db.execute(q.order_by(desc("count")))
    rows = result.all()
    out = []
    for code, count in rows:
        label = CAUSE_CODES.get(code, {}).get("label", "Unknown") if code is not None else "None"
        out.append({"cause_code": code, "label": label, "count": count})
    return out


@router.get("/analytics/volume")
async def call_volume(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    source_id: int | None = None,
) -> dict:
    q = select(func.count(CallRecord.id))
    if source_id is not None:
        q = q.where(CallRecord.source_id == source_id)
    total = await db.scalar(q) or 0

    avg_dur_result = await db.scalar(
        select(func.avg(CallRecord.duration_sec)).where(CallRecord.duration_sec.isnot(None))
    )
    return {
        "total_records": total,
        "avg_duration_sec": round(float(avg_dur_result), 1) if avg_dur_result else None,
    }


# ── Journeys ──────────────────────────────────────────────────────────────────


@router.get("/journeys/{journey_id}")
async def get_journey(
    journey_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    journey = await db.get(CallJourney, journey_id)
    if journey is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Journey not found")
    result = await db.execute(
        select(CallRecord)
        .where(CallRecord.journey_id == journey_id)
        .order_by(CallRecord.start_time)
    )
    legs = result.scalars().all()
    return {
        "id": journey.id,
        "anchor_global_call_id": journey.anchor_global_call_id,
        "start_time": journey.start_time.isoformat() if journey.start_time else None,
        "end_time": journey.end_time.isoformat() if journey.end_time else None,
        "leg_count": journey.leg_count,
        "cause_code": journey.cause_code,
        "legs": [_record_dict(leg) for leg in legs],
    }


# ── DRF sets ──────────────────────────────────────────────────────────────────


@router.get("/drf-sets")
async def list_drf_sets(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    source_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    q = select(DrfSet).order_by(desc(DrfSet.backup_date)).limit(limit)
    if source_id is not None:
        q = q.where(DrfSet.source_id == source_id)
    result = await db.execute(q)
    sets = result.scalars().all()
    return [
        {
            "id": s.id,
            "source_id": s.source_id,
            "cluster_name": s.cluster_name,
            "backup_date": s.backup_date.isoformat() if s.backup_date else None,
            "file_count": s.file_count,
            "total_bytes": s.total_bytes,
            "complete": s.complete,
            "path": s.path,
        }
        for s in sets
    ]
