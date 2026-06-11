"""Phase 3 DNS plugin tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_zone(**kwargs):
    from types import SimpleNamespace

    defaults = dict(
        id=1,
        name="lab.local",
        zone_type="primary",
        ttl=3600,
        serial=2024010101,
        refresh=3600,
        retry=900,
        expire=604800,
        ns_primary="ns1.lab.local.",
        admin_email="admin@lab.local",
        enabled=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_record(**kwargs):
    from types import SimpleNamespace

    defaults = dict(
        id=1,
        zone_id=1,
        name="@",
        record_type="A",
        ttl=None,
        rdata="10.0.0.1",
        priority=None,
        weight=None,
        port=None,
        enabled=True,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── zone name validation ──────────────────────────────────────────────────────


def test_validate_zone_name_valid():
    from sipsmith_dns.zones import validate_zone_name

    assert validate_zone_name("lab.local")
    assert validate_zone_name("lab.local.")
    assert validate_zone_name("example.com")
    assert validate_zone_name("sub.domain.test")
    assert validate_zone_name("2.1.10.in-addr.arpa")


def test_validate_zone_name_invalid():
    from sipsmith_dns.zones import validate_zone_name

    assert not validate_zone_name("")
    assert not validate_zone_name("-bad.local")
    assert not validate_zone_name("a" * 254)


# ── record name validation ────────────────────────────────────────────────────


def test_validate_record_name_valid():
    from sipsmith_dns.zones import validate_record_name

    assert validate_record_name("@")
    assert validate_record_name("*")
    assert validate_record_name("www")
    assert validate_record_name("_cisco-uds._tcp")
    assert validate_record_name("host.sub")


def test_validate_record_name_invalid():
    from sipsmith_dns.zones import validate_record_name

    assert not validate_record_name("")
    assert not validate_record_name("a" * 254)


# ── serial management ─────────────────────────────────────────────────────────


def test_next_serial_today_prefix():
    import datetime

    from sipsmith_dns.zones import next_serial

    today = datetime.datetime.now(datetime.UTC)
    prefix = int(today.strftime("%Y%m%d")) * 100
    serial = next_serial(prefix + 3)
    assert serial == prefix + 4


def test_next_serial_old_serial():
    import datetime

    from sipsmith_dns.zones import next_serial

    today = datetime.datetime.now(datetime.UTC)
    prefix = int(today.strftime("%Y%m%d")) * 100
    serial = next_serial(2020010101)
    assert serial == prefix + 1


def test_next_serial_saturates_at_99():
    import datetime

    from sipsmith_dns.zones import next_serial

    today = datetime.datetime.now(datetime.UTC)
    prefix = int(today.strftime("%Y%m%d")) * 100
    # At 99 it wraps to next day's 01
    serial = next_serial(prefix + 99)
    assert serial == prefix + 1


# ── zone file rendering ───────────────────────────────────────────────────────


def test_render_zone_file_basic():
    from sipsmith_dns.zones import render_zone_file

    zone = _make_zone()
    records = [_make_record(name="www", record_type="A", rdata="10.0.0.2")]
    out = render_zone_file(zone, records)

    assert "$ORIGIN lab.local." in out
    assert "IN SOA" in out
    assert "IN NS" in out
    assert "www" in out
    assert "10.0.0.2" in out


def test_render_zone_file_srv():
    from sipsmith_dns.zones import render_zone_file

    zone = _make_zone()
    records = [
        _make_record(
            name="_sip._tcp",
            record_type="SRV",
            ttl=3600,
            rdata="cucm.lab.local",
            priority=10,
            weight=5,
            port=5060,
        )
    ]
    out = render_zone_file(zone, records)
    assert "_sip._tcp" in out
    assert "SRV 10 5 5060" in out
    assert "cucm.lab.local." in out


def test_render_zone_file_skips_disabled():
    from sipsmith_dns.zones import render_zone_file

    zone = _make_zone()
    records = [_make_record(name="disabled", rdata="1.2.3.4", enabled=False)]
    out = render_zone_file(zone, records)
    assert "disabled" not in out


def test_render_zone_file_txt_quoted():
    from sipsmith_dns.zones import render_zone_file

    zone = _make_zone()
    records = [_make_record(name="@", record_type="TXT", rdata="v=spf1 -all")]
    out = render_zone_file(zone, records)
    assert '"v=spf1 -all"' in out


# ── named.conf rendering ──────────────────────────────────────────────────────


def _make_settings(**kwargs):
    from types import SimpleNamespace

    defaults = dict(
        forwarders="[]",
        allow_recursion='["any"]',
        dlz_enabled=False,
        dlz_module_path=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_render_named_conf_empty():
    from sipsmith_dns.zones import render_named_conf

    s = _make_settings(allow_recursion='["localnets"]')
    out = render_named_conf([], s)
    assert "sipsmith-recursion" in out
    assert "localnets" in out
    assert "zone" not in out


def test_render_named_conf_with_zone_and_forwarder():
    from sipsmith_dns.zones import render_named_conf

    s = _make_settings(forwarders='["8.8.8.8"]')
    zone = _make_zone()
    out = render_named_conf([zone], s)
    assert 'zone "lab.local"' in out
    assert "type master" in out
    assert "forwarders" in out
    assert "8.8.8.8" in out


# ── presets ───────────────────────────────────────────────────────────────────


def test_cucm_presets():
    from sipsmith_dns.presets import cisco_cucm_presets

    presets = cisco_cucm_presets("cucm.lab.local")
    names = {p["name"] for p in presets}
    assert "_cisco-uds._tcp" in names
    assert "_sip._tcp" in names
    assert "_sips._tcp" in names
    assert "_sip._udp" in names
    assert "_cuplogin._tcp" in names
    for p in presets:
        assert p["rdata"].endswith(".")


def test_expressway_presets():
    from sipsmith_dns.presets import cisco_expressway_presets

    presets = cisco_expressway_presets("exp-e.lab.local.")
    names = {p["name"] for p in presets}
    assert "_collab-edge._tls" in names
    assert "_xmpp-server._tcp" in names
    for p in presets:
        assert p["rdata"].endswith(".")


# ── auto-PTR helper ───────────────────────────────────────────────────────────


def test_expand_auto_ptr_ipv4():
    from sipsmith_dns.zones import expand_auto_ptr

    result = expand_auto_ptr("10.1.2.3")
    assert result is not None
    zone, name = result
    assert zone == "2.1.10.in-addr.arpa"
    assert name == "3"


def test_expand_auto_ptr_invalid():
    from sipsmith_dns.zones import expand_auto_ptr

    assert expand_auto_ptr("not-an-ip") is None


# ── air-gap: no external URL references ──────────────────────────────────────


def test_no_external_urls_in_dns_plugin():
    """Ensure DNS plugin source has no http(s):// external references."""
    plugin_dir = Path(__file__).parent.parent / "plugins" / "sipsmith_dns"
    import re

    external_re = re.compile(r"https?://(?!localhost|127\.0\.0\.1)")
    violations = []
    for py_file in plugin_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if external_re.search(line):
                violations.append(f"{py_file}:{lineno}: {line.strip()}")
    assert not violations, "External URLs found in DNS plugin:\n" + "\n".join(violations)
