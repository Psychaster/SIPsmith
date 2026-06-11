"""Phase 4 UC Cert Orchestrator tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── Pack loader ───────────────────────────────────────────────────────────────


def test_list_packs():
    from sipsmith_uc_certs.packs.loader import list_packs

    packs = list_packs()
    ids = {p["product"] for p in packs}
    assert "cucm" in ids
    assert "expressway" in ids


def test_load_pack_cucm():
    from sipsmith_uc_certs.packs.loader import load_pack

    pack = load_pack("cucm")
    assert pack["product"] == "cucm"
    assert "services" in pack
    assert any(s["id"] == "tomcat" for s in pack["services"])


def test_load_pack_missing():
    import pytest
    from sipsmith_uc_certs.packs.loader import load_pack

    with pytest.raises(FileNotFoundError):
        load_pack("nonexistent_product_xyz")


def test_get_pack_services_cucm_14():
    from sipsmith_uc_certs.packs.loader import get_pack_services

    services = get_pack_services("cucm", "14.0.1")
    ids = {s["id"] for s in services}
    assert "tomcat" in ids
    assert "callmanager" in ids
    assert "ipsec" in ids


def test_get_pack_services_version_too_old():
    import pytest
    from sipsmith_uc_certs.packs.loader import get_pack_services

    with pytest.raises(ValueError, match="floor"):
        get_pack_services("cucm", "11.0")


def test_get_pack_services_expressway():
    from sipsmith_uc_certs.packs.loader import get_pack_services

    services = get_pack_services("expressway", "X14.0")
    assert any(s["id"] == "expressway" for s in services)


def test_version_tuple_expressway():
    from sipsmith_uc_certs.packs.loader import _version_tuple

    assert _version_tuple("X14.3") == (14, 3)
    assert _version_tuple("14.0.1") == (14, 0, 1)
    assert _version_tuple("12.5") == (12, 5)


# ── SAN computation ───────────────────────────────────────────────────────────


def _nodes(*fqdns):
    return [SimpleNamespace(fqdn=f, ip=None) for f in fqdns]


def test_compute_sans_all_nodes():
    from sipsmith_uc_certs.packs.loader import compute_sans

    svc = {"san_sources": ["all_node_fqdns", "all_node_hostnames"]}
    nodes = _nodes("cucm-pub.lab.local", "cucm-sub.lab.local")
    sans = compute_sans(svc, nodes, [], {})
    assert "cucm-pub.lab.local" in sans
    assert "cucm-sub.lab.local" in sans
    assert "cucm-pub" in sans
    assert "cucm-sub" in sans


def test_compute_sans_per_node():
    from sipsmith_uc_certs.packs.loader import compute_sans

    svc = {"san_sources": ["node_fqdn", "node_hostname"]}
    nodes = _nodes("cucm-pub.lab.local")
    node = SimpleNamespace(fqdn="cucm-pub.lab.local")
    sans = compute_sans(svc, nodes, [], {}, current_node=node)
    assert "cucm-pub.lab.local" in sans
    assert "cucm-pub" in sans


def test_compute_sans_deduplication():
    from sipsmith_uc_certs.packs.loader import compute_sans

    svc = {"san_sources": ["all_node_fqdns", "all_node_fqdns"]}
    nodes = _nodes("host.lab.local")
    sans = compute_sans(svc, nodes, [], {})
    assert sans.count("host.lab.local") == 1


def test_compute_sans_mra_domains():
    from sipsmith_uc_certs.packs.loader import compute_sans

    svc = {"san_sources": ["mra_domains"]}
    nodes = []
    toggles = {"mra_domains": ["collab.lab.local", "mra.lab.local"]}
    sans = compute_sans(svc, nodes, [], toggles)
    assert "collab.lab.local" in sans
    assert "mra.lab.local" in sans


# ── DNS requirements ──────────────────────────────────────────────────────────


def test_dns_requirements_cucm_no_mra():
    from sipsmith_uc_certs.packs.loader import get_dns_requirements

    reqs = get_dns_requirements("cucm", {})
    types = {r["type"] for r in reqs}
    assert "A" in types
    assert "PTR" in types
    # SRV should not appear without mra_enabled
    assert all(r["type"] != "SRV" for r in reqs)


def test_dns_requirements_cucm_with_mra():
    from sipsmith_uc_certs.packs.loader import get_dns_requirements

    reqs = get_dns_requirements("cucm", {"mra_enabled": True})
    srv_reqs = [r for r in reqs if r["type"] == "SRV"]
    names = [r["name_pattern"] for r in srv_reqs]
    assert any("_cisco-uds._tcp" in n for n in names)
    assert any("_collab-edge" not in n for n in names)  # collab-edge is expressway


def test_dns_requirements_expressway_with_mra():
    from sipsmith_uc_certs.packs.loader import get_dns_requirements

    reqs = get_dns_requirements("expressway", {"mra_enabled": True})
    srv_reqs = [r for r in reqs if r["type"] == "SRV"]
    names = [r["name_pattern"] for r in srv_reqs]
    assert any("_collab-edge._tls" in n for n in names)


# ── CSR validation ────────────────────────────────────────────────────────────


def _make_csr(sans: list[str], key_bits: int = 2048) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_bits)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0] if sans else "test")])
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def test_validate_csr_valid():
    from sipsmith_uc_certs.orchestrator import validate_csr

    sans = ["cucm-pub.lab.local", "cucm-sub.lab.local"]
    csr_pem = _make_csr(sans)
    valid, errors = validate_csr(sans, csr_pem)
    assert valid
    assert not any("Missing" in e for e in errors)


def test_validate_csr_missing_sans():
    from sipsmith_uc_certs.orchestrator import validate_csr

    expected = ["cucm-pub.lab.local", "cucm-sub.lab.local"]
    csr_pem = _make_csr(["cucm-pub.lab.local"])  # missing sub
    valid, errors = validate_csr(expected, csr_pem)
    assert not valid
    assert any("Missing" in e for e in errors)


def test_validate_csr_small_key():
    from sipsmith_uc_certs.orchestrator import validate_csr

    csr_pem = _make_csr(["host.lab.local"], key_bits=1024)
    valid, errors = validate_csr(["host.lab.local"], csr_pem)
    assert not valid
    assert any("RSA key too small" in e for e in errors)


# ── Air-gap ───────────────────────────────────────────────────────────────────


def test_no_external_urls_in_uc_certs_plugin():
    import re

    plugin_dir = Path(__file__).parent.parent / "plugins" / "sipsmith_uc_certs"
    external_re = re.compile(r"https?://(?!localhost|127\.0\.0\.1)")
    violations = []
    for src in plugin_dir.rglob("*.py"):
        for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            if external_re.search(line):
                violations.append(f"{src}:{lineno}: {line.strip()}")
    assert not violations, "External URLs in UC certs plugin:\n" + "\n".join(violations)
