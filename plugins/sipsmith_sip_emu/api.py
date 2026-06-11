"""SIP Endpoint Emulator REST API — mounted at /plugins/sip-emu."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_sip_emu.models import (
    Endpoint,
    EndpointCall,
    RtpStatsSample,
    Scenario,
    ScenarioRun,
    SipEvent,
)
from sipsmith_sip_emu.scenarios.engine import run_scenario
from sipsmith_sip_emu.scenarios.schema import ScenarioDefinition
from sipsmith_sip_emu.worker.manager import get_manager

log = logging.getLogger("sipsmith.sip_emu.api")

router = APIRouter(prefix="/plugins/sip-emu", tags=["sip-emu"])

# ── Helpers ───────────────────────────────────────────────────────────────────

_OPERATOR_ROLES = (Role.operator, Role.admin)


def _ep_to_dict(ep: Endpoint) -> dict[str, Any]:
    return {
        "id": ep.id,
        "name": ep.name,
        "sip_user": ep.sip_user,
        "sip_domain": ep.sip_domain,
        "display_name": ep.display_name,
        "registrar_uri": ep.registrar_uri,
        "transport": ep.transport,
        "group_name": ep.group_name,
        "sort_order": ep.sort_order,
        "use_srtp": ep.use_srtp,
        "video_enabled": ep.video_enabled,
        "audio_codec": ep.audio_codec,
        "reg_state": ep.reg_state,
        "reg_expires": ep.reg_expires,
        "reg_contact": ep.reg_contact,
        "reg_error_detail": ep.reg_error_detail,
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
    }


def _call_to_dict(call: EndpointCall, endpoint_name: str | None = None) -> dict[str, Any]:
    return {
        "id": call.id,
        "endpoint_id": call.endpoint_id,
        "endpoint_name": endpoint_name,
        "pjsip_call_id": call.pjsip_call_id,
        "local_uri": call.local_uri,
        "remote_uri": call.remote_uri,
        "direction": call.direction,
        "call_state": call.call_state,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "connected_at": call.connected_at.isoformat() if call.connected_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration_sec": call.duration_sec,
        "cause_code": call.cause_code,
        "cause_label": call.cause_label,
        "audio_muted": call.audio_muted,
        "video_muted": call.video_muted,
        "on_hold": call.on_hold,
        "journey_id": call.journey_id,
        "ingested_at": call.ingested_at.isoformat() if call.ingested_at else None,
    }


def _worker_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="SIP worker is not available (pjsua2 may not be installed)",
    )


# ── Pydantic request bodies ───────────────────────────────────────────────────


class EndpointCreateRequest(BaseModel):
    name: str
    sip_user: str
    sip_domain: str
    sip_password: str | None = None
    display_name: str | None = None
    registrar_uri: str | None = None
    transport: str = "udp"
    group_name: str | None = None
    sort_order: int = 0
    use_srtp: bool = False
    video_enabled: bool = False
    audio_codec: str = "PCMU"


class EndpointPatchRequest(BaseModel):
    name: str | None = None
    sip_user: str | None = None
    sip_domain: str | None = None
    sip_password: str | None = None
    display_name: str | None = None
    registrar_uri: str | None = None
    transport: str | None = None
    group_name: str | None = None
    sort_order: int | None = None
    use_srtp: bool | None = None
    video_enabled: bool | None = None
    audio_codec: str | None = None


class CallCreateRequest(BaseModel):
    endpoint_id: int
    dest_uri: str


class MuteRequest(BaseModel):
    audio: bool
    video: bool


class DtmfRequest(BaseModel):
    digits: str


class ScenarioCreateRequest(BaseModel):
    name: str
    description: str | None = None
    yaml_content: str


# ── Endpoints CRUD ────────────────────────────────────────────────────────────


@router.get("/endpoints")
async def list_endpoints(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))
    return [_ep_to_dict(ep) for ep in result.scalars().all()]


@router.post("/endpoints", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: EndpointCreateRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    ep = Endpoint(
        name=body.name,
        sip_user=body.sip_user,
        sip_domain=body.sip_domain,
        sip_password=body.sip_password,
        display_name=body.display_name,
        registrar_uri=body.registrar_uri,
        transport=body.transport,
        group_name=body.group_name,
        sort_order=body.sort_order,
        use_srtp=body.use_srtp,
        video_enabled=body.video_enabled,
        audio_codec=body.audio_codec,
    )
    db.add(ep)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.endpoint.create",
        resource=f"endpoint:{ep.id}",
        new_value=body.name,
    )
    await db.commit()
    return {"id": ep.id}


@router.patch("/endpoints/{ep_id}")
async def patch_endpoint(
    ep_id: int,
    body: EndpointPatchRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    ep = await db.get(Endpoint, ep_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    old_name = ep.name
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(ep, field, val)
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.endpoint.update",
        resource=f"endpoint:{ep_id}",
        old_value=old_name,
        new_value=ep.name,
    )
    await db.commit()
    return _ep_to_dict(ep)


@router.delete("/endpoints/{ep_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    ep_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    ep = await db.get(Endpoint, ep_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.endpoint.delete",
        resource=f"endpoint:{ep_id}",
        old_value=ep.name,
    )
    await db.delete(ep)
    await db.commit()


@router.post("/endpoints/{ep_id}/register")
async def register_endpoint(
    ep_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    ep = await db.get(Endpoint, ep_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    ep_data = {
        "id": ep.id,
        "name": ep.name,
        "sip_user": ep.sip_user,
        "sip_domain": ep.sip_domain,
        "sip_password": ep.sip_password,
        "display_name": ep.display_name,
        "registrar_uri": ep.registrar_uri,
        "transport": ep.transport,
        "use_srtp": ep.use_srtp,
        "audio_codec": ep.audio_codec,
    }
    await manager.send_command("register", ep_data)
    ep.reg_state = "registering"
    await db.commit()
    return {"status": "registering"}


@router.post("/endpoints/{ep_id}/unregister")
async def unregister_endpoint(
    ep_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    ep = await db.get(Endpoint, ep_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await manager.send_command("unregister", {"ep_id": ep_id})
    ep.reg_state = "unregistered"
    await db.commit()
    return {"status": "unregistered"}


@router.post("/endpoints/register-all")
async def register_all_endpoints(
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    result = await db.execute(select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name))
    endpoints = result.scalars().all()
    count = 0
    for ep in endpoints:
        ep_data = {
            "id": ep.id,
            "name": ep.name,
            "sip_user": ep.sip_user,
            "sip_domain": ep.sip_domain,
            "sip_password": ep.sip_password,
            "display_name": ep.display_name,
            "registrar_uri": ep.registrar_uri,
            "transport": ep.transport,
            "use_srtp": ep.use_srtp,
            "audio_codec": ep.audio_codec,
        }
        await manager.send_command("register", ep_data)
        ep.reg_state = "registering"
        count += 1
    await db.commit()
    return {"registered": count}


# ── Calls ─────────────────────────────────────────────────────────────────────


@router.get("/calls")
async def list_calls(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(EndpointCall, Endpoint.name)
        .join(Endpoint, EndpointCall.endpoint_id == Endpoint.id)
        .where(EndpointCall.call_state != "disconnected")
        .order_by(desc(EndpointCall.started_at))
    )
    rows = result.all()
    return [_call_to_dict(call, ep_name) for call, ep_name in rows]


@router.post("/calls", status_code=status.HTTP_201_CREATED)
async def create_call(
    body: CallCreateRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    ep = await db.get(Endpoint, body.endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    call = EndpointCall(
        endpoint_id=body.endpoint_id,
        remote_uri=body.dest_uri,
        direction="outbound",
        call_state="calling",
        started_at=datetime.datetime.now(datetime.UTC),
    )
    db.add(call)
    await db.flush()
    call_id = call.id

    await manager.send_command("call", {"ep_id": body.endpoint_id, "dest_uri": body.dest_uri})
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.call.create",
        resource=f"call:{call_id}",
        new_value=body.dest_uri,
    )
    await db.commit()
    return {"id": call_id}


@router.get("/calls/{call_id}")
async def get_call(
    call_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    ep = await db.get(Endpoint, call.endpoint_id)

    events_result = await db.execute(
        select(SipEvent).where(SipEvent.call_id == call_id).order_by(SipEvent.ts).limit(100)
    )
    events = [
        {
            "id": e.id,
            "ts": e.ts.isoformat(),
            "direction": e.direction,
            "method": e.method,
            "status_code": e.status_code,
            "from_participant": e.from_participant,
            "to_participant": e.to_participant,
            "cseq": e.cseq,
            "raw_first_line": e.raw_first_line,
        }
        for e in events_result.scalars().all()
    ]

    rtp_result = await db.execute(
        select(RtpStatsSample)
        .where(RtpStatsSample.call_id == call_id)
        .order_by(desc(RtpStatsSample.ts))
        .limit(60)
    )
    samples = rtp_result.scalars().all()
    samples_sorted = sorted(samples, key=lambda s: s.ts)
    rtp = [
        {
            "id": s.id,
            "ts": s.ts.isoformat(),
            "audio_tx_pkt": s.audio_tx_pkt,
            "audio_rx_pkt": s.audio_rx_pkt,
            "jitter_ms": s.jitter_ms,
            "rtt_ms": s.rtt_ms,
            "loss_pct": s.loss_pct,
            "mos": s.mos,
        }
        for s in samples_sorted
    ]

    detail = _call_to_dict(call, ep.name if ep else None)
    detail["sip_events"] = events
    detail["rtp_samples"] = rtp
    return detail


@router.get("/calls/{call_id}/events")
async def get_call_events(
    call_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(SipEvent).where(SipEvent.call_id == call_id).order_by(SipEvent.ts)
    )
    return [
        {
            "id": e.id,
            "ts": e.ts.isoformat(),
            "direction": e.direction,
            "method": e.method,
            "status_code": e.status_code,
            "from_participant": e.from_participant,
            "to_participant": e.to_participant,
            "cseq": e.cseq,
            "raw_first_line": e.raw_first_line,
        }
        for e in result.scalars().all()
    ]


@router.get("/calls/{call_id}/rtp")
async def get_call_rtp(
    call_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(RtpStatsSample)
        .where(RtpStatsSample.call_id == call_id)
        .order_by(desc(RtpStatsSample.ts))
        .limit(120)
    )
    samples = sorted(result.scalars().all(), key=lambda s: s.ts)
    return [
        {
            "id": s.id,
            "ts": s.ts.isoformat(),
            "audio_tx_pkt": s.audio_tx_pkt,
            "audio_rx_pkt": s.audio_rx_pkt,
            "audio_tx_bytes": s.audio_tx_bytes,
            "audio_rx_bytes": s.audio_rx_bytes,
            "jitter_ms": s.jitter_ms,
            "rtt_ms": s.rtt_ms,
            "loss_pct": s.loss_pct,
            "mos": s.mos,
        }
        for s in samples
    ]


@router.post("/calls/{call_id}/hangup")
async def hangup_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.pjsip_call_id:
        raise HTTPException(status_code=400, detail="Call has no pjsip_call_id")
    await manager.send_command("hangup", {"call_id": call.pjsip_call_id})
    return {"status": "hangup_sent"}


@router.post("/calls/{call_id}/hold")
async def hold_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.pjsip_call_id:
        raise HTTPException(status_code=400, detail="Call has no pjsip_call_id")
    await manager.send_command("hold", {"call_id": call.pjsip_call_id})
    call.on_hold = True
    await db.commit()
    return {"status": "hold_sent"}


@router.post("/calls/{call_id}/unhold")
async def unhold_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.pjsip_call_id:
        raise HTTPException(status_code=400, detail="Call has no pjsip_call_id")
    await manager.send_command("unhold", {"call_id": call.pjsip_call_id})
    call.on_hold = False
    await db.commit()
    return {"status": "unhold_sent"}


@router.post("/calls/{call_id}/mute")
async def mute_call(
    call_id: int,
    body: MuteRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.pjsip_call_id:
        raise HTTPException(status_code=400, detail="Call has no pjsip_call_id")
    await manager.send_command(
        "mute", {"call_id": call.pjsip_call_id, "audio": body.audio, "video": body.video}
    )
    call.audio_muted = body.audio
    call.video_muted = body.video
    await db.commit()
    return {"status": "mute_sent"}


@router.post("/calls/{call_id}/dtmf")
async def dtmf_call(
    call_id: int,
    body: DtmfRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()
    call = await db.get(EndpointCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not call.pjsip_call_id:
        raise HTTPException(status_code=400, detail="Call has no pjsip_call_id")
    await manager.send_command("dtmf", {"call_id": call.pjsip_call_id, "digits": body.digits})
    return {"status": "dtmf_sent"}


# ── SSE stream ────────────────────────────────────────────────────────────────


@router.get("/events")
async def sse_events(
    user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    """Server-Sent Events stream of worker events."""

    async def _generator():  # type: ignore[return]
        import json

        yield "retry: 2000\n\n"
        last_event = asyncio.get_event_loop().time()
        while True:
            manager = get_manager()
            if manager is None:
                await asyncio.sleep(1)
                # keepalive while worker is not up
                if asyncio.get_event_loop().time() - last_event > 15:
                    yield ": keepalive\n\n"
                    last_event = asyncio.get_event_loop().time()
                continue

            event = await manager.next_event(timeout=1.0)
            now = asyncio.get_event_loop().time()
            if event is not None:
                yield f"data: {json.dumps(event)}\n\n"
                last_event = now
            elif now - last_event > 15:
                yield ": keepalive\n\n"
                last_event = now

    return StreamingResponse(_generator(), media_type="text/event-stream")


# ── Scenarios ─────────────────────────────────────────────────────────────────


@router.get("/scenarios")
async def list_scenarios(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(select(Scenario).order_by(Scenario.name))
    return [
        {
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in result.scalars().all()
    ]


@router.post("/scenarios", status_code=status.HTTP_201_CREATED)
async def create_scenario(
    body: ScenarioCreateRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    try:
        defn = ScenarioDefinition.from_yaml(body.yaml_content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"YAML parse error: {exc}") from exc

    errors = defn.validate_actions()
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    scenario = Scenario(
        name=body.name,
        description=body.description,
        yaml_content=body.yaml_content,
    )
    db.add(scenario)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.scenario.create",
        resource=f"scenario:{scenario.id}",
        new_value=body.name,
    )
    await db.commit()
    return {"id": scenario.id}


@router.delete("/scenarios/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scenario(
    scenario_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await AuditWriter(db).write(
        actor=user.username,
        action="sip_emu.scenario.delete",
        resource=f"scenario:{scenario_id}",
        old_value=scenario.name,
    )
    await db.delete(scenario)
    await db.commit()


@router.post("/scenarios/{scenario_id}/run", status_code=status.HTTP_201_CREATED)
async def run_scenario_endpoint(
    scenario_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None or not manager.is_available:
        raise _worker_unavailable()

    scenario = await db.get(Scenario, scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")

    run = ScenarioRun(
        scenario_id=scenario_id,
        scenario_name=scenario.name,
        status="pending",
        triggered_by=user.username,
    )
    db.add(run)
    await db.flush()
    run_id = run.id
    await db.commit()

    from sipsmith.database import AsyncSessionLocal

    async def _bg_run() -> None:
        async with AsyncSessionLocal() as bg_db:
            await run_scenario(scenario_id, run_id, bg_db, manager)

    asyncio.create_task(_bg_run(), name=f"scenario-run-{run_id}")
    return {"run_id": run_id}


# ── Scenario runs ─────────────────────────────────────────────────────────────


@router.get("/scenario-runs")
async def list_scenario_runs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(select(ScenarioRun).order_by(desc(ScenarioRun.created_at)).limit(20))
    return [
        {
            "id": r.id,
            "scenario_id": r.scenario_id,
            "scenario_name": r.scenario_name,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "steps_total": r.steps_total,
            "steps_passed": r.steps_passed,
            "steps_failed": r.steps_failed,
            "triggered_by": r.triggered_by,
        }
        for r in result.scalars().all()
    ]


@router.get("/scenario-runs/{run_id}")
async def get_scenario_run(
    run_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await db.get(ScenarioRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ScenarioRun not found")
    return {
        "id": run.id,
        "scenario_id": run.scenario_id,
        "scenario_name": run.scenario_name,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "steps_total": run.steps_total,
        "steps_passed": run.steps_passed,
        "steps_failed": run.steps_failed,
        "log_output": run.log_output,
        "triggered_by": run.triggered_by,
    }


# ── Worker status ─────────────────────────────────────────────────────────────


@router.get("/worker/status")
async def worker_status(
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    manager = get_manager()
    if manager is None:
        return {"status": "stopped", "available": False}
    return {"status": manager.status, "available": manager.is_available}
