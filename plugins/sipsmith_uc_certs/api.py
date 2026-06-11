"""UC Cert Orchestrator REST API — mounted at /api/v1/plugins/uc-certs."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_uc_certs.models import (
    ItemStatus,
    PlanStatus,
    UcCertPlan,
    UcCertPlanItem,
    UcCluster,
    UcNode,
)

log = logging.getLogger("sipsmith.uc_certs.api")

router = APIRouter(prefix="/plugins/uc-certs", tags=["uc-certs"])


# ── Pydantic request/response models ─────────────────────────────────────────


class ClusterCreateRequest(BaseModel):
    name: str
    product: str
    version: str
    domains: list[str] = []
    toggles: dict = {}
    notes: str | None = None


class ClusterPatchRequest(BaseModel):
    name: str | None = None
    version: str | None = None
    domains: list[str] | None = None
    toggles: dict | None = None
    notes: str | None = None


class NodeCreateRequest(BaseModel):
    fqdn: str
    ip: str | None = None
    role: str = "node"


class CsrUploadRequest(BaseModel):
    csr_pem: str


# ── Clusters ──────────────────────────────────────────────────────────────────


@router.get("/clusters")
async def list_clusters(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(select(UcCluster).order_by(UcCluster.name))
    clusters = result.scalars().all()
    out = []
    for c in clusters:
        cnt = await db.scalar(func.count(UcNode.id).where(UcNode.cluster_id == c.id)) or 0
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "product": c.product,
                "version": c.version,
                "domains": json.loads(c.domains or "[]"),
                "toggles": json.loads(c.toggles or "{}"),
                "notes": c.notes,
                "node_count": cnt,
            }
        )
    return out


@router.post("/clusters", status_code=201)
async def create_cluster(
    body: ClusterCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.packs.loader import load_pack

    try:
        load_pack(body.product)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=422, detail=f"No knowledge pack for product {body.product!r}"
        ) from exc

    existing = await db.scalar(select(UcCluster.id).where(UcCluster.name == body.name))
    if existing:
        raise HTTPException(status_code=409, detail=f"Cluster {body.name!r} already exists")

    cluster = UcCluster(
        name=body.name,
        product=body.product,
        version=body.version,
        domains=json.dumps(body.domains),
        toggles=json.dumps(body.toggles),
        notes=body.notes,
    )
    db.add(cluster)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.create_cluster",
        resource=body.name,
        new_value={"product": body.product, "version": body.version},
    )
    await db.commit()
    return {"id": cluster.id, "name": cluster.name}


@router.get("/clusters/{cluster_id}")
async def get_cluster(
    cluster_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)
    nodes_result = await db.execute(
        select(UcNode).where(UcNode.cluster_id == cluster_id).order_by(UcNode.sort_order)
    )
    nodes = nodes_result.scalars().all()
    return {
        "id": c.id,
        "name": c.name,
        "product": c.product,
        "version": c.version,
        "domains": json.loads(c.domains or "[]"),
        "toggles": json.loads(c.toggles or "{}"),
        "notes": c.notes,
        "nodes": [{"id": n.id, "fqdn": n.fqdn, "ip": n.ip, "role": n.role} for n in nodes],
    }


@router.patch("/clusters/{cluster_id}")
async def patch_cluster(
    cluster_id: int,
    body: ClusterPatchRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)

    old: dict = {"version": c.version}
    if body.name is not None:
        c.name = body.name
    if body.version is not None:
        c.version = body.version
    if body.domains is not None:
        c.domains = json.dumps(body.domains)
    if body.toggles is not None:
        c.toggles = json.dumps(body.toggles)
    if body.notes is not None:
        c.notes = body.notes

    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.update_cluster",
        resource=c.name,
        old_value=old,
        new_value={"version": c.version},
    )
    await db.commit()
    return {"id": c.id, "name": c.name}


@router.delete("/clusters/{cluster_id}", status_code=204)
async def delete_cluster(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)
    name = c.name
    await db.delete(c)
    await AuditWriter(db).write(
        actor=user.username, action="uc_certs.delete_cluster", resource=name
    )
    await db.commit()


# ── Nodes ─────────────────────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/nodes", status_code=201)
async def add_node(
    cluster_id: int,
    body: NodeCreateRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)

    cnt = await db.scalar(func.count(UcNode.id).where(UcNode.cluster_id == cluster_id)) or 0
    node = UcNode(
        cluster_id=cluster_id,
        fqdn=body.fqdn.lower().strip(),
        ip=body.ip,
        role=body.role,
        sort_order=cnt,
    )
    db.add(node)
    await db.flush()
    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.add_node",
        resource=f"{c.name}/{body.fqdn}",
    )
    await db.commit()
    return {"id": node.id, "fqdn": node.fqdn}


@router.delete("/clusters/{cluster_id}/nodes/{node_id}", status_code=204)
async def delete_node(
    cluster_id: int,
    node_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    node = await db.get(UcNode, node_id)
    if node is None or node.cluster_id != cluster_id:
        raise HTTPException(status_code=404)
    c = await db.get(UcCluster, cluster_id)
    fqdn = node.fqdn
    await db.delete(node)
    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.delete_node",
        resource=f"{c.name if c else cluster_id}/{fqdn}",
    )
    await db.commit()


# ── Plans ─────────────────────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/plans", status_code=201)
async def create_plan(
    cluster_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.orchestrator import generate_plan

    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)

    node_cnt = await db.scalar(func.count(UcNode.id).where(UcNode.cluster_id == cluster_id)) or 0
    if node_cnt == 0:
        raise HTTPException(
            status_code=422, detail="Add at least one node before generating a plan"
        )

    try:
        plan = await generate_plan(cluster_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    item_cnt = (
        await db.scalar(func.count(UcCertPlanItem.id).where(UcCertPlanItem.plan_id == plan.id)) or 0
    )
    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.generate_plan",
        resource=c.name,
        new_value={"plan_id": plan.id, "items": item_cnt},
    )
    await db.commit()
    return {"id": plan.id, "item_count": item_cnt}


@router.get("/clusters/{cluster_id}/plans")
async def list_plans(
    cluster_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(
        select(UcCertPlan)
        .where(UcCertPlan.cluster_id == cluster_id)
        .order_by(UcCertPlan.generated_at.desc())
    )
    plans = result.scalars().all()
    out = []
    for p in plans:
        cnt = (
            await db.scalar(func.count(UcCertPlanItem.id).where(UcCertPlanItem.plan_id == p.id))
            or 0
        )
        out.append(
            {
                "id": p.id,
                "status": p.status,
                "generated_at": p.generated_at.isoformat(),
                "item_count": cnt,
            }
        )
    return out


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)

    items_result = await db.execute(
        select(UcCertPlanItem)
        .where(UcCertPlanItem.plan_id == plan_id)
        .order_by(UcCertPlanItem.restart_priority)
    )
    items = items_result.scalars().all()

    return {
        "id": plan.id,
        "cluster_id": plan.cluster_id,
        "status": plan.status,
        "dns_preflight": json.loads(plan.dns_preflight or "null"),
        "generated_at": plan.generated_at.isoformat(),
        "items": [
            {
                "id": i.id,
                "service_id": i.service_id,
                "label": i.label,
                "scope": i.scope,
                "node_id": i.node_id,
                "sans": json.loads(i.sans or "[]"),
                "eku": json.loads(i.eku or "[]"),
                "profile": i.profile,
                "trust_stores": json.loads(i.trust_stores or "[]"),
                "restart_priority": i.restart_priority,
                "service_note": i.service_note,
                "status": i.status,
                "csr_valid": i.csr_valid,
                "csr_errors": json.loads(i.csr_errors or "[]"),
                "has_csr": bool(i.csr_pem),
                "has_cert": bool(i.cert_pem),
            }
            for i in items
        ],
    }


@router.post("/plans/{plan_id}/dns-preflight")
async def dns_preflight(
    plan_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.orchestrator import run_dns_preflight

    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)

    try:
        report = await run_dns_preflight(plan_id, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.dns_preflight",
        resource=f"plan/{plan_id}",
        new_value={"status": report.get("status")},
    )
    await db.commit()
    return report


# ── CSR upload / signing ──────────────────────────────────────────────────────


@router.post("/plans/{plan_id}/items/{item_id}/upload-csr")
async def upload_csr(
    plan_id: int,
    item_id: int,
    body: CsrUploadRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.orchestrator import validate_csr

    item = await db.get(UcCertPlanItem, item_id)
    if item is None or item.plan_id != plan_id:
        raise HTTPException(status_code=404)

    item_sans = json.loads(item.sans or "[]")
    valid, errors = validate_csr(item_sans, body.csr_pem)

    item.csr_pem = body.csr_pem
    item.csr_valid = valid
    item.csr_errors = json.dumps(errors)
    item.status = ItemStatus.csr_uploaded if valid else ItemStatus.csr_invalid

    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.upload_csr",
        resource=f"plan/{plan_id}/item/{item_id}",
        new_value={"valid": valid, "errors": errors[:3]},
    )
    await db.commit()
    return {"valid": valid, "errors": errors}


@router.post("/plans/{plan_id}/items/{item_id}/sign")
async def sign_item(
    plan_id: int,
    item_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.orchestrator import sign_item as _sign

    item = await db.get(UcCertPlanItem, item_id)
    if item is None or item.plan_id != plan_id:
        raise HTTPException(status_code=404)
    if not item.csr_pem:
        raise HTTPException(status_code=422, detail="No CSR uploaded for this item")

    try:
        await _sign(item_id, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    await AuditWriter(db).write(
        actor=user.username,
        action="uc_certs.sign_item",
        resource=f"plan/{plan_id}/item/{item_id}/{item.service_id}",
    )
    await db.commit()
    return {"signed": True, "item_id": item_id}


@router.post("/plans/{plan_id}/sign-all")
async def sign_all(
    plan_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_uc_certs.orchestrator import sign_item as _sign

    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)

    items_result = await db.execute(
        select(UcCertPlanItem).where(
            UcCertPlanItem.plan_id == plan_id,
            UcCertPlanItem.status == ItemStatus.csr_uploaded,
            UcCertPlanItem.csr_pem.isnot(None),
        )
    )
    items = items_result.scalars().all()

    signed = 0
    errors = []
    for item in items:
        try:
            await _sign(item.id, db)
            signed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"item {item.id}: {exc}")

    if signed:
        plan.status = PlanStatus.signing
        await AuditWriter(db).write(
            actor=user.username,
            action="uc_certs.sign_all",
            resource=f"plan/{plan_id}",
            new_value={"signed": signed},
        )
        await db.commit()

    # Check if all items with CSRs are now signed → complete
    pending_result = await db.execute(
        select(UcCertPlanItem).where(
            UcCertPlanItem.plan_id == plan_id,
            UcCertPlanItem.csr_pem.isnot(None),
            UcCertPlanItem.status != ItemStatus.signed,
        )
    )
    if not pending_result.scalars().first():
        plan.status = PlanStatus.complete
        await db.commit()

    return {"signed": signed, "errors": errors}


# ── Cert download ─────────────────────────────────────────────────────────────


@router.get("/plans/{plan_id}/items/{item_id}/cert.pem")
async def download_cert(
    plan_id: int,
    item_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    item = await db.get(UcCertPlanItem, item_id)
    if item is None or item.plan_id != plan_id or not item.cert_pem:
        raise HTTPException(status_code=404)
    fname = f"{item.service_id}.pem"
    return Response(
        content=item.cert_pem,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@router.get("/plans/{plan_id}/delivery-bundle")
async def delivery_bundle(
    plan_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    from sipsmith_uc_certs.orchestrator import build_delivery_bundle

    plan = await db.get(UcCertPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)

    cluster = await db.get(UcCluster, plan.cluster_id)
    try:
        data = await build_delivery_bundle(plan_id, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    fname = f"{cluster.name if cluster else 'cluster'}-certs-plan{plan_id}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ── TLS probes ────────────────────────────────────────────────────────────────


@router.post("/clusters/{cluster_id}/tls-probes")
async def tls_probes(
    cluster_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    from sipsmith_uc_certs.orchestrator import run_tls_probes

    c = await db.get(UcCluster, cluster_id)
    if c is None:
        raise HTTPException(status_code=404)

    try:
        return await run_tls_probes(cluster_id, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── Pack catalog ──────────────────────────────────────────────────────────────


@router.get("/packs")
async def list_packs(
    _user: Annotated[User, Depends(get_current_user)],
) -> list[dict]:
    from sipsmith_uc_certs.packs.loader import list_packs as _list

    return _list()
