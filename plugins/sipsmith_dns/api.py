"""DNS plugin REST API — mounted at /api/v1/plugins/dns."""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_dns.models import DnsRecord, DnsSettings, DnsZone, RecordType

log = logging.getLogger("sipsmith.dns.api")

router = APIRouter(prefix="/plugins/dns", tags=["dns"])


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_settings(db: AsyncSession) -> DnsSettings:
    result = await db.execute(select(DnsSettings).where(DnsSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        s = DnsSettings(id=1)
        db.add(s)
        await db.flush()
    return s


async def _apply_all(db: AsyncSession) -> None:
    """Re-render all zones and apply via agent. Best-effort — logs on failure."""
    from sipsmith.agent.client import AgentClient, AgentError
    from sipsmith_dns.zones import render_named_conf, render_zone_file

    settings = await _get_settings(db)
    zones_result = await db.execute(select(DnsZone).where(DnsZone.enabled).order_by(DnsZone.name))
    zones = zones_result.scalars().all()

    zone_files: dict[str, str] = {}
    for zone in zones:
        recs_result = await db.execute(
            select(DnsRecord).where(DnsRecord.zone_id == zone.id, DnsRecord.enabled)
        )
        records = recs_result.scalars().all()
        zone_files[zone.name] = render_zone_file(zone, list(records))

    conf = render_named_conf(list(zones), settings)
    try:
        client = AgentClient()
        await client.named_apply_config(conf, zone_files)
    except AgentError as exc:
        log.warning("named_apply_config failed (agent unavailable?): %s", exc)


# ── Pydantic models ───────────────────────────────────────────────────────────


class ZoneOut(BaseModel):
    id: int
    name: str
    zone_type: str
    ttl: int
    serial: int
    ns_primary: str
    admin_email: str
    enabled: bool
    record_count: int = 0

    model_config = {"from_attributes": True}


class ZoneCreateRequest(BaseModel):
    name: str
    ns_primary: str
    admin_email: str
    ttl: int = 3600
    zone_type: str = "primary"


class RecordOut(BaseModel):
    id: int
    zone_id: int
    name: str
    record_type: str
    ttl: int | None
    rdata: str
    priority: int | None
    weight: int | None
    port: int | None
    enabled: bool

    model_config = {"from_attributes": True}


class RecordCreateRequest(BaseModel):
    name: str
    record_type: RecordType
    rdata: str
    ttl: int | None = None
    priority: int | None = None
    weight: int | None = None
    port: int | None = None


class SettingsOut(BaseModel):
    forwarders: list[str]
    allow_recursion: list[str]
    dlz_enabled: bool
    dlz_module_path: str | None


class SettingsUpdateRequest(BaseModel):
    forwarders: list[str] = []
    allow_recursion: list[str] = ["any"]
    dlz_enabled: bool = False
    dlz_module_path: str | None = None


class PresetApplyRequest(BaseModel):
    preset: str  # "cucm" | "expressway" | "imp"
    cucm_fqdn: str | None = None
    exp_edge_fqdn: str | None = None
    imp_fqdn: str | None = None


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def dns_status(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    zone_count = await db.scalar(func.count(DnsZone.id))
    record_count = await db.scalar(func.count(DnsRecord.id))
    return {"zone_count": zone_count or 0, "record_count": record_count or 0}


# ── Zones ─────────────────────────────────────────────────────────────────────


@router.get("/zones")
async def list_zones(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    zones_result = await db.execute(select(DnsZone).order_by(DnsZone.name))
    zones = zones_result.scalars().all()
    out = []
    for z in zones:
        cnt = await db.scalar(select(func.count(DnsRecord.id)).where(DnsRecord.zone_id == z.id))
        out.append(
            {
                "id": z.id,
                "name": z.name,
                "zone_type": z.zone_type,
                "ttl": z.ttl,
                "serial": z.serial,
                "ns_primary": z.ns_primary,
                "admin_email": z.admin_email,
                "enabled": z.enabled,
                "record_count": cnt or 0,
            }
        )
    return out


@router.post("/zones", status_code=201)
async def create_zone(
    body: ZoneCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_dns.zones import next_serial, validate_zone_name

    if not validate_zone_name(body.name):
        raise HTTPException(status_code=422, detail=f"Invalid zone name: {body.name!r}")

    existing = await db.scalar(select(DnsZone.id).where(DnsZone.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail=f"Zone {body.name!r} already exists")

    zone = DnsZone(
        name=body.name.rstrip("."),
        zone_type=body.zone_type,
        ttl=body.ttl,
        serial=next_serial(0),
        ns_primary=body.ns_primary,
        admin_email=body.admin_email,
    )
    db.add(zone)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="dns.create_zone",
        resource=body.name,
        new_value={"name": body.name, "type": body.zone_type},
    )
    await db.commit()
    await _apply_all(db)
    return {"id": zone.id, "name": zone.name}


@router.get("/zones/{zone_id}")
async def get_zone(
    zone_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)
    cnt = await db.scalar(select(func.count(DnsRecord.id)).where(DnsRecord.zone_id == z.id))
    return {
        "id": z.id,
        "name": z.name,
        "zone_type": z.zone_type,
        "ttl": z.ttl,
        "serial": z.serial,
        "refresh": z.refresh,
        "retry": z.retry,
        "expire": z.expire,
        "ns_primary": z.ns_primary,
        "admin_email": z.admin_email,
        "enabled": z.enabled,
        "record_count": cnt or 0,
    }


@router.delete("/zones/{zone_id}", status_code=204)
async def delete_zone(
    zone_id: int,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)
    name = z.name
    await db.delete(z)
    await AuditWriter(db).write(
        actor=user.username,
        action="dns.delete_zone",
        resource=name,
    )
    await db.commit()
    await _apply_all(db)


# ── Records ───────────────────────────────────────────────────────────────────


@router.get("/zones/{zone_id}/records")
async def list_records(
    zone_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    record_type: str | None = Query(default=None),
) -> list[dict]:
    q = (
        select(DnsRecord)
        .where(DnsRecord.zone_id == zone_id)
        .order_by(DnsRecord.record_type, DnsRecord.name)
    )
    if record_type:
        q = q.where(DnsRecord.record_type == record_type.upper())
    result = await db.execute(q)
    return [
        {
            "id": r.id,
            "zone_id": r.zone_id,
            "name": r.name,
            "record_type": r.record_type,
            "ttl": r.ttl,
            "rdata": r.rdata,
            "priority": r.priority,
            "weight": r.weight,
            "port": r.port,
            "enabled": r.enabled,
        }
        for r in result.scalars().all()
    ]


@router.post("/zones/{zone_id}/records", status_code=201)
async def create_record(
    zone_id: int,
    body: RecordCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_dns.zones import next_serial, validate_record_name

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404, detail="Zone not found")

    if not validate_record_name(body.name):
        raise HTTPException(status_code=422, detail=f"Invalid record name: {body.name!r}")

    rec = DnsRecord(
        zone_id=zone_id,
        name=body.name,
        record_type=body.record_type.value,
        ttl=body.ttl,
        rdata=body.rdata,
        priority=body.priority,
        weight=body.weight,
        port=body.port,
    )
    db.add(rec)
    z.serial = next_serial(z.serial)
    await AuditWriter(db).write(
        actor=user.username,
        action="dns.create_record",
        resource=f"{body.name}.{z.name}",
        new_value={"type": body.record_type.value, "rdata": body.rdata},
    )
    await db.commit()
    await _apply_all(db)
    return {"id": rec.id, "name": rec.name, "record_type": rec.record_type}


@router.put("/zones/{zone_id}/records/{record_id}")
async def update_record(
    zone_id: int,
    record_id: int,
    body: RecordCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_dns.zones import next_serial

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    rec = await db.get(DnsRecord, record_id)
    if rec is None or rec.zone_id != zone_id:
        raise HTTPException(status_code=404, detail="Record not found")

    old = {"name": rec.name, "type": rec.record_type, "rdata": rec.rdata}
    rec.name = body.name
    rec.record_type = body.record_type.value
    rec.rdata = body.rdata
    rec.ttl = body.ttl
    rec.priority = body.priority
    rec.weight = body.weight
    rec.port = body.port
    z.serial = next_serial(z.serial)

    await AuditWriter(db).write(
        actor=user.username,
        action="dns.update_record",
        resource=f"{body.name}.{z.name}",
        old_value=old,
        new_value={"type": body.record_type.value, "rdata": body.rdata},
    )
    await db.commit()
    await _apply_all(db)
    return {"id": rec.id, "name": rec.name, "record_type": rec.record_type}


@router.delete("/zones/{zone_id}/records/{record_id}", status_code=204)
async def delete_record(
    zone_id: int,
    record_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith_dns.zones import next_serial

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)
    rec = await db.get(DnsRecord, record_id)
    if rec is None or rec.zone_id != zone_id:
        raise HTTPException(status_code=404)

    name = f"{rec.name}.{z.name}"
    await db.delete(rec)
    z.serial = next_serial(z.serial)
    await AuditWriter(db).write(actor=user.username, action="dns.delete_record", resource=name)
    await db.commit()
    await _apply_all(db)


# ── Presets ───────────────────────────────────────────────────────────────────


@router.get("/presets")
async def get_presets(
    _user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from sipsmith_dns.presets import PRESET_CATALOG

    return PRESET_CATALOG


@router.post("/zones/{zone_id}/apply-presets", status_code=201)
async def apply_presets(
    zone_id: int,
    body: PresetApplyRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_dns.presets import (
        cisco_cucm_presets,
        cisco_expressway_presets,
        cisco_imp_presets,
    )
    from sipsmith_dns.zones import next_serial

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)

    preset_map = {
        "cucm": (cisco_cucm_presets, body.cucm_fqdn),
        "expressway": (cisco_expressway_presets, body.exp_edge_fqdn),
        "imp": (cisco_imp_presets, body.imp_fqdn),
    }
    if body.preset not in preset_map:
        raise HTTPException(status_code=422, detail=f"Unknown preset: {body.preset!r}")

    fn, fqdn = preset_map[body.preset]
    if not fqdn:
        raise HTTPException(status_code=422, detail="FQDN required for this preset")

    presets = fn(fqdn)
    created = 0
    for p in presets:
        rec = DnsRecord(
            zone_id=zone_id,
            name=p["name"],
            record_type=p["record_type"],
            ttl=p.get("ttl"),
            rdata=p["rdata"],
            priority=p.get("priority"),
            weight=p.get("weight"),
            port=p.get("port"),
        )
        db.add(rec)
        created += 1

    z.serial = next_serial(z.serial)
    await AuditWriter(db).write(
        actor=user.username,
        action="dns.apply_preset",
        resource=z.name,
        new_value={"preset": body.preset, "fqdn": fqdn, "records_added": created},
    )
    await db.commit()
    await _apply_all(db)
    return {"records_added": created}


# ── Settings ──────────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SettingsOut:
    s = await _get_settings(db)
    return SettingsOut(
        forwarders=json.loads(s.forwarders or "[]"),
        allow_recursion=json.loads(s.allow_recursion or '["any"]'),
        dlz_enabled=s.dlz_enabled,
        dlz_module_path=s.dlz_module_path,
    )


@router.put("/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SettingsOut:
    s = await _get_settings(db)
    old = {
        "forwarders": json.loads(s.forwarders or "[]"),
        "dlz_enabled": s.dlz_enabled,
    }
    s.forwarders = json.dumps(body.forwarders)
    s.allow_recursion = json.dumps(body.allow_recursion)
    s.dlz_enabled = body.dlz_enabled
    s.dlz_module_path = body.dlz_module_path
    await AuditWriter(db).write(
        actor=user.username,
        action="dns.update_settings",
        resource="settings",
        old_value=old,
        new_value={"forwarders": body.forwarders, "dlz_enabled": body.dlz_enabled},
    )
    await db.commit()
    await _apply_all(db)
    return SettingsOut(
        forwarders=body.forwarders,
        allow_recursion=body.allow_recursion,
        dlz_enabled=body.dlz_enabled,
        dlz_module_path=body.dlz_module_path,
    )


# ── Reload / import ───────────────────────────────────────────────────────────


@router.post("/reload")
async def trigger_reload(
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    await _apply_all(db)
    await AuditWriter(db).write(actor=user.username, action="dns.reload", resource="named")
    await db.commit()
    return {"ok": True}


@router.post("/zones/{zone_id}/import-csv", status_code=201)
async def import_csv(
    zone_id: int,
    file: Annotated[UploadFile, File()],
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Bulk import A/AAAA records from CSV (columns: name,rdata[,ttl])."""
    from sipsmith_dns.zones import next_serial, validate_ipv4, validate_ipv6

    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)

    content = (await file.read()).decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    created = 0
    errors = []

    for i, row in enumerate(reader, start=2):
        name = (row.get("name") or "").strip()
        rdata = (row.get("rdata") or row.get("ip") or "").strip()
        ttl_raw = (row.get("ttl") or "").strip()
        ttl = int(ttl_raw) if ttl_raw.isdigit() else None

        if not name or not rdata:
            errors.append(f"row {i}: missing name or rdata")
            continue

        if validate_ipv4(rdata):
            rtype = "A"
        elif validate_ipv6(rdata):
            rtype = "AAAA"
        else:
            errors.append(f"row {i}: {rdata!r} is not a valid IPv4/IPv6 address")
            continue

        db.add(DnsRecord(zone_id=zone_id, name=name, record_type=rtype, rdata=rdata, ttl=ttl))
        created += 1

    if created:
        z.serial = next_serial(z.serial)
        await AuditWriter(db).write(
            actor=user.username,
            action="dns.import_csv",
            resource=z.name,
            new_value={"imported": created},
        )
        await db.commit()
        await _apply_all(db)

    return {"imported": created, "errors": errors}


@router.get("/zones/{zone_id}/export-csv")
async def export_csv(
    zone_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    z = await db.get(DnsZone, zone_id)
    if z is None:
        raise HTTPException(status_code=404)

    result = await db.execute(
        select(DnsRecord)
        .where(DnsRecord.zone_id == zone_id)
        .order_by(DnsRecord.record_type, DnsRecord.name)
    )
    records = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "type", "ttl", "rdata", "priority", "weight", "port"])
    for r in records:
        writer.writerow(
            [
                r.name,
                r.record_type,
                r.ttl or "",
                r.rdata,
                r.priority or "",
                r.weight or "",
                r.port or "",
            ]
        )

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={z.name}-records.csv"},
    )
