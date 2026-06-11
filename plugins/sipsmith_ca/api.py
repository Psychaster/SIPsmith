"""CA plugin REST API — mounted at /api/v1/plugins/ca."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.config import get_settings
from sipsmith.core.audit import AuditWriter
from sipsmith.database import get_db
from sipsmith.models.user import Role, User
from sipsmith_ca.models import CaCertificate, CertProfile, RevocationReason

log = logging.getLogger("sipsmith.ca.api")

router = APIRouter(prefix="/plugins/ca", tags=["ca"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_secret() -> str:
    return get_settings().security.secret_key


def _require_initialized() -> None:
    from sipsmith_ca.crypto import is_initialized

    if not is_initialized():
        raise HTTPException(status_code=503, detail="CA not initialized — run /setup first")


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class CASetupRequest(BaseModel):
    root_cn: str = "SIPsmith Root CA"
    issuing_cn: str = "SIPsmith Issuing CA"
    key_type: str = "rsa4096"


class SignCSRRequest(BaseModel):
    csr_pem: str
    profile: CertProfile = CertProfile.server_client
    extra_sans: list[str] = []
    validity_days: int | None = None


class IssueKeyRequest(BaseModel):
    subject_cn: str
    sans: list[str] = []
    profile: CertProfile = CertProfile.server_client
    key_type: str = "rsa2048"
    validity_days: int | None = None


class RevokeRequest(BaseModel):
    reason: RevocationReason = RevocationReason.unspecified


class CertOut(BaseModel):
    id: int
    serial_hex: str
    subject_cn: str
    subject_dn: str
    san_json: str
    not_before: str
    not_after: str
    profile: str
    is_ca: bool
    is_lab_key: bool
    revoked: bool
    revoke_reason: RevocationReason | None
    issued_at: str
    issued_by: str | None

    model_config = {"from_attributes": True}


class CertDetailOut(CertOut):
    cert_pem: str


# ── Setup / status ────────────────────────────────────────────────────────────


@router.get("/status")
async def ca_status(
    _user: Annotated[User, Depends(get_current_user)],
) -> dict:
    from sipsmith_ca.crypto import (
        is_initialized,
        load_issuing_cert,
        load_root_cert,
    )

    if not is_initialized():
        return {"initialized": False}

    root = load_root_cert()
    issuing = load_issuing_cert()
    return {
        "initialized": True,
        "root": {
            "subject": root.subject.rfc4514_string(),
            "not_after": root.not_valid_after_utc.isoformat(),
            "serial": hex(root.serial_number),
        },
        "issuing": {
            "subject": issuing.subject.rfc4514_string(),
            "not_after": issuing.not_valid_after_utc.isoformat(),
            "serial": hex(issuing.serial_number),
        },
    }


@router.post("/setup", status_code=201)
async def setup_ca(
    body: CASetupRequest,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    from sipsmith_ca.crypto import generate_ca, is_initialized

    if is_initialized():
        raise HTTPException(status_code=409, detail="CA already initialized")

    settings = get_settings()
    fqdn = settings.server.fqdn

    root_cert, issuing_cert = generate_ca(
        secret_key=_get_secret(),
        root_cn=body.root_cn,
        issuing_cn=body.issuing_cn,
        key_type=body.key_type,
        fqdn=fqdn,
    )

    # Record CA certs in DB
    from cryptography.hazmat.primitives.serialization import Encoding as _Enc
    from cryptography.x509.oid import NameOID as _NameOID

    from sipsmith_ca.crypto import sans_from_cert

    for cert, profile, _is_root in [
        (root_cert, "root_ca", True),
        (issuing_cert, "issuing_ca", True),
    ]:
        row = CaCertificate(
            serial_hex=hex(cert.serial_number),
            subject_cn=cert.subject.get_attributes_for_oid(_NameOID.COMMON_NAME)[0].value,
            subject_dn=cert.subject.rfc4514_string(),
            san_json=json.dumps(sans_from_cert(cert)),
            not_before=cert.not_valid_before_utc,
            not_after=cert.not_valid_after_utc,
            cert_pem=cert.public_bytes(_Enc.PEM).decode(),
            profile=profile,
            is_ca=True,
            issued_by=user.username,
        )
        db.add(row)

    await db.commit()
    await AuditWriter(db).write(
        actor=user.username, action="ca.setup", resource="ca", new_value={"key_type": body.key_type}
    )
    log.info("CA initialized by %s", user.username)
    return {"ok": True, "root_cn": body.root_cn, "issuing_cn": body.issuing_cn}


# ── Certificate download ──────────────────────────────────────────────────────


@router.get("/root.pem", response_class=PlainTextResponse)
async def download_root_pem(_user: Annotated[User, Depends(get_current_user)]) -> str:
    _require_initialized()
    from cryptography.hazmat.primitives.serialization import Encoding

    from sipsmith_ca.crypto import load_root_cert

    return load_root_cert().public_bytes(Encoding.PEM).decode()


@router.get("/root.der")
async def download_root_der(_user: Annotated[User, Depends(get_current_user)]) -> Response:
    _require_initialized()
    from cryptography.hazmat.primitives.serialization import Encoding

    from sipsmith_ca.crypto import load_root_cert

    return Response(
        content=load_root_cert().public_bytes(Encoding.DER),
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": "attachment; filename=sipsmith-root-ca.der"},
    )


@router.get("/chain.pem", response_class=PlainTextResponse)
async def download_chain_pem(_user: Annotated[User, Depends(get_current_user)]) -> str:
    _require_initialized()
    from cryptography.hazmat.primitives.serialization import Encoding

    from sipsmith_ca.crypto import load_issuing_cert, load_root_cert

    chain = (
        load_issuing_cert().public_bytes(Encoding.PEM).decode()
        + load_root_cert().public_bytes(Encoding.PEM).decode()
    )
    return chain


@router.get("/chain.p7b")
async def download_chain_p7b(_user: Annotated[User, Depends(get_current_user)]) -> Response:
    """PKCS#7 bundle for trust store import."""
    _require_initialized()
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.hazmat.primitives.serialization.pkcs7 import serialize_certificates

    from sipsmith_ca.crypto import load_issuing_cert, load_root_cert

    data = serialize_certificates([load_issuing_cert(), load_root_cert()], Encoding.DER)
    return Response(
        content=data,
        media_type="application/x-pkcs7-certificates",
        headers={"Content-Disposition": "attachment; filename=sipsmith-ca-chain.p7b"},
    )


# ── CRL ───────────────────────────────────────────────────────────────────────


@router.get("/crl")
async def get_crl(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """HTTP CRL distribution point (unauthenticated — clients fetch it directly)."""
    _require_initialized()
    from sipsmith_ca.crypto import generate_crl, load_issuing_ca

    revoked = await db.execute(
        select(CaCertificate).where(CaCertificate.revoked, ~CaCertificate.is_ca)
    )
    revoked_list = [
        {
            "serial_hex": c.serial_hex,
            "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
            "reason": str(c.revoke_reason) if c.revoke_reason else "unspecified",
        }
        for c in revoked.scalars().all()
    ]
    issuing_key, issuing_cert = load_issuing_ca(_get_secret())
    crl_der = generate_crl(issuing_key, issuing_cert, revoked_list)
    return Response(
        content=crl_der,
        media_type="application/pkix-crl",
        headers={"Content-Disposition": "attachment; filename=issuing.crl"},
    )


# ── OCSP responder ────────────────────────────────────────────────────────────


@router.post("/ocsp")
@router.get("/ocsp")
async def ocsp_responder(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """OCSP responder (RFC 6960). GET with base64-encoded request or POST with DER body."""
    _require_initialized()
    import base64

    from cryptography import x509

    from sipsmith_ca.crypto import load_issuing_ca, ocsp_response_bytes

    body = await request.body()
    if not body and request.method == "GET":
        # GET requests encode OCSP request in path or query — return error
        from cryptography.x509.ocsp import OCSPResponseBuilder, OCSPResponseStatus

        return Response(
            content=OCSPResponseBuilder.build_unsuccessful(
                OCSPResponseStatus.MALFORMED_REQUEST
            ).public_bytes(
                __import__(
                    "cryptography.hazmat.primitives.serialization",
                    fromlist=["Encoding"],
                ).Encoding.DER
            ),
            media_type="application/ocsp-response",
        )

    # Try to handle GET-style (base64 in query)
    if request.method == "GET":
        import urllib.parse

        path = str(request.url.path)
        parts = path.split("/ocsp/", 1)
        if len(parts) == 2:
            body = base64.b64decode(urllib.parse.unquote(parts[1]))

    # Fetch cert + revocation data from DB
    certs_result = await db.execute(
        select(CaCertificate).where(~CaCertificate.revoked, ~CaCertificate.is_ca)
    )
    cert_lookup: dict[int, x509.Certificate] = {}
    for row in certs_result.scalars().all():
        try:
            c = x509.load_pem_x509_certificate(row.cert_pem.encode())
            cert_lookup[c.serial_number] = c
        except Exception:  # noqa: BLE001, S110
            pass

    revoked_result = await db.execute(
        select(CaCertificate).where(CaCertificate.revoked, ~CaCertificate.is_ca)
    )
    import datetime

    revoked_lookup: dict[int, tuple[datetime.datetime, str | None]] = {}
    for row in revoked_result.scalars().all():
        serial = int(row.serial_hex, 16)
        revoked_lookup[serial] = (
            row.revoked_at or datetime.datetime.now(datetime.UTC),
            str(row.revoke_reason) if row.revoke_reason else None,
        )

    issuing_key, issuing_cert = load_issuing_ca(_get_secret())
    resp_der = ocsp_response_bytes(body, issuing_key, issuing_cert, cert_lookup, revoked_lookup)
    return Response(content=resp_der, media_type="application/ocsp-response")


# ── CSR signing ───────────────────────────────────────────────────────────────


@router.post("/sign", status_code=201)
async def sign_csr(
    body: SignCSRRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    _require_initialized()
    from sipsmith_ca.crypto import load_issuing_ca, sans_from_cert
    from sipsmith_ca.crypto import sign_csr as _sign

    settings = get_settings()
    issuing_key, issuing_cert = load_issuing_ca(_get_secret())

    try:
        cert = _sign(
            issuing_key,
            issuing_cert,
            body.csr_pem.encode(),
            body.profile.value,
            body.extra_sans or None,
            body.validity_days,
            fqdn=settings.server.fqdn,
        )
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    cert_pem = cert.public_bytes(Encoding.PEM).decode()
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = cn_attrs[0].value if cn_attrs else ""

    row = CaCertificate(
        serial_hex=hex(cert.serial_number),
        subject_cn=cn,
        subject_dn=cert.subject.rfc4514_string(),
        san_json=json.dumps(sans_from_cert(cert)),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        cert_pem=cert_pem,
        profile=body.profile.value,
        is_ca=False,
        is_lab_key=False,
        issued_by=user.username,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await AuditWriter(db).write(
        actor=user.username,
        action="ca.sign",
        resource=cn,
        new_value={"serial": hex(cert.serial_number), "profile": body.profile.value},
    )
    return {"cert_pem": cert_pem, "serial_hex": hex(cert.serial_number), "id": row.id}


@router.post("/issue", status_code=201)
async def issue_keypair(
    body: IssueKeyRequest,
    user: Annotated[User, Depends(require_role(Role.admin, Role.operator))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Generate key + cert on-box (lab-only, D3)."""
    _require_initialized()
    from sipsmith_ca.crypto import (
        generate_keypair_and_cert,
        load_issuing_ca,
        sans_from_cert,
    )

    settings = get_settings()
    issuing_key, issuing_cert = load_issuing_ca(_get_secret())

    try:
        key, cert = generate_keypair_and_cert(
            issuing_key,
            issuing_cert,
            body.subject_cn,
            body.sans,
            body.profile.value,
            body.validity_days,
            body.key_type,
            fqdn=settings.server.fqdn,
        )
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    cert_pem = cert.public_bytes(Encoding.PEM).decode()
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

    row = CaCertificate(
        serial_hex=hex(cert.serial_number),
        subject_cn=body.subject_cn,
        subject_dn=cert.subject.rfc4514_string(),
        san_json=json.dumps(sans_from_cert(cert)),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        cert_pem=cert_pem,
        profile=body.profile.value,
        is_ca=False,
        is_lab_key=True,
        issued_by=user.username,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await AuditWriter(db).write(
        actor=user.username,
        action="ca.issue_keypair",
        resource=body.subject_cn,
        new_value={"serial": hex(cert.serial_number), "profile": body.profile.value},
    )
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "serial_hex": hex(cert.serial_number),
        "id": row.id,
    }


# ── Certificate inventory ─────────────────────────────────────────────────────


@router.get("/certs", response_model=list[CertOut])
async def list_certs(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int | None = Query(default=None, description="Filter: expiring within N days"),
    revoked: bool | None = Query(default=None),
    is_ca: bool | None = Query(default=None),
) -> list[CaCertificate]:
    stmt = select(CaCertificate).order_by(CaCertificate.not_after)
    if revoked is not None:
        stmt = stmt.where(CaCertificate.revoked == revoked)
    if is_ca is not None:
        stmt = stmt.where(CaCertificate.is_ca == is_ca)
    result = await db.execute(stmt)
    certs = result.scalars().all()

    if days is not None:
        import datetime

        cutoff = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)
        certs = [c for c in certs if c.not_after.replace(tzinfo=datetime.UTC) <= cutoff]

    return list(certs)


@router.get("/certs/{cert_id}", response_model=CertDetailOut)
async def get_cert(
    cert_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CaCertificate:
    result = await db.execute(select(CaCertificate).where(CaCertificate.id == cert_id))
    cert = result.scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404, detail="Certificate not found")
    return cert


@router.get("/certs/{cert_id}/download")
async def download_cert(
    cert_id: int,
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    fmt: str = Query(default="pem", description="pem or der"),
) -> Response:
    result = await db.execute(select(CaCertificate).where(CaCertificate.id == cert_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404)

    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(row.cert_pem.encode())
    fn_base = row.subject_cn.replace(" ", "-").lower()

    if fmt == "der":
        return Response(
            content=cert.public_bytes(Encoding.DER),
            media_type="application/x-x509-user-cert",
            headers={"Content-Disposition": f"attachment; filename={fn_base}.der"},
        )
    return Response(
        content=cert.public_bytes(Encoding.PEM),
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f"attachment; filename={fn_base}.pem"},
    )


# ── Revocation ────────────────────────────────────────────────────────────────


@router.post("/certs/{cert_id}/revoke", status_code=204)
async def revoke_cert(
    cert_id: int,
    body: RevokeRequest,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    import datetime

    result = await db.execute(select(CaCertificate).where(CaCertificate.id == cert_id))
    cert = result.scalar_one_or_none()
    if cert is None:
        raise HTTPException(status_code=404)
    if cert.is_ca:
        raise HTTPException(
            status_code=400, detail="Cannot revoke CA certificates via this endpoint"
        )
    if cert.revoked:
        raise HTTPException(status_code=409, detail="Certificate already revoked")

    cert.revoked = True
    cert.revoked_at = datetime.datetime.now(datetime.UTC)
    cert.revoke_reason = body.reason
    await db.commit()

    await AuditWriter(db).write(
        actor=user.username,
        action="ca.revoke",
        resource=cert.subject_cn,
        new_value={"serial": cert.serial_hex, "reason": body.reason.value},
    )


# ── GUI HTTPS cert re-issue ───────────────────────────────────────────────────


@router.post("/gui-cert", status_code=201)
async def reissue_gui_cert(
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Re-issue the SIPsmith GUI HTTPS certificate from this CA."""
    _require_initialized()
    from sipsmith_ca.crypto import (
        generate_keypair_and_cert,
        load_issuing_ca,
        sans_from_cert,
    )

    settings = get_settings()
    fqdn = settings.server.fqdn
    issuing_key, issuing_cert = load_issuing_ca(_get_secret())

    key, cert = generate_keypair_and_cert(
        issuing_key,
        issuing_cert,
        subject_cn=fqdn,
        sans=[f"DNS:{fqdn}", "DNS:localhost"],
        profile_name="server",
        key_type="rsa2048",
        fqdn=fqdn,
    )

    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    cert_pem = cert.public_bytes(Encoding.PEM)
    key_pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())

    # Write via agent
    from sipsmith.agent.client import AgentClient

    agent = AgentClient()
    gui_cert_path = settings.server.tls_cert
    gui_key_path = settings.server.tls_key
    await agent.write_config(gui_cert_path, cert_pem.decode())
    await agent.write_config(gui_key_path, key_pem.decode())

    row = CaCertificate(
        serial_hex=hex(cert.serial_number),
        subject_cn=fqdn,
        subject_dn=cert.subject.rfc4514_string(),
        san_json=json.dumps(sans_from_cert(cert)),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        cert_pem=cert_pem.decode(),
        profile="server",
        is_ca=False,
        is_lab_key=True,
        issued_by=user.username,
    )
    db.add(row)
    await db.commit()

    await AuditWriter(db).write(actor=user.username, action="ca.gui_cert_reissue", resource=fqdn)
    return {"ok": True, "serial_hex": hex(cert.serial_number), "fqdn": fqdn}
