"""SCEP (Simple Certificate Enrollment Protocol) endpoint — RFC 8894."""

from __future__ import annotations

import base64
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.database import get_db
from sipsmith_ca.models import CaCertificate

log = logging.getLogger("sipsmith.ca.scep")

router = APIRouter(prefix="/plugins/ca/scep", tags=["ca-scep"])

_SCEP_OPS = {"GetCACert", "PKCSReq", "GetCertInitial", "GetCert", "GetCRL"}


@router.get("")
@router.post("")
async def scep_endpoint(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    operation: str = Query(..., description="SCEP operation name"),
    message: str | None = Query(default=None, description="Base64-encoded message for GET"),
) -> Response:

    from sipsmith_ca.crypto import is_initialized, scep_get_ca_cert_response

    if not is_initialized():
        return Response(content=b"CA not initialized", status_code=503)

    if operation not in _SCEP_OPS:
        return Response(content=b"Unsupported operation", status_code=400)

    # ── GetCACert ─────────────────────────────────────────────────────────────
    if operation == "GetCACert":
        data, ct = scep_get_ca_cert_response(include_chain=True)
        return Response(content=data, media_type=ct)

    # ── PKCSReq ───────────────────────────────────────────────────────────────
    if operation == "PKCSReq":
        body = await request.body()
        if not body and message:
            body = base64.b64decode(message)
        if not body:
            return Response(content=b"Empty PKCSReq message", status_code=400)

        try:
            return await _handle_pki_req(body, db)
        except Exception as exc:  # noqa: BLE001
            log.warning("SCEP PKCSReq failed: %s", exc)
            return Response(content=b"PKCSReq failed", status_code=400)

    # ── GetCertInitial ────────────────────────────────────────────────────────
    if operation == "GetCertInitial":
        body = await request.body()
        if not body and message:
            body = base64.b64decode(message)
        # For simplicity, respond the same as PKCSReq (synchronous issuance)
        return await _handle_pki_req(body, db) if body else Response(content=b"", status_code=400)

    # ── GetCRL ────────────────────────────────────────────────────────────────
    if operation == "GetCRL":
        from sipsmith.config import get_settings
        from sipsmith_ca.crypto import generate_crl, load_issuing_ca

        settings = get_settings()
        revoked_result = await db.execute(
            select(CaCertificate).where(CaCertificate.revoked, ~CaCertificate.is_ca)
        )
        revoked_list = [
            {
                "serial_hex": c.serial_hex,
                "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
                "reason": str(c.revoke_reason) if c.revoke_reason else "unspecified",
            }
            for c in revoked_result.scalars().all()
        ]
        key, cert = load_issuing_ca(settings.security.secret_key)
        crl_der = generate_crl(key, cert, revoked_list)
        return Response(content=crl_der, media_type="application/pkix-crl")

    return Response(content=b"Unsupported", status_code=400)


async def _handle_pki_req(body: bytes, db: AsyncSession) -> Response:
    """Process a PKCSReq and return a CMS response with the issued cert."""
    import json

    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    from sipsmith.config import get_settings
    from sipsmith_ca.crypto import load_issuing_ca, sans_from_cert, scep_process_pki_req
    from sipsmith_ca.models import CaCertificate

    settings = get_settings()
    issuing_key, issuing_cert = load_issuing_ca(settings.security.secret_key)

    response_der = scep_process_pki_req(
        body, issuing_key, issuing_cert, "server_client", fqdn=settings.server.fqdn
    )

    # Extract the cert from the response to record it in the DB
    from sipsmith_ca.crypto import _extract_signed_data_content

    cert_der = _extract_signed_data_content(response_der)
    from cryptography import x509

    cert = x509.load_der_x509_certificate(cert_der)
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
        profile="server_client",
        is_ca=False,
        issued_by="scep",
    )
    db.add(row)
    await db.commit()
    log.info("SCEP: issued cert CN=%s serial=%s", cn, hex(cert.serial_number))

    return Response(content=response_der, media_type="application/x-pki-message")
