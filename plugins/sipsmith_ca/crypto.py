"""CA cryptographic operations — pure Python via the cryptography library."""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    PrivateFormat,
)
from cryptography.hazmat.primitives.serialization.pkcs7 import (
    PKCS7Options,
    PKCS7SignatureBuilder,
    pkcs7_decrypt_der,
    serialize_certificates,
)
from cryptography.x509.ocsp import OCSPCertStatus, OCSPResponseBuilder
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# ── Filesystem layout ─────────────────────────────────────────────────────────

CA_DIR = Path("/var/lib/sipsmith/ca")
ROOT_KEY_PATH = CA_DIR / "root" / "root.key.enc"
ROOT_CERT_PATH = CA_DIR / "root" / "root.crt"
ISSUING_KEY_PATH = CA_DIR / "issuing" / "issuing.key.enc"
ISSUING_CERT_PATH = CA_DIR / "issuing" / "issuing.crt"
CRL_PATH = CA_DIR / "crl" / "issuing.crl"

# ── Profiles ──────────────────────────────────────────────────────────────────

PROFILES: dict[str, dict[str, Any]] = {
    "server": {
        "key_usage": ["digital_signature", "key_encipherment"],
        "eku": [ExtendedKeyUsageOID.SERVER_AUTH],
        "validity_days": 730,
        "description": "TLS server certificate",
    },
    "client": {
        "key_usage": ["digital_signature"],
        "eku": [ExtendedKeyUsageOID.CLIENT_AUTH],
        "validity_days": 365,
        "description": "TLS client certificate",
    },
    "server_client": {
        "key_usage": ["digital_signature", "key_encipherment"],
        "eku": [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH],
        "validity_days": 730,
        "description": "TLS server + client (CUCM, Expressway, IM&P)",
    },
    "code_signing": {
        "key_usage": ["digital_signature"],
        "eku": [ExtendedKeyUsageOID.CODE_SIGNING],
        "validity_days": 365,
        "description": "Code signing",
    },
}

_KEY_USAGE_FLAGS: dict[str, x509.KeyUsage] = {}  # built lazily


# ── Internal helpers ──────────────────────────────────────────────────────────


def _derive_passphrase(secret_key: str) -> bytes:
    """Derive a stable key-encryption passphrase from the appliance secret."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sipsmith-ca-key-v1",
        info=b"ca-private-key-encryption",
    )
    return hkdf.derive(secret_key.encode())


def _generate_key(key_type: str) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    if key_type == "rsa4096":
        return rsa.generate_private_key(public_exponent=65537, key_size=4096)
    if key_type == "rsa2048":
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if key_type == "ec384":
        return ec.generate_private_key(ec.SECP384R1())
    if key_type == "ec256":
        return ec.generate_private_key(ec.SECP256R1())
    raise ValueError(f"Unknown key type: {key_type}")


def _save_key(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    path: Path,
    passphrase: bytes,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=BestAvailableEncryption(passphrase),
    )
    path.write_bytes(pem)
    path.chmod(0o600)


def _load_key(path: Path, passphrase: bytes) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=passphrase)


def _name(cn: str, org: str = "SIPsmith Lab", country: str = "US") -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _build_key_usage(flags: list[str]) -> x509.KeyUsage:
    allowed = {
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
        "encipher_only",
        "decipher_only",
    }
    kw: dict[str, bool] = {k: False for k in allowed}
    for f in flags:
        if f not in allowed:
            raise ValueError(f"Unknown key usage: {f}")
        kw[f] = True
    return x509.KeyUsage(**kw)


def _make_root_cert(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    cn: str,
    fqdn: str,
) -> x509.Certificate:
    now = _utc_now()
    subject = issuer = _name(cn)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                key_cert_sign=True,
                crl_sign=True,
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
    )
    return builder.sign(key, hashes.SHA256())


def _make_issuing_cert(
    key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    cn: str,
    root_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    root_cert: x509.Certificate,
    fqdn: str,
) -> x509.Certificate:
    now = _utc_now()
    subject = _name(cn)
    crl_dp_url = _crl_dp_url(fqdn)
    ocsp_url = _ocsp_url(fqdn)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365 * 10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                key_cert_sign=True,
                crl_sign=True,
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_cert.public_key()),
            critical=False,
        )
        .add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(crl_dp_url)],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.oid.AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(ocsp_url),
                    )
                ]
            ),
            critical=False,
        )
    )
    return builder.sign(root_key, hashes.SHA256())


def _crl_dp_url(fqdn: str) -> str:
    return f"http://{fqdn}/api/v1/plugins/ca/crl"


def _ocsp_url(fqdn: str) -> str:
    return f"http://{fqdn}/api/v1/plugins/ca/ocsp"


# ── Public API ────────────────────────────────────────────────────────────────


def is_initialized() -> bool:
    return ROOT_CERT_PATH.exists() and ISSUING_CERT_PATH.exists()


def generate_ca(
    secret_key: str,
    root_cn: str = "SIPsmith Root CA",
    issuing_cn: str = "SIPsmith Issuing CA",
    key_type: str = "rsa4096",
    fqdn: str = "localhost",
) -> tuple[x509.Certificate, x509.Certificate]:
    """Generate Root + Issuing CA hierarchy. Idempotent: raises if already initialized."""
    if is_initialized():
        raise RuntimeError("CA already initialized. Revoke and re-generate to replace.")

    passphrase = _derive_passphrase(secret_key)

    # Root CA
    root_key = _generate_key(key_type)
    root_cert = _make_root_cert(root_key, root_cn, fqdn)
    _save_key(root_key, ROOT_KEY_PATH, passphrase)
    ROOT_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ROOT_CERT_PATH.write_bytes(root_cert.public_bytes(Encoding.PEM))

    # Issuing CA
    issuing_key = _generate_key(key_type)
    issuing_cert = _make_issuing_cert(issuing_key, issuing_cn, root_key, root_cert, fqdn)
    _save_key(issuing_key, ISSUING_KEY_PATH, passphrase)
    ISSUING_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ISSUING_CERT_PATH.write_bytes(issuing_cert.public_bytes(Encoding.PEM))

    (CA_DIR / "crl").mkdir(parents=True, exist_ok=True)
    return root_cert, issuing_cert


def load_root_cert() -> x509.Certificate:
    return x509.load_pem_x509_certificate(ROOT_CERT_PATH.read_bytes())


def load_issuing_cert() -> x509.Certificate:
    return x509.load_pem_x509_certificate(ISSUING_CERT_PATH.read_bytes())


def load_issuing_ca(
    secret_key: str,
) -> tuple[rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, x509.Certificate]:
    passphrase = _derive_passphrase(secret_key)
    key = _load_key(ISSUING_KEY_PATH, passphrase)
    cert = load_issuing_cert()
    return key, cert


def parse_san_strings(sans: list[str]) -> list[x509.GeneralName]:
    """
    Parse SAN strings like "DNS:foo.local", "IP:10.0.0.1", "email:a@b.com".
    """
    result: list[x509.GeneralName] = []
    for s in sans:
        if ":" not in s:
            result.append(x509.DNSName(s))
            continue
        kind, value = s.split(":", 1)
        kind = kind.strip().lower()
        value = value.strip()
        if kind == "dns":
            result.append(x509.DNSName(value))
        elif kind in ("ip", "ip address"):
            result.append(x509.IPAddress(ipaddress.ip_address(value)))
        elif kind in ("email", "rfc822"):
            result.append(x509.RFC822Name(value))
        elif kind == "uri":
            result.append(x509.UniformResourceIdentifier(value))
        else:
            raise ValueError(f"Unknown SAN type: {kind!r}")
    return result


def sans_from_cert(cert: x509.Certificate) -> list[str]:
    """Return SANs as 'type:value' strings."""
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    result = []
    for gn in ext.value:
        if isinstance(gn, x509.DNSName):
            result.append(f"DNS:{gn.value}")
        elif isinstance(gn, x509.IPAddress):
            result.append(f"IP:{gn.value}")
        elif isinstance(gn, x509.RFC822Name):
            result.append(f"email:{gn.value}")
        elif isinstance(gn, x509.UniformResourceIdentifier):
            result.append(f"URI:{gn.value}")
    return result


def sign_csr(
    issuing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    issuing_cert: x509.Certificate,
    csr_pem: bytes | str,
    profile_name: str,
    extra_sans: list[str] | None = None,
    validity_days: int | None = None,
    fqdn: str = "localhost",
) -> x509.Certificate:
    """Sign a CSR with the Issuing CA using the named profile."""
    if isinstance(csr_pem, str):
        csr_pem = csr_pem.encode()

    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature is invalid")

    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(f"Unknown profile: {profile_name!r}")

    days = validity_days if validity_days is not None else profile["validity_days"]
    now = _utc_now()

    # Collect SANs: from CSR + any extras
    try:
        csr_sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        san_values = list(csr_sans.value)
    except x509.ExtensionNotFound:
        san_values = []

    if extra_sans:
        san_values.extend(parse_san_strings(extra_sans))

    builder = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(issuing_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            _build_key_usage(profile["key_usage"]),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(profile["eku"]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuing_cert.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),
            critical=False,
        )
        .add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(_crl_dp_url(fqdn))],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.oid.AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(_ocsp_url(fqdn)),
                    )
                ]
            ),
            critical=False,
        )
    )

    if san_values:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_values), critical=False)

    return builder.sign(issuing_key, hashes.SHA256())


def generate_keypair_and_cert(
    issuing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    issuing_cert: x509.Certificate,
    subject_cn: str,
    sans: list[str],
    profile_name: str,
    validity_days: int | None = None,
    key_type: str = "rsa2048",
    fqdn: str = "localhost",
) -> tuple[rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Generate a private key + signed certificate (lab-only off-box keygen)."""
    key = _generate_key(key_type)
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(f"Unknown profile: {profile_name!r}")

    days = validity_days if validity_days is not None else profile["validity_days"]
    now = _utc_now()
    subject = _name(subject_cn)
    san_values = parse_san_strings(sans) if sans else []

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuing_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_build_key_usage(profile["key_usage"]), critical=True)
        .add_extension(x509.ExtendedKeyUsage(profile["eku"]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuing_cert.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        full_name=[x509.UniformResourceIdentifier(_crl_dp_url(fqdn))],
                        relative_name=None,
                        reasons=None,
                        crl_issuer=None,
                    )
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        x509.oid.AuthorityInformationAccessOID.OCSP,
                        x509.UniformResourceIdentifier(_ocsp_url(fqdn)),
                    )
                ]
            ),
            critical=False,
        )
    )
    if san_values:
        builder = builder.add_extension(x509.SubjectAlternativeName(san_values), critical=False)

    cert = builder.sign(issuing_key, hashes.SHA256())
    return key, cert


def generate_crl(
    issuing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    issuing_cert: x509.Certificate,
    revoked_entries: list[dict],
    next_update_days: int = 7,
) -> bytes:
    """
    Generate a CRL and return DER bytes.
    revoked_entries: list of {serial_hex, revoked_at, reason} dicts.
    """
    from cryptography.x509 import ReasonFlags

    now = _utc_now()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuing_cert.subject)
        .last_update(now)
        .next_update(now + datetime.timedelta(days=next_update_days))
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuing_cert.public_key()),
            critical=False,
        )
        .add_extension(x509.CRLNumber(int.from_bytes(os.urandom(4), "big")), critical=False)
    )

    _reason_map = {
        "unspecified": ReasonFlags.unspecified,
        "key_compromise": ReasonFlags.key_compromise,
        "ca_compromise": ReasonFlags.ca_compromise,
        "affiliation_changed": ReasonFlags.affiliation_changed,
        "superseded": ReasonFlags.superseded,
        "cessation_of_operation": ReasonFlags.cessation_of_operation,
        "privilege_withdrawn": ReasonFlags.privilege_withdrawn,
    }

    for entry in revoked_entries:
        serial = int(entry["serial_hex"], 16)
        revoked_at = entry.get("revoked_at") or now
        if isinstance(revoked_at, str):
            revoked_at = datetime.datetime.fromisoformat(revoked_at).replace(tzinfo=datetime.UTC)
        reason_str = entry.get("reason", "unspecified") or "unspecified"
        reason = _reason_map.get(reason_str, ReasonFlags.unspecified)

        rc = (
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(revoked_at)
            .add_extension(x509.CRLReason(reason), critical=False)
            .build()
        )
        builder = builder.add_revoked_certificate(rc)

    crl = builder.sign(private_key=issuing_key, algorithm=hashes.SHA256())
    return crl.public_bytes(Encoding.DER)


def ocsp_response_bytes(
    request_der: bytes,
    issuing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    issuing_cert: x509.Certificate,
    cert_lookup: dict[int, x509.Certificate],
    revoked_lookup: dict[int, tuple[datetime.datetime, str | None]],
) -> bytes:
    """
    Build an OCSP response for the given request.
    cert_lookup: serial_int -> Certificate (for good certs)
    revoked_lookup: serial_int -> (revoked_at, reason_str)
    """
    from cryptography.x509 import ocsp

    req = ocsp.load_der_ocsp_request(request_der)

    # Find the cert matching the request
    serial = req.serial_number
    cert = cert_lookup.get(serial)

    now = _utc_now()
    builder = OCSPResponseBuilder()

    if cert is None and serial not in revoked_lookup:
        # Unknown cert — return error response
        return ocsp.OCSPResponseBuilder.build_unsuccessful(
            ocsp.OCSPResponseStatus.UNAUTHORIZED
        ).public_bytes(Encoding.DER)

    if serial in revoked_lookup:
        revoked_at, reason_str = revoked_lookup[serial]
        from cryptography.x509 import ReasonFlags

        _reason_map = {
            "key_compromise": ReasonFlags.key_compromise,
            "ca_compromise": ReasonFlags.ca_compromise,
            "affiliation_changed": ReasonFlags.affiliation_changed,
            "superseded": ReasonFlags.superseded,
            "cessation_of_operation": ReasonFlags.cessation_of_operation,
            "privilege_withdrawn": ReasonFlags.privilege_withdrawn,
        }
        reason = _reason_map.get(reason_str or "", ReasonFlags.unspecified)
        builder = builder.add_response(
            cert=cert or issuing_cert,
            issuer=issuing_cert,
            algorithm=hashes.SHA1(),  # noqa: S303 — OCSP spec requires SHA-1 for key hash
            cert_status=OCSPCertStatus.REVOKED,
            this_update=now,
            next_update=now + datetime.timedelta(hours=1),
            revocation_time=revoked_at,
            revocation_reason=reason,
        )
    else:
        builder = builder.add_response(
            cert=cert,
            issuer=issuing_cert,
            algorithm=hashes.SHA1(),  # noqa: S303 — OCSP spec requires SHA-1 for key hash
            cert_status=OCSPCertStatus.GOOD,
            this_update=now,
            next_update=now + datetime.timedelta(hours=1),
            revocation_time=None,
            revocation_reason=None,
        )

    resp = builder.responder_id(x509.ocsp.OCSPResponderEncoding.HASH, issuing_cert).sign(
        issuing_key, hashes.SHA256()
    )
    return resp.public_bytes(Encoding.DER)


# ── SCEP helpers ──────────────────────────────────────────────────────────────


def _extract_signed_data_content(der: bytes) -> bytes:
    """
    Extract the eContent OCTET STRING from a CMS ContentInfo(SignedData) DER.
    Navigates: ContentInfo → SignedData → encapContentInfo → [0] → OCTET STRING.
    """

    def _parse_len(data: bytes, off: int) -> tuple[int, int]:
        b = data[off]
        if b < 0x80:
            return b, off + 1
        n = b & 0x7F
        return int.from_bytes(data[off + 1 : off + 1 + n], "big"), off + 1 + n

    off = 0
    assert der[off] == 0x30, "Expected ContentInfo SEQUENCE"
    _, off = _parse_len(der, off + 1)
    assert der[off] == 0x06, "Expected contentType OID"
    olen, o = _parse_len(der, off + 1)
    off = o + olen
    assert der[off] == 0xA0, "Expected [0] EXPLICIT"
    _, off = _parse_len(der, off + 1)
    assert der[off] == 0x30, "Expected SignedData SEQUENCE"
    _, off = _parse_len(der, off + 1)
    assert der[off] == 0x02, "Expected version INTEGER"
    vlen, o = _parse_len(der, off + 1)
    off = o + vlen
    assert der[off] == 0x31, "Expected digestAlgorithms SET"
    dlen, o = _parse_len(der, off + 1)
    off = o + dlen
    assert der[off] == 0x30, "Expected encapContentInfo SEQUENCE"
    _, off = _parse_len(der, off + 1)
    assert der[off] == 0x06, "Expected contentType OID"
    olen, o = _parse_len(der, off + 1)
    off = o + olen
    assert der[off] == 0xA0, "Expected [0] EXPLICIT (eContent)"
    _, off = _parse_len(der, off + 1)
    assert der[off] == 0x04, "Expected OCTET STRING (content)"
    clen, o = _parse_len(der, off + 1)
    return der[o : o + clen]


def scep_get_ca_cert_response(include_chain: bool = True) -> tuple[bytes, str]:
    """
    Return (data, content_type) for SCEP GetCACert.
    Returns PKCS#7 chain if include_chain, else single cert DER.
    """
    if not is_initialized():
        raise RuntimeError("CA not initialized")

    if include_chain:
        root_cert = load_root_cert()
        issuing_cert = load_issuing_cert()
        data = serialize_certificates([issuing_cert, root_cert], Encoding.DER)
        return data, "application/x-x509-ca-ra-cert"
    else:
        data = load_issuing_cert().public_bytes(Encoding.DER)
        return data, "application/x-x509-ca-cert"


def scep_process_pki_req(
    pki_message: bytes,
    issuing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    issuing_cert: x509.Certificate,
    profile_name: str,
    fqdn: str,
) -> bytes:
    """
    Process a SCEP PKCSReq message. Returns DER-encoded CMS response.
    pki_message: raw DER bytes of the outer CMSEnvelopedData.
    """
    # Step 1: Decrypt outer EnvelopedData
    inner_signed_der = pkcs7_decrypt_der(pki_message, issuing_cert, issuing_key, [])

    # Step 2: Extract CSR from inner SignedData
    csr_der = _extract_signed_data_content(inner_signed_der)
    csr = x509.load_der_x509_csr(csr_der)
    if not csr.is_signature_valid:
        raise ValueError("SCEP PKCSReq CSR signature invalid")

    # Step 3: Sign the CSR
    csr_pem = csr.public_bytes(Encoding.PEM)
    cert = sign_csr(issuing_key, issuing_cert, csr_pem, profile_name, fqdn=fqdn)

    # Step 4: Build response as DER SignedData containing the signed cert
    cert_der = cert.public_bytes(Encoding.DER)
    response = (
        PKCS7SignatureBuilder()
        .set_data(cert_der)
        .add_signer(issuing_cert, issuing_key, hashes.SHA256())
        .sign(Encoding.DER, [PKCS7Options.Binary, PKCS7Options.NoCerts])
    )
    return response
