"""CTI (JTAPI) plugin REST API — mounted at /plugins/cti."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import AsyncSessionLocal, get_db
from sipsmith.models.user import Role, User
from sipsmith_cti.axl import AXLClient
from sipsmith_cti.manager import get_all_managers, get_manager, start_manager, stop_manager
from sipsmith_cti.models import CtiCall, CtiCluster, CtiDevice, CtiEvent

log = logging.getLogger("sipsmith.cti.api")

router = APIRouter(prefix="/plugins/cti", tags=["cti"])

_OPERATOR_ROLES = (Role.operator, Role.admin)
_JTAPI_DIR = "/var/lib/sipsmith/cti"

# ── Pydantic schemas ──────────────────────────────────────────────────────────


class ClusterCreate(BaseModel):
    name: str
    cucm_host: str
    cucm_version: str | None = None
    axl_user: str | None = None
    axl_password: str | None = None
    app_user: str | None = None
    app_password: str | None = None


class ClusterUpdate(BaseModel):
    name: str | None = None
    cucm_host: str | None = None
    cucm_version: str | None = None
    axl_user: str | None = None
    axl_password: str | None = None
    app_user: str | None = None
    app_password: str | None = None


class CallRequest(BaseModel):
    cluster_id: int
    calling_device: str
    called_dn: str


class TransferRequest(BaseModel):
    destination: str


class DtmfRequest(BaseModel):
    digits: str


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cluster_to_dict(c: CtiCluster) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "cucm_host": c.cucm_host,
        "cucm_version": c.cucm_version,
        "axl_user": c.axl_user,
        "app_user": c.app_user,
        "jtapi_jar_path": c.jtapi_jar_path,
        "status": c.status,
        "last_connected_at": (c.last_connected_at.isoformat() if c.last_connected_at else None),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _device_to_dict(d: CtiDevice) -> dict[str, Any]:
    return {
        "id": d.id,
        "cluster_id": d.cluster_id,
        "device_name": d.device_name,
        "directory_number": d.directory_number,
        "description": d.description,
        "reg_state": d.reg_state,
        "active_call_id": d.active_call_id,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _sidecar_unavailable(cluster_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"CTI sidecar for cluster {cluster_id} is not connected",
    )


# ── Cluster CRUD ──────────────────────────────────────────────────────────────


@router.get("/clusters")
async def list_clusters(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(select(CtiCluster).order_by(CtiCluster.name))
    return [_cluster_to_dict(c) for c in result.scalars().all()]


@router.post("/clusters", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    body: ClusterCreate,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = CtiCluster(
        name=body.name,
        cucm_host=body.cucm_host,
        cucm_version=body.cucm_version,
        axl_user=body.axl_user,
        axl_password=body.axl_password,
        app_user=body.app_user,
        app_password=body.app_password,
    )
    db.add(cluster)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.create",
        resource=f"cluster:{cluster.id}",
        new_value=body.name,
    )
    await db.commit()
    return {"id": cluster.id}


@router.get("/clusters/{cluster_id}")
async def get_cluster(
    cluster_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return _cluster_to_dict(cluster)


@router.put("/clusters/{cluster_id}")
async def update_cluster(
    cluster_id: int,
    body: ClusterUpdate,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    old_name = cluster.name
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(cluster, field, val)
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.update",
        resource=f"cluster:{cluster_id}",
        old_value=old_name,
        new_value=cluster.name,
    )
    await db.commit()
    return _cluster_to_dict(cluster)


@router.delete("/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cluster(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    await stop_manager(cluster_id)
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.delete",
        resource=f"cluster:{cluster_id}",
        old_value=cluster.name,
    )
    await db.delete(cluster)
    await db.commit()


# ── Connection management ─────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/connect")
async def connect_cluster(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not cluster.app_user or not cluster.app_password:
        raise HTTPException(
            status_code=400,
            detail="app_user and app_password must be set before connecting",
        )

    mgr = await start_manager(
        cluster_id=cluster_id,
        cucm_host=cluster.cucm_host,
        cucm_version=cluster.cucm_version or "14.0",
        app_user=cluster.app_user,
        app_password=cluster.app_password,
    )
    cluster.status = mgr.status
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.connect",
        resource=f"cluster:{cluster_id}",
    )
    await db.commit()
    return {"status": mgr.status}


@router.post("/clusters/{cluster_id}/disconnect")
async def disconnect_cluster(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    await stop_manager(cluster_id)
    cluster.status = "disconnected"
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.disconnect",
        resource=f"cluster:{cluster_id}",
    )
    await db.commit()
    return {"status": "disconnected"}


# ── JTAPI jar download ────────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/fetch-jtapi-jar")
async def fetch_jtapi_jar(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")

    version = cluster.cucm_version or "unknown"
    url = f"http://{cluster.cucm_host}:8080/plugins/jtapi.jar"

    dest_dir = Path(_JTAPI_DIR) / str(cluster_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"jtapi-{version}.jar"

    try:
        async with httpx.AsyncClient(verify=False, timeout=60) as client:  # noqa: S501
            r = await client.get(url)
            r.raise_for_status()
            dest_path.write_bytes(r.content)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"CUCM returned HTTP {exc.response.status_code} for {url}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch JTAPI jar: {exc}",
        ) from exc

    cluster.jtapi_jar_path = str(dest_path)
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.fetch_jtapi_jar",
        resource=f"cluster:{cluster_id}",
        new_value=str(dest_path),
    )
    await db.commit()
    return {"ok": True, "path": str(dest_path), "size_bytes": dest_path.stat().st_size}


# ── Connectivity check ────────────────────────────────────────────────────────


@router.get("/clusters/{cluster_id}/check-connectivity")
async def check_connectivity(
    cluster_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")

    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cluster.cucm_host, 2748), timeout=5.0
        )
        latency_ms = (time.monotonic() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001, S110
            pass
        return {"ok": True, "latency_ms": round(latency_ms, 2)}
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.monotonic() - t0) * 1000
        return {"ok": False, "latency_ms": round(latency_ms, 2), "error": str(exc)}


# ── AXL setup ─────────────────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/axl-setup")
async def axl_setup(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    cluster = await db.get(CtiCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Cluster not found")
    if not cluster.axl_user or not cluster.axl_password:
        raise HTTPException(
            status_code=400,
            detail="axl_user and axl_password must be configured",
        )
    if not cluster.app_user or not cluster.app_password:
        raise HTTPException(
            status_code=400,
            detail="app_user and app_password must be configured",
        )

    axl = AXLClient(
        host=cluster.cucm_host,
        user=cluster.axl_user,
        password=cluster.axl_password,
        version=cluster.cucm_version or "14.0",
    )

    # Check whether the app user already exists; create it if not.
    check = await axl.get_app_user(cluster.app_user)
    results: dict[str, Any] = {"get_app_user": check["status_code"]}
    if check["status_code"] == 404:
        add = await axl.add_app_user(cluster.app_user, cluster.app_password, [])
        results["add_app_user"] = add["status_code"]

    cti = await axl.update_app_user_cti(cluster.app_user)
    results["update_app_user_cti"] = cti["status_code"]

    await AuditWriter(db).write(
        actor=user.username,
        action="cti.cluster.axl_setup",
        resource=f"cluster:{cluster_id}",
    )
    return {"ok": True, "results": results}


# ── Devices ───────────────────────────────────────────────────────────────────


@router.get("/clusters/{cluster_id}/devices")
async def list_cluster_devices(
    cluster_id: int,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(CtiDevice).where(CtiDevice.cluster_id == cluster_id).order_by(CtiDevice.device_name)
    )
    devices = [_device_to_dict(d) for d in result.scalars().all()]

    mgr = get_manager(cluster_id)
    if mgr and mgr.is_available:
        try:
            await mgr.send_command("listDevices", {})
        except Exception:  # noqa: BLE001
            log.debug("Could not send listDevices to cluster %d", cluster_id)

    return devices


@router.get("/devices")
async def list_all_devices(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(CtiDevice).order_by(CtiDevice.cluster_id, CtiDevice.device_name)
    )
    return [_device_to_dict(d) for d in result.scalars().all()]


@router.post("/devices/{device_name}/observe")
async def observe_device(
    device_name: str,
    cluster_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
) -> dict[str, Any]:
    mgr = get_manager(cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(cluster_id)
    await mgr.send_command("observe", {"device": device_name})
    return {"status": "observe_sent", "device": device_name}


# ── Calls ─────────────────────────────────────────────────────────────────────


@router.post("/calls", status_code=status.HTTP_201_CREATED)
async def make_call(
    body: CallRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    mgr = get_manager(body.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(body.cluster_id)

    call = CtiCall(
        cluster_id=body.cluster_id,
        calling_device=body.calling_device,
        called_dn=body.called_dn,
        call_state="calling",
        started_at=datetime.datetime.now(datetime.UTC),
    )
    db.add(call)
    await db.flush()
    call_id = call.id

    await mgr.send_command(
        "makeCall",
        {
            "device": body.calling_device,
            "destination": body.called_dn,
            "call_id": call_id,
        },
    )
    await AuditWriter(db).write(
        actor=user.username,
        action="cti.call.make",
        resource=f"call:{call_id}",
        new_value=f"{body.calling_device} -> {body.called_dn}",
    )
    await db.commit()
    return {"id": call_id}


async def _get_call_or_404(call_id: int, db: AsyncSession) -> CtiCall:
    call = await db.get(CtiCall, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.post("/calls/{call_id}/hangup")
async def hangup_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await _get_call_or_404(call_id, db)
    mgr = get_manager(call.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(call.cluster_id)
    await mgr.send_command("hangup", {"call_id": call_id})
    return {"status": "hangup_sent"}


@router.post("/calls/{call_id}/hold")
async def hold_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await _get_call_or_404(call_id, db)
    mgr = get_manager(call.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(call.cluster_id)
    await mgr.send_command("hold", {"call_id": call_id})
    return {"status": "hold_sent"}


@router.post("/calls/{call_id}/resume")
async def resume_call(
    call_id: int,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await _get_call_or_404(call_id, db)
    mgr = get_manager(call.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(call.cluster_id)
    await mgr.send_command("resume", {"call_id": call_id})
    return {"status": "resume_sent"}


@router.post("/calls/{call_id}/transfer")
async def transfer_call(
    call_id: int,
    body: TransferRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await _get_call_or_404(call_id, db)
    mgr = get_manager(call.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(call.cluster_id)
    await mgr.send_command("transfer", {"call_id": call_id, "destination": body.destination})
    return {"status": "transfer_sent"}


@router.post("/calls/{call_id}/dtmf")
async def dtmf_call(
    call_id: int,
    body: DtmfRequest,
    user: Annotated[User, Depends(require_role(*_OPERATOR_ROLES))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    call = await _get_call_or_404(call_id, db)
    mgr = get_manager(call.cluster_id)
    if mgr is None or not mgr.is_available:
        raise _sidecar_unavailable(call.cluster_id)
    await mgr.send_command("sendDTMF", {"call_id": call_id, "digits": body.digits})
    return {"status": "dtmf_sent"}


# ── SSE event stream ──────────────────────────────────────────────────────────


@router.get("/events/stream")
async def events_stream(
    user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    """Server-Sent Events stream: polls CtiEvent rows every second."""

    async def _generator():  # type: ignore[return]
        yield "retry: 2000\n\n"
        last_keepalive = asyncio.get_event_loop().time()
        seen_id: int | None = None

        while True:
            try:
                async with AsyncSessionLocal() as db:
                    stmt = select(CtiEvent).order_by(CtiEvent.ts.desc()).limit(20)
                    result = await db.execute(stmt)
                    rows = list(reversed(result.scalars().all()))

                for row in rows:
                    if seen_id is None or row.id > seen_id:
                        seen_id = row.id
                        payload = {
                            "id": row.id,
                            "cluster_id": row.cluster_id,
                            "device_name": row.device_name,
                            "event_type": row.event_type,
                            "payload": row.payload,
                            "ts": row.ts.isoformat(),
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                        last_keepalive = asyncio.get_event_loop().time()
            except Exception:  # noqa: BLE001, S110
                pass

            now = asyncio.get_event_loop().time()
            if now - last_keepalive > 15:
                yield ": keepalive\n\n"
                last_keepalive = now

            await asyncio.sleep(1.0)

    return StreamingResponse(_generator(), media_type="text/event-stream")


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def plugin_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    result = await db.execute(select(CtiCluster).order_by(CtiCluster.name))
    clusters = result.scalars().all()

    out: list[dict[str, Any]] = []
    for c in clusters:
        device_count = (
            await db.scalar(select(func.count(CtiDevice.id)).where(CtiDevice.cluster_id == c.id))
            or 0
        )
        mgr = get_manager(c.id)
        live_status = mgr.status if mgr is not None else c.status
        out.append(
            {
                "cluster_id": c.id,
                "name": c.name,
                "host": c.cucm_host,
                "status": live_status,
                "device_count": device_count,
            }
        )
    return out


# ── All-cluster status (managers only) ───────────────────────────────────────


@router.get("/workers")
async def worker_status(
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    managers = get_all_managers()
    return {
        str(cid): {"status": mgr.status, "available": mgr.is_available}
        for cid, mgr in managers.items()
    }
