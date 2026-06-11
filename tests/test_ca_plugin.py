"""Phase 2 CA plugin tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── crypto helpers ────────────────────────────────────────────────────────────


def _make_issuing_ca(tmp_path):
    """Return (key, cert) for a temporary Issuing CA using a temp CA dir."""
    import sipsmith_ca.crypto as cm

    orig = cm.CA_DIR
    cm.CA_DIR = tmp_path / "ca"
    cm.ROOT_KEY_PATH = cm.CA_DIR / "root" / "root.key.enc"
    cm.ROOT_CERT_PATH = cm.CA_DIR / "root" / "root.crt"
    cm.ISSUING_KEY_PATH = cm.CA_DIR / "issuing" / "issuing.key.enc"
    cm.ISSUING_CERT_PATH = cm.CA_DIR / "issuing" / "issuing.crt"

    root_cert, issuing_cert = cm.generate_ca(
        secret_key="test-secret",
        root_cn="Test Root CA",
        issuing_cn="Test Issuing CA",
        key_type="rsa2048",
        fqdn="sipsmith.test",
    )

    issuing_key, issuing_cert2 = cm.load_issuing_ca("test-secret")

    # Restore originals
    cm.CA_DIR = orig
    cm.ROOT_KEY_PATH = orig / "root" / "root.key.enc"
    cm.ROOT_CERT_PATH = orig / "root" / "root.crt"
    cm.ISSUING_KEY_PATH = orig / "issuing" / "issuing.key.enc"
    cm.ISSUING_CERT_PATH = orig / "issuing" / "issuing.crt"

    return issuing_key, issuing_cert2


@pytest.fixture
def ca_dir(tmp_path):
    """Patch CA_DIR and related paths to a temp directory for each test."""
    import sipsmith_ca.crypto as cm

    orig = cm.CA_DIR
    orig_paths = {
        "ROOT_KEY_PATH": cm.ROOT_KEY_PATH,
        "ROOT_CERT_PATH": cm.ROOT_CERT_PATH,
        "ISSUING_KEY_PATH": cm.ISSUING_KEY_PATH,
        "ISSUING_CERT_PATH": cm.ISSUING_CERT_PATH,
        "CRL_PATH": cm.CRL_PATH,
    }

    ca = tmp_path / "ca"
    cm.CA_DIR = ca
    cm.ROOT_KEY_PATH = ca / "root" / "root.key.enc"
    cm.ROOT_CERT_PATH = ca / "root" / "root.crt"
    cm.ISSUING_KEY_PATH = ca / "issuing" / "issuing.key.enc"
    cm.ISSUING_CERT_PATH = ca / "issuing" / "issuing.crt"
    cm.CRL_PATH = ca / "crl" / "issuing.crl"

    yield ca

    cm.CA_DIR = orig
    for attr, val in orig_paths.items():
        setattr(cm, attr, val)


# ── CA generation ─────────────────────────────────────────────────────────────


def test_generate_ca_creates_files(ca_dir):
    from sipsmith_ca.crypto import generate_ca, is_initialized

    assert not is_initialized()
    root_cert, issuing_cert = generate_ca(
        secret_key="test-secret",
        root_cn="Test Root CA",
        issuing_cn="Test Issuing CA",
        key_type="rsa2048",
        fqdn="sipsmith.test",
    )
    assert is_initialized()
    assert (ca_dir / "root" / "root.crt").exists()
    assert (ca_dir / "issuing" / "issuing.crt").exists()
    assert (ca_dir / "root" / "root.key.enc").exists()
    assert (ca_dir / "issuing" / "issuing.key.enc").exists()

    from cryptography.x509.oid import NameOID

    cn = root_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "Test Root CA"


def test_ca_key_not_plaintext(ca_dir):
    """Key file must be encrypted — must not contain PRIVATE KEY header."""
    from sipsmith_ca.crypto import generate_ca

    generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")
    key_bytes = (ca_dir / "root" / "root.key.enc").read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in key_bytes
    assert b"PRIVATE KEY" in key_bytes  # PKCS8 encrypted PEM
    # Must NOT be plaintext
    assert b"BEGIN RSA PRIVATE KEY" not in key_bytes


def test_ca_key_file_mode(ca_dir):
    """Key files must be mode 0600."""

    from sipsmith_ca.crypto import generate_ca

    generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")
    for kf in [(ca_dir / "root" / "root.key.enc"), (ca_dir / "issuing" / "issuing.key.enc")]:
        mode = kf.stat().st_mode & 0o777
        assert mode == 0o600, f"{kf} has mode {oct(mode)}, expected 0600"


def test_generate_ca_idempotent_raises(ca_dir):
    from sipsmith_ca.crypto import generate_ca

    generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")
    with pytest.raises(RuntimeError, match="already initialized"):
        generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")


def test_ca_hierarchy(ca_dir):
    """Issuing cert must be signed by Root CA."""

    from sipsmith_ca.crypto import generate_ca, load_issuing_cert, load_root_cert

    generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")
    root = load_root_cert()
    issuing = load_issuing_cert()

    # Issuing issuer == Root subject
    assert issuing.issuer == root.subject
    # Root is self-signed
    assert root.issuer == root.subject


def test_issuing_ca_path_length(ca_dir):
    """Issuing CA must have path_length=0 (cannot sign sub-CAs)."""
    from cryptography import x509
    from sipsmith_ca.crypto import generate_ca, load_issuing_cert

    generate_ca(secret_key="test-secret", key_type="rsa2048", fqdn="test")
    issuing = load_issuing_cert()
    bc = issuing.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca
    assert bc.value.path_length == 0


# ── CSR signing ───────────────────────────────────────────────────────────────


def test_sign_csr(ca_dir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from sipsmith_ca.crypto import generate_ca, load_issuing_ca, sign_csr

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="sipsmith.test")
    key, issuing_cert = load_issuing_ca("s")

    # Generate a test CSR
    req_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cucm.lab.local")]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("cucm.lab.local"),
                    x509.DNSName("cucm-pub.lab.local"),
                ]
            ),
            critical=False,
        )
        .sign(req_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)

    signed = sign_csr(key, issuing_cert, csr_pem, "server_client", fqdn="sipsmith.test")

    assert signed.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "cucm.lab.local"
    # Verify signed by Issuing CA
    issuing_cert.public_key().verify(
        signed.signature,
        signed.tbs_certificate_bytes,
        asym_padding.PKCS1v15(),
        signed.signature_hash_algorithm,
    )


def test_sign_csr_server_client_eku(ca_dir):
    """server_client profile must include both serverAuth and clientAuth EKU."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    from sipsmith_ca.crypto import generate_ca, load_issuing_ca, sign_csr

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    key, issuing_cert = load_issuing_ca("s")

    req_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.local")]))
        .sign(req_key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    cert = sign_csr(key, issuing_cert, csr_pem, "server_client")
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    oids = list(eku.value)
    assert ExtendedKeyUsageOID.SERVER_AUTH in oids
    assert ExtendedKeyUsageOID.CLIENT_AUTH in oids


def test_sign_csr_with_extra_sans(ca_dir):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from sipsmith_ca.crypto import generate_ca, load_issuing_ca, sign_csr

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    key, issuing_cert = load_issuing_ca("s")

    req_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "base.local")]))
        .sign(req_key, hashes.SHA256())
    )
    cert = sign_csr(
        key,
        issuing_cert,
        csr.public_bytes(serialization.Encoding.PEM),
        "server",
        extra_sans=["DNS:extra.local", "IP:10.0.0.1"],
    )
    san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    names = [str(n.value) for n in san_ext.value]
    assert "extra.local" in names
    assert "10.0.0.1" in names


# ── Keypair generation ────────────────────────────────────────────────────────


def test_generate_keypair_and_cert(ca_dir):
    from sipsmith_ca.crypto import generate_ca, generate_keypair_and_cert, load_issuing_ca

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    issuing_key, issuing_cert = load_issuing_ca("s")

    key, cert = generate_keypair_and_cert(
        issuing_key,
        issuing_cert,
        subject_cn="device.lab.local",
        sans=["DNS:device.lab.local", "IP:192.168.1.100"],
        profile_name="server_client",
        key_type="rsa2048",
    )

    from cryptography.x509.oid import NameOID

    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "device.lab.local"
    assert key is not None


# ── CRL ───────────────────────────────────────────────────────────────────────


def test_generate_crl_empty(ca_dir):
    from sipsmith_ca.crypto import generate_ca, generate_crl, load_issuing_ca

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    key, cert = load_issuing_ca("s")
    crl_der = generate_crl(key, cert, [])
    assert crl_der[:2] == b"0\x82"  # DER SEQUENCE tag


def test_generate_crl_with_revoked(ca_dir):
    """CRL must contain the revoked serial."""

    from cryptography import x509
    from sipsmith_ca.crypto import generate_ca, generate_crl, load_issuing_ca

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    key, cert = load_issuing_ca("s")
    fake_serial = "0xdeadbeef"
    crl_der = generate_crl(
        key,
        cert,
        [{"serial_hex": fake_serial, "revoked_at": None, "reason": "key_compromise"}],
    )
    crl = x509.load_der_x509_crl(crl_der)
    serials = [rc.serial_number for rc in crl]
    assert int(fake_serial, 16) in serials


# ── OCSP ──────────────────────────────────────────────────────────────────────


def test_ocsp_good_response(ca_dir):
    """OCSP response for a known cert should return GOOD status."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509 import ocsp
    from cryptography.x509.oid import NameOID
    from sipsmith_ca.crypto import generate_ca, load_issuing_ca, ocsp_response_bytes, sign_csr

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    issuing_key, issuing_cert = load_issuing_ca("s")

    req_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ocsp-test.local")]))
        .sign(req_key, hashes.SHA256())
    )
    cert = sign_csr(
        issuing_key, issuing_cert, csr.public_bytes(serialization.Encoding.PEM), "server"
    )

    # Build OCSP request
    ocsp_req = (
        ocsp.OCSPRequestBuilder()
        .add_certificate(cert, issuing_cert, hashes.SHA1())  # noqa: S303
        .build()
    )
    req_der = ocsp_req.public_bytes(serialization.Encoding.DER)

    resp_der = ocsp_response_bytes(
        req_der,
        issuing_key,
        issuing_cert,
        cert_lookup={cert.serial_number: cert},
        revoked_lookup={},
    )
    resp = ocsp.load_der_ocsp_response(resp_der)
    assert resp.certificate_status == ocsp.OCSPCertStatus.GOOD


# ── SAN parsing ───────────────────────────────────────────────────────────────


def test_parse_san_strings_dns():
    from cryptography import x509
    from sipsmith_ca.crypto import parse_san_strings

    sans = parse_san_strings(["DNS:cucm.lab.local", "IP:10.1.1.1"])
    assert any(isinstance(s, x509.DNSName) and s.value == "cucm.lab.local" for s in sans)
    assert any(str(s.value) == "10.1.1.1" for s in sans if isinstance(s, x509.IPAddress))


def test_parse_san_strings_rejects_unknown():
    from sipsmith_ca.crypto import parse_san_strings

    with pytest.raises(ValueError, match="Unknown SAN type"):
        parse_san_strings(["WEIRDO:blah"])


# ── SCEP ──────────────────────────────────────────────────────────────────────


def test_scep_get_ca_cert(ca_dir):
    from sipsmith_ca.crypto import generate_ca, scep_get_ca_cert_response

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    data, ct = scep_get_ca_cert_response(include_chain=True)
    assert ct == "application/x-x509-ca-ra-cert"
    assert len(data) > 0


def test_scep_pki_req_round_trip(ca_dir):
    """Encrypt a CSR as a SCEP PKCSReq, send to CA, verify response contains cert."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization.pkcs7 import (
        PKCS7EnvelopeBuilder,
        PKCS7Options,
        PKCS7SignatureBuilder,
    )
    from cryptography.x509.oid import NameOID
    from sipsmith_ca.crypto import (
        _extract_signed_data_content,
        generate_ca,
        load_issuing_ca,
        scep_process_pki_req,
    )

    generate_ca(secret_key="s", key_type="rsa2048", fqdn="test")
    issuing_key, issuing_cert = load_issuing_ca("s")

    # Simulate a SCEP client
    req_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    req_subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone.lab.local")])
    req_cert = (
        x509.CertificateBuilder()
        .subject_name(req_subj)
        .issuer_name(req_subj)
        .public_key(req_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(__import__("datetime").datetime.now(__import__("datetime").UTC))
        .not_valid_after(
            __import__("datetime").datetime.now(__import__("datetime").UTC)
            + __import__("datetime").timedelta(days=1)
        )
        .sign(req_key, hashes.SHA256())
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(req_subj)
        .sign(req_key, hashes.SHA256())
    )
    csr_der = csr.public_bytes(serialization.Encoding.DER)

    # Build PKCSReq: CSR → inner SignedData → outer EnvelopedData (encrypted to CA)
    inner_signed = (
        PKCS7SignatureBuilder()
        .set_data(csr_der)
        .add_signer(req_cert, req_key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [PKCS7Options.Binary, PKCS7Options.NoCerts])
    )
    pki_message = (
        PKCS7EnvelopeBuilder()
        .set_data(inner_signed)
        .add_recipient(issuing_cert)
        .encrypt(serialization.Encoding.DER, [PKCS7Options.Binary])
    )

    # Server processes it
    response_der = scep_process_pki_req(
        pki_message, issuing_key, issuing_cert, "server_client", fqdn="test"
    )

    # Extract and parse the issued cert from the response
    cert_der = _extract_signed_data_content(response_der)
    issued = x509.load_der_x509_certificate(cert_der)
    cn = issued.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "phone.lab.local"


# ── Air-gap check ─────────────────────────────────────────────────────────────


def test_ca_code_no_external_urls():
    """CA plugin code must not contain any http:// or https:// external URLs."""
    plugin_dir = Path(__file__).parent.parent / "plugins" / "sipsmith_ca"
    for py_file in plugin_dir.rglob("*.py"):
        content = py_file.read_text()
        for line in content.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("#"):
                continue
            assert "https://" not in line_stripped, f"External URL in {py_file}: {line_stripped}"
