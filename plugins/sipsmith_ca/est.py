"""EST (Enrollment over Secure Transport) endpoint — RFC 7030."""

from __future__ import annotations

import base64
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.database import get_db
from sipsmith_ca.models import CaCertificate

log = logging.getLogger("sipsmith.ca.est")

# EST path is /.well-known/est per RFC 7030
router = APIRouter(prefix="/.well-known/est", tags=["ca-est"])


@router.get("/cacerts")
async def est_cacerts() -> Response:
    """
    RFC 7030 §4.1.1 — Return CA certificates as base64-encoded PKCS#7.
    Content-Type: application/pkcs7-mime; smime-type=certs-only
    """
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.hazmat.primitives.serialization.pkcs7 import serialize_certificates

    from sipsmith_ca.crypto import is_initialized, load_issuing_cert, load_root_cert

    if not is_initialized():
        return Response(status_code=503, content="CA not initialized")

    p7 = serialize_certificates([load_issuing_cert(), load_root_cert()], Encoding.DER)
    return Response(
        content=base64.b64encode(p7),
        media_type="application/pkcs7-mime; smime-type=certs-only",
        headers={"Content-Transfer-Encoding": "base64"},
    )


@router.post("/simpleenroll")
async def est_simple_enroll(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """
    RFC 7030 §4.2.1 — Simple enrollment.
    Body: base64-encoded DER PKCS#10 CSR.
    Returns: base64-encoded DER PKCS#7 (cert-only SignedData).
    """

    from sipsmith_ca.crypto import is_initialized

    if not is_initialized():
        return Response(status_code=503, content="CA not initialized")

    body = await request.body()
    if not body:
        return Response(status_code=400, content="Empty body")

    # Body is base64-encoded DER CSR per RFC 7030
    try:
        csr_der = base64.b64decode(body)
    except Exception:  # noqa: BLE001
        csr_der = body  # maybe raw DER

    return await _enroll(csr_der, db, is_reenroll=False)


@router.post("/simplereenroll")
async def est_simple_reenroll(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """RFC 7030 §4.2.2 — Re-enrollment. Same as simpleenroll for v1."""

    body = await request.body()
    if not body:
        return Response(status_code=400, content="Empty body")

    try:
        csr_der = base64.b64decode(body)
    except Exception:  # noqa: BLE001
        csr_der = body

    return await _enroll(csr_der, db, is_reenroll=True)


async def _enroll(csr_der: bytes, db: AsyncSession, is_reenroll: bool) -> Response:

    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    from sipsmith.config import get_settings
    from sipsmith_ca.crypto import load_issuing_ca, sans_from_cert
    from sipsmith_ca.crypto import sign_csr as _sign

    settings = get_settings()

    # Convert DER CSR to PEM
    try:
        csr = x509.load_der_x509_csr(csr_der)
    except Exception as exc:  # noqa: BLE001
        return Response(status_code=400, content=f"Invalid CSR: {exc}")

    csr_pem = csr.public_bytes(Encoding.PEM)
    issuing_key, issuing_cert = load_issuing_ca(settings.security.secret_key)

    try:
        cert = _sign(issuing_key, issuing_cert, csr_pem, "server_client", fqdn=settings.server.fqdn)
    except Exception as exc:  # noqa: BLE001
        return Response(status_code=422, content=f"Signing failed: {exc}")

    cert_pem = cert.public_bytes(Encoding.PEM).decode()
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn = cn_attrs[0].value if cn_attrs else ""

    # Record in DB
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
        issued_by="est",
    )
    db.add(row)
    await db.commit()
    log.info("EST: issued cert CN=%s serial=%s", cn, hex(cert.serial_number))

    # Response: base64-encoded DER cert per RFC 7030
    return Response(
        content=base64.b64encode(cert.public_bytes(Encoding.DER)),
        media_type="application/pkcs7-mime; smime-type=certs-only",
        headers={"Content-Transfer-Encoding": "base64"},
    )
