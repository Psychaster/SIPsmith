from __future__ import annotations

import datetime
import logging
import socket
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_ad.models import AdDomain, AdGroup, AdOU, AdSyncAccount, AdUser

log = logging.getLogger("sipsmith.ad.api")

router = APIRouter(prefix="/plugins/ad", tags=["ad"])


def _realm_to_base_dn(realm: str) -> str:
    return ",".join(f"DC={part}" for part in realm.upper().split("."))


def _user_dn(sam_account: str, realm: str) -> str:
    base = _realm_to_base_dn(realm)
    return f"CN={sam_account},CN=Users,{base}"


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class ProvisionRequest(BaseModel):
    realm: str
    netbios_name: str
    admin_password: str
    dsrm_password: str
    dns_mode: str = "dlz"


class UserCreate(BaseModel):
    sam_account: str
    password: str
    first_name: str = ""
    last_name: str = ""
    telephone_number: str = ""
    ip_phone: str = ""
    ou_dn: str = ""
    email: str = ""


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    ou_dn: str = ""


class OUCreate(BaseModel):
    name: str
    description: str = ""


class SyncAccountCreate(BaseModel):
    sam_account: str
    password: str
    display_name: str = ""


class BulkMode(StrEnum):
    pattern = "pattern"
    csv = "csv"


class PatternSpec(BaseModel):
    prefix: str
    start: int = 1
    count: int = 10
    phone_prefix: str = ""
    phone_start: int = 1000
    ou_dn: str = ""
    password: str


class BulkUserRequest(BaseModel):
    mode: BulkMode
    pattern: PatternSpec | None = None
    csv_content: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_domain(db: AsyncSession) -> AdDomain:
    row = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
    if row is None:
        raise HTTPException(status_code=404, detail="Domain not provisioned")
    return row


# ── Domain ────────────────────────────────────────────────────────────────────


@router.get("/domain")
async def get_domain(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    row = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
    if row is None:
        raise HTTPException(status_code=404, detail="Domain not provisioned")

    info: dict = {}
    if row.provisioned:
        from sipsmith.agent.client import AgentClient

        try:
            agent = AgentClient()
            info = await agent.ad_info()
        except Exception:  # noqa: BLE001
            info = {}

    return {
        "id": row.id,
        "realm": row.realm,
        "netbios_name": row.netbios_name,
        "dns_mode": row.dns_mode,
        "provisioned": row.provisioned,
        "provisioned_at": row.provisioned_at.isoformat() if row.provisioned_at else None,
        "ldaps_enabled": row.ldaps_enabled,
        "base_dn": _realm_to_base_dn(row.realm),
        "samba_info": info,
    }


@router.post("/domain/provision")
async def provision_domain(
    body: ProvisionRequest,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    agent = AgentClient()
    result = await agent.ad_provision(
        realm=body.realm,
        netbios=body.netbios_name,
        adminpass=body.admin_password,
        dsrm_pass=body.dsrm_password,
    )

    existing = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
    now = datetime.datetime.now(datetime.UTC)
    if existing:
        existing.realm = body.realm
        existing.netbios_name = body.netbios_name
        existing.dns_mode = body.dns_mode
        existing.provisioned = True
        existing.provisioned_at = now
    else:
        db.add(
            AdDomain(
                id=1,
                realm=body.realm,
                netbios_name=body.netbios_name,
                dns_mode=body.dns_mode,
                provisioned=True,
                provisioned_at=now,
            )
        )

    await AuditWriter(db).write(
        actor=user.username,
        action="ad.provision",
        resource=body.realm,
        new_value={"realm": body.realm, "netbios": body.netbios_name, "dns_mode": body.dns_mode},
    )
    await db.commit()

    if body.dns_mode == "dlz":
        try:
            await agent.ad_dlz_bind_include()
        except Exception:  # noqa: BLE001
            log.warning("ad_dlz_bind_include failed; DNS DLZ wiring may need manual setup")

    detail = result.get("detail", "provisioned") if isinstance(result, dict) else "provisioned"
    return {"ok": True, "detail": detail}


@router.delete("/domain")
async def deprovision_domain(
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    agent = AgentClient()
    await agent.ad_deprovision()

    await db.execute(delete(AdUser))
    await db.execute(delete(AdGroup))
    await db.execute(delete(AdOU))
    await db.execute(delete(AdSyncAccount))
    await db.execute(delete(AdDomain).where(AdDomain.id == 1))

    await AuditWriter(db).write(actor=user.username, action="ad.deprovision", resource="domain")
    await db.commit()
    return {"ok": True}


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def ad_status(
    _user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from sipsmith.agent.client import AgentClient, AgentError

    agent = AgentClient()
    info: dict = {}
    smbd_ok = False
    winbind_ok = False
    try:
        info = await agent.ad_info()
    except AgentError:
        info = {"error": "agent unavailable"}
    try:
        r = await agent.systemctl("status", "samba-ad-dc")
        smbd_ok = r.get("ok", False)
        winbind_ok = smbd_ok
    except AgentError:
        pass
    return {"samba_info": info, "smbd_ok": smbd_ok, "winbind_ok": winbind_ok}


# ── Users ─────────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    stmt = select(AdUser).order_by(AdUser.sam_account)
    count_stmt = select(func.count(AdUser.id))
    if q:
        like = f"%{q}%"
        condition = or_(
            AdUser.sam_account.ilike(like),
            AdUser.display_name.ilike(like),
            AdUser.telephone_number.ilike(like),
        )
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = await db.scalar(count_stmt)
    result = await db.execute(stmt.offset(offset).limit(limit))
    users = result.scalars().all()
    return {
        "total": total or 0,
        "users": [
            {
                "id": u.id,
                "sam_account": u.sam_account,
                "display_name": u.display_name,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "email": u.email,
                "telephone_number": u.telephone_number,
                "ip_phone": u.ip_phone,
                "ou_dn": u.ou_dn,
                "enabled": u.enabled,
            }
            for u in users
        ],
    }


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    domain = await _get_domain(db)
    agent = AgentClient()

    await agent.ad_user_create(
        sam_account=body.sam_account,
        password=body.password,
        given_name=body.first_name,
        surname=body.last_name,
        telephone_number=body.telephone_number,
        ou=body.ou_dn,
    )

    if body.ip_phone:
        dn = _user_dn(body.sam_account, domain.realm)
        try:
            await agent.ad_ldb_set_attr(dn=dn, attr="ipPhone", value=body.ip_phone)
        except Exception:  # noqa: BLE001
            log.warning("Failed to set ipPhone for %s", body.sam_account)

    display_name = f"{body.first_name} {body.last_name}".strip() or body.sam_account
    row = AdUser(
        sam_account=body.sam_account,
        display_name=display_name,
        first_name=body.first_name or None,
        last_name=body.last_name or None,
        email=body.email or None,
        telephone_number=body.telephone_number or None,
        ip_phone=body.ip_phone or None,
        ou_dn=body.ou_dn or None,
    )
    db.add(row)
    await AuditWriter(db).write(
        actor=user.username,
        action="ad.user_create",
        resource=body.sam_account,
        new_value={"sam_account": body.sam_account, "ou_dn": body.ou_dn},
    )
    await db.commit()
    await db.refresh(row)
    return {
        "id": row.id,
        "sam_account": row.sam_account,
        "display_name": row.display_name,
        "telephone_number": row.telephone_number,
        "ip_phone": row.ip_phone,
    }


@router.delete("/users/{sam_account}", status_code=204)
async def delete_user(
    sam_account: str,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    row = await db.scalar(select(AdUser).where(AdUser.sam_account == sam_account))
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    agent = AgentClient()
    await agent.ad_user_delete(sam_account)

    await db.delete(row)
    await AuditWriter(db).write(actor=user.username, action="ad.user_delete", resource=sam_account)
    await db.commit()


@router.post("/users/bulk")
async def bulk_create_users(
    body: BulkUserRequest,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    import csv
    import io

    from sipsmith.agent.client import AgentClient

    domain = await _get_domain(db)
    agent = AgentClient()

    users_to_create: list[dict] = []

    if body.mode == BulkMode.pattern:
        if body.pattern is None:
            raise HTTPException(status_code=422, detail="pattern required for mode=pattern")
        spec = body.pattern
        for i in range(spec.count):
            n = spec.start + i
            sam = f"{spec.prefix}{n:03d}"
            phone = f"{spec.phone_prefix}{spec.phone_start + i}" if spec.phone_prefix else ""
            users_to_create.append(
                {
                    "sam_account": sam,
                    "password": spec.password,
                    "first_name": spec.prefix,
                    "last_name": f"{n:03d}",
                    "display_name": sam,
                    "telephone_number": phone,
                    "ip_phone": phone,
                    "ou_dn": spec.ou_dn,
                }
            )
    elif body.mode == BulkMode.csv:
        if not body.csv_content:
            raise HTTPException(status_code=422, detail="csv_content required for mode=csv")
        reader = csv.DictReader(io.StringIO(body.csv_content))
        for row_data in reader:
            sam = (row_data.get("sam_account") or "").strip()
            if not sam:
                continue
            users_to_create.append(
                {
                    "sam_account": sam,
                    "password": (row_data.get("password") or "").strip(),
                    "first_name": (row_data.get("first_name") or "").strip(),
                    "last_name": (row_data.get("last_name") or "").strip(),
                    "display_name": (row_data.get("display_name") or sam).strip(),
                    "telephone_number": (row_data.get("telephone_number") or "").strip(),
                    "ip_phone": (row_data.get("ip_phone") or "").strip(),
                    "ou_dn": (row_data.get("ou_dn") or "").strip(),
                }
            )

    created = 0
    failed = 0
    errors: list[str] = []

    for u in users_to_create:
        try:
            await agent.ad_user_create(
                sam_account=u["sam_account"],
                password=u["password"],
                given_name=u["first_name"],
                surname=u["last_name"],
                telephone_number=u["telephone_number"],
                ou=u["ou_dn"],
            )
            if u.get("ip_phone"):
                dn = _user_dn(u["sam_account"], domain.realm)
                try:
                    await agent.ad_ldb_set_attr(dn=dn, attr="ipPhone", value=u["ip_phone"])
                except Exception:  # noqa: BLE001
                    log.debug("ipPhone set failed for %s", u["sam_account"])

            db.add(
                AdUser(
                    sam_account=u["sam_account"],
                    display_name=u["display_name"],
                    first_name=u["first_name"] or None,
                    last_name=u["last_name"] or None,
                    telephone_number=u["telephone_number"] or None,
                    ip_phone=u["ip_phone"] or None,
                    ou_dn=u["ou_dn"] or None,
                )
            )
            created += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"{u['sam_account']}: {exc}")

    if created:
        await AuditWriter(db).write(
            actor=user.username,
            action="ad.bulk_user_create",
            resource="users",
            new_value={"created": created, "failed": failed},
        )
        await db.commit()

    return {"created": created, "failed": failed, "errors": errors}


# ── Groups ────────────────────────────────────────────────────────────────────


@router.get("/groups")
async def list_groups(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(select(AdGroup).order_by(AdGroup.name))
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "ou_dn": g.ou_dn,
        }
        for g in result.scalars().all()
    ]


@router.post("/groups", status_code=201)
async def create_group(
    body: GroupCreate,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    agent = AgentClient()
    await agent.ad_group_create(
        name=body.name,
        description=body.description,
        ou=body.ou_dn,
    )

    row = AdGroup(name=body.name, description=body.description or None, ou_dn=body.ou_dn or None)
    db.add(row)
    await AuditWriter(db).write(
        actor=user.username,
        action="ad.group_create",
        resource=body.name,
        new_value={"name": body.name, "ou_dn": body.ou_dn},
    )
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/groups/{name}", status_code=204)
async def delete_group(
    name: str,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    row = await db.scalar(select(AdGroup).where(AdGroup.name == name))
    if row is None:
        raise HTTPException(status_code=404, detail="Group not found")

    agent = AgentClient()
    await agent.ad_group_delete(name)

    await db.delete(row)
    await AuditWriter(db).write(actor=user.username, action="ad.group_delete", resource=name)
    await db.commit()


# ── OUs ───────────────────────────────────────────────────────────────────────


@router.get("/ous")
async def list_ous(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict]:
    result = await db.execute(select(AdOU).order_by(AdOU.name))
    return [
        {
            "id": o.id,
            "name": o.name,
            "dn": o.dn,
            "description": o.description,
        }
        for o in result.scalars().all()
    ]


@router.post("/ous", status_code=201)
async def create_ou(
    body: OUCreate,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    domain = await _get_domain(db)
    base_dn = _realm_to_base_dn(domain.realm)
    dn = f"OU={body.name},{base_dn}"

    agent = AgentClient()
    await agent.ad_ou_create(dn=dn, description=body.description)

    row = AdOU(name=body.name, dn=dn, description=body.description or None)
    db.add(row)
    await AuditWriter(db).write(
        actor=user.username,
        action="ad.ou_create",
        resource=dn,
        new_value={"name": body.name, "dn": dn},
    )
    await db.commit()
    await db.refresh(row)
    return {"id": row.id, "name": row.name, "dn": row.dn}


@router.delete("/ous/{ou_id}", status_code=204)
async def delete_ou(
    ou_id: int,
    user: Annotated[User, Depends(require_role(Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    from sipsmith.agent.client import AgentClient

    row = await db.get(AdOU, ou_id)
    if row is None:
        raise HTTPException(status_code=404, detail="OU not found")

    agent = AgentClient()
    await agent.ad_ou_delete(row.dn)

    await db.delete(row)
    await AuditWriter(db).write(actor=user.username, action="ad.ou_delete", resource=row.dn)
    await db.commit()


# ── Sync accounts ─────────────────────────────────────────────────────────────


@router.post("/sync-account", status_code=201)
async def create_sync_account(
    body: SyncAccountCreate,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.agent.client import AgentClient

    domain = await _get_domain(db)
    agent = AgentClient()

    display = body.display_name or body.sam_account
    await agent.ad_user_create(
        sam_account=body.sam_account,
        password=body.password,
        given_name=display,
        surname="",
    )

    dn = _user_dn(body.sam_account, domain.realm)
    try:
        await agent.ad_ldb_set_attr(dn=dn, attr="userAccountControl", value="66048")
    except Exception:  # noqa: BLE001
        log.debug("userAccountControl set failed for %s", body.sam_account)

    row = AdSyncAccount(
        sam_account=body.sam_account,
        display_name=display or None,
        purpose="cucm_sync",
    )
    db.add(row)
    await AuditWriter(db).write(
        actor=user.username,
        action="ad.sync_account_create",
        resource=body.sam_account,
    )
    await db.commit()
    await db.refresh(row)

    base_dn = _realm_to_base_dn(domain.realm)
    realm_lower = domain.realm.lower()
    return {
        "sam_account": row.sam_account,
        "search_base": base_dn,
        "ldap_url": f"ldap://{realm_lower}",
        "ldaps_url": f"ldaps://{realm_lower}",
    }


@router.get("/sync-helper")
async def sync_helper(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith.config import get_settings

    domain = await _get_domain(db)

    try:
        appliance_ip = get_settings().server.host
        if appliance_ip in ("0.0.0.0", ""):  # noqa: S104
            appliance_ip = socket.gethostbyname(socket.getfqdn())
    except Exception:  # noqa: BLE001
        appliance_ip = "127.0.0.1"

    result = await db.execute(select(AdSyncAccount).order_by(AdSyncAccount.sam_account))
    sync_accounts = [
        {"sam_account": s.sam_account, "display_name": s.display_name, "purpose": s.purpose}
        for s in result.scalars().all()
    ]

    base_dn = _realm_to_base_dn(domain.realm)
    trust_callout = (
        f"Import the SIPsmith CA certificate into CUCM Trust Store before enabling LDAPS.\n"
        f"Download from: https://{appliance_ip}:8443/api/v1/plugins/ca/root.pem"  # noqa
    )

    return {
        "ldap_server": appliance_ip,
        "ldap_port": 389,
        "ldaps_port": 636,
        "base_dn": base_dn,
        "sync_accounts": sync_accounts,
        "ldaps_enabled": domain.ldaps_enabled,
        "trust_callout": trust_callout,
    }


# ── LDAPS cert issuance ───────────────────────────────────────────────────────


@router.post("/ldaps/issue-cert", status_code=201)
async def issue_ldaps_cert(
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    import httpx

    from sipsmith.agent.client import AgentClient
    from sipsmith.config import get_settings

    domain = await _get_domain(db)
    settings = get_settings()
    base_url = f"https://127.0.0.1:{settings.server.port}"
    realm_lower = domain.realm.lower()

    sans = [f"DNS:{realm_lower}", f"DNS:*.{realm_lower}"]

    async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
        resp = await client.post(
            f"{base_url}/api/v1/plugins/ca/issue",
            json={
                "subject_cn": realm_lower,
                "sans": sans,
                "profile": "server",
                "key_type": "rsa2048",
            },
            headers={"X-Internal-Token": settings.security.secret_key},
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(
            status_code=502, detail=f"CA issue failed: {resp.status_code} {resp.text[:200]}"
        )

    data = resp.json()
    cert_pem: str = data["cert_pem"]
    key_pem: str = data["key_pem"]

    ca_pem = ""
    try:
        async with httpx.AsyncClient(verify=False) as client:  # noqa: S501
            ca_resp = await client.get(f"{base_url}/api/v1/plugins/ca/chain.pem")
        if ca_resp.status_code == 200:
            ca_pem = ca_resp.text
    except Exception:  # noqa: BLE001
        log.debug("Could not fetch CA chain PEM")

    agent = AgentClient()
    await agent.ad_set_ldaps_cert(cert_pem=cert_pem, key_pem=key_pem, ca_pem=ca_pem)

    row = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
    if row:
        row.ldaps_enabled = True

    await AuditWriter(db).write(
        actor=user.username,
        action="ad.ldaps_cert_issued",
        resource=realm_lower,
        new_value={"serial_hex": data.get("serial_hex")},
    )
    await db.commit()
    return {"ok": True, "serial_hex": data.get("serial_hex"), "cn": realm_lower}
