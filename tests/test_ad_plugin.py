"""Phase 7 Active Directory plugin tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── Models ────────────────────────────────────────────────────────────────────


def test_models_importable():
    from sipsmith_ad.models import AdDomain, AdGroup, AdOU, AdSyncAccount, AdUser

    assert "domain" in AdDomain.__tablename__
    assert "users" in AdUser.__tablename__
    assert "groups" in AdGroup.__tablename__
    assert "ous" in AdOU.__tablename__
    assert "sync" in AdSyncAccount.__tablename__


# ── Realm → base DN conversion ────────────────────────────────────────────────


def test_realm_to_base_dn_simple():
    from sipsmith_ad.api import _realm_to_base_dn

    assert _realm_to_base_dn("LAB.LOCAL") == "DC=LAB,DC=LOCAL"


def test_realm_to_base_dn_three_parts():
    from sipsmith_ad.api import _realm_to_base_dn

    assert _realm_to_base_dn("CORP.LAB.LOCAL") == "DC=CORP,DC=LAB,DC=LOCAL"


def test_user_dn():
    from sipsmith_ad.api import _user_dn

    dn = _user_dn("jsmith", "LAB.LOCAL")
    assert dn == "CN=jsmith,CN=Users,DC=LAB,DC=LOCAL"


# ── Bulk user pattern generation ──────────────────────────────────────────────


def test_bulk_pattern_generates_correct_names():
    """Pattern mode: prefix+index zero-padded to width of last index."""
    prefix = "user"
    start = 1
    count = 10
    pad = len(str(start + count - 1))
    names = [f"{prefix}{(start + i):0{pad}d}" for i in range(count)]
    assert names[0] == "user01"
    assert names[-1] == "user10"
    assert len(names) == 10


def test_bulk_pattern_large_count():
    prefix = "ep"
    start = 1
    count = 250
    pad = len(str(start + count - 1))
    first = f"{prefix}{start:0{pad}d}"
    last = f"{prefix}{(start + count - 1):0{pad}d}"
    assert first == "ep001"
    assert last == "ep250"


def test_bulk_pattern_phone_numbering():
    phone_prefix = "+1-555-"
    phone_start = 1000
    count = 5
    phones = [f"{phone_prefix}{phone_start + i}" for i in range(count)]
    assert phones[0] == "+1-555-1000"
    assert phones[4] == "+1-555-1004"


# ── CSV bulk parsing ───────────────────────────────────────────────────────────


def test_bulk_csv_parse():
    import csv
    import io

    csv_content = (
        "sam_account,first_name,last_name,password,telephone_number,ip_phone,ou_dn\n"
        "user001,User,001,Sipsmith1!,+1-555-1001,2001,OU=TestUsers,DC=lab,DC=local\n"
        "user002,User,002,Sipsmith1!,+1-555-1002,2002,\n"
    )
    reader = csv.DictReader(io.StringIO(csv_content))
    rows = list(reader)
    assert len(rows) == 2
    assert rows[0]["sam_account"] == "user001"
    assert rows[0]["telephone_number"] == "+1-555-1001"
    assert rows[0]["ip_phone"] == "2001"
    assert rows[1]["ou_dn"] == ""


# ── Provision request validation ──────────────────────────────────────────────


def test_provision_request_schema():
    from sipsmith_ad.api import ProvisionRequest

    req = ProvisionRequest(
        realm="LAB.LOCAL",
        netbios_name="LAB",
        admin_password="Sipsmith1!",
        dsrm_password="DsrmPass1!",
    )
    assert req.realm == "LAB.LOCAL"
    assert req.dns_mode == "dlz"


def test_ou_create_schema():
    from sipsmith_ad.api import OUCreate

    ou = OUCreate(name="TestUsers", description="Test user OU")
    assert ou.name == "TestUsers"
    assert ou.description == "Test user OU"


# ── Agent protocol: AD verbs in allowlist ─────────────────────────────────────


def test_ad_verbs_in_agent_allowlist():
    from sipsmith.agent.protocol import ALLOWED_VERBS

    expected = {
        "ad.provision",
        "ad.deprovision",
        "ad.info",
        "ad.user_create",
        "ad.user_delete",
        "ad.group_create",
        "ad.group_delete",
        "ad.ou_create",
        "ad.ou_delete",
        "ad.ldb_set_attr",
        "ad.set_ldaps_cert",
        "ad.dlz_bind_include",
    }
    assert expected.issubset(ALLOWED_VERBS)


def test_samba_write_paths_allowed():
    from sipsmith.agent.protocol import ALLOWED_WRITE_PREFIXES

    assert any(p.startswith("/etc/samba") for p in ALLOWED_WRITE_PREFIXES)
    assert any(p.startswith("/var/lib/samba") for p in ALLOWED_WRITE_PREFIXES)


# ── Air-gap: no external URLs ─────────────────────────────────────────────────


def test_no_external_urls():
    import re

    pkg = Path(__file__).parent.parent / "plugins" / "sipsmith_ad"
    pattern = re.compile(r"https?://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)")
    violations = []
    for pyfile in pkg.rglob("*.py"):
        for i, line in enumerate(pyfile.read_text().splitlines(), 1):
            if pattern.search(line) and "noqa" not in line:
                violations.append(f"{pyfile.relative_to(pkg)}:{i}: {line.strip()}")
    assert not violations, "External URL(s) found:\n" + "\n".join(violations)
