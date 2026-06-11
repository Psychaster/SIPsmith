"""Phase 5 Records Landing tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))


# ── Q.850 ─────────────────────────────────────────────────────────────────────


def test_q850_label_normal():
    from sipsmith_records.parsers.q850 import label

    assert label(16) == "Normal call clearing"


def test_q850_label_unknown():
    from sipsmith_records.parsers.q850 import label

    assert label(255) == "Cause 255"


def test_q850_label_none():
    from sipsmith_records.parsers.q850 import label

    assert label(None) == ""


def test_q850_is_normal_true():
    from sipsmith_records.parsers.q850 import is_normal

    assert is_normal(16)
    assert is_normal(17)
    assert is_normal(19)
    assert is_normal(31)


def test_q850_is_normal_false():
    from sipsmith_records.parsers.q850 import is_normal

    assert not is_normal(1)
    assert not is_normal(21)
    assert not is_normal(None)


def test_q850_cause_codes_dict():
    from sipsmith_records.parsers.q850 import CAUSE_CODES

    assert isinstance(CAUSE_CODES, dict)
    assert 16 in CAUSE_CODES


# ── CUCM CDR parser ───────────────────────────────────────────────────────────

_CUCM_SAMPLE = (
    "globalCallID_callManagerId,globalCallID_callId,callingPartyNumber,calledPartyNumber,"
    "origDeviceName,destDeviceName,dateTimeOrigination,dateTimeConnect,dateTimeDisconnect,"
    "duration,origCause_value\n"
    "1,1001,1000,2000,SEP111111111111,SEP222222222222,"
    "1700000000,1700000005,1700000065,60,16\n"
    "2,1002,3000,4000,,,0,0,0,0,1\n"
)


def test_cucm_parse_basic():
    from sipsmith_records.parsers.cucm_cdr import parse_cdr_content

    records = parse_cdr_content(_CUCM_SAMPLE, source_id=1)
    assert len(records) == 2
    r = records[0]
    assert r["global_call_id"] == "1:1001"
    assert r["calling_dn"] == "1000"
    assert r["called_dn"] == "2000"
    assert r["duration_sec"] == 60
    assert r["cause_code"] == 16
    assert r["cause_label"] == "Normal call clearing"
    assert r["raw_source"] == "cucm_cdr"


def test_cucm_parse_zero_timestamps():
    from sipsmith_records.parsers.cucm_cdr import parse_cdr_content

    records = parse_cdr_content(_CUCM_SAMPLE, source_id=1)
    r = records[1]
    assert r["start_time"] is None
    assert r["cause_code"] == 1


def test_cucm_parse_epoch_conversion():
    import datetime

    from sipsmith_records.parsers.cucm_cdr import _epoch_to_dt

    dt = _epoch_to_dt("1700000000")
    assert dt is not None
    assert dt.tzinfo == datetime.UTC


def test_cucm_parse_empty():
    from sipsmith_records.parsers.cucm_cdr import parse_cdr_content

    records = parse_cdr_content("", source_id=1)
    assert records == []


def test_cucm_parse_header_only():
    from sipsmith_records.parsers.cucm_cdr import parse_cdr_content

    records = parse_cdr_content(
        "globalCallID_callManagerId,globalCallID_callId,callingPartyNumber\n",
        source_id=1,
    )
    assert records == []


# ── CMS CDR XML parser ────────────────────────────────────────────────────────

_CMS_SINGLE = """<record id="abc-123">
  <callId>abc-123</callId>
  <callerID>Alice</callerID>
  <calleeID>Bob</calleeID>
  <startTime>2024-01-15T10:00:00</startTime>
  <endTime>2024-01-15T10:05:00</endTime>
  <duration>300</duration>
  <causeCode>16</causeCode>
  <sipCallId>call-id-xyz@cms</sipCallId>
</record>"""

_CMS_BATCH = """<records>
  <record id="r1">
    <callId>r1</callId>
    <callerID>1000</callerID>
    <calleeID>2000</calleeID>
    <startTime>2024-01-15T09:00:00</startTime>
    <endTime>2024-01-15T09:01:00</endTime>
  </record>
  <record id="r2">
    <callId>r2</callId>
    <callerID>3000</callerID>
    <calleeID>4000</calleeID>
    <startTime>2024-01-15T09:05:00</startTime>
  </record>
</records>"""

_CMS_INVALID = "<not valid xml"


def test_cms_parse_single():
    from sipsmith_records.parsers.cms_cdr import parse_cms_cdr_xml

    records = parse_cms_cdr_xml(_CMS_SINGLE, source_id=2)
    assert len(records) == 1
    r = records[0]
    assert r["cms_call_id"] == "abc-123"
    assert r["calling_dn"] == "Alice"
    assert r["called_dn"] == "Bob"
    assert r["duration_sec"] == 300
    assert r["cause_code"] == 16
    assert r["sip_call_id"] == "call-id-xyz@cms"
    assert r["raw_source"] == "cms_cdr"


def test_cms_parse_batch():
    from sipsmith_records.parsers.cms_cdr import parse_cms_cdr_xml

    records = parse_cms_cdr_xml(_CMS_BATCH, source_id=2)
    assert len(records) == 2
    assert records[0]["cms_call_id"] == "r1"
    assert records[1]["cms_call_id"] == "r2"


def test_cms_parse_duration_computed():
    from sipsmith_records.parsers.cms_cdr import parse_cms_cdr_xml

    xml = """<record>
      <callId>x1</callId>
      <startTime>2024-01-15T10:00:00</startTime>
      <endTime>2024-01-15T10:01:30</endTime>
    </record>"""
    records = parse_cms_cdr_xml(xml, source_id=2)
    assert len(records) == 1
    assert records[0]["duration_sec"] == 90


def test_cms_parse_invalid_xml():
    from sipsmith_records.parsers.cms_cdr import parse_cms_cdr_xml

    records = parse_cms_cdr_xml(_CMS_INVALID, source_id=2)
    assert records == []


def test_cms_parse_bytes():
    from sipsmith_records.parsers.cms_cdr import parse_cms_cdr_xml

    records = parse_cms_cdr_xml(_CMS_SINGLE.encode(), source_id=2)
    assert len(records) == 1


# ── Air-gap: no external URLs ─────────────────────────────────────────────────


def test_no_external_urls():
    import re

    pkg = Path(__file__).parent.parent / "plugins" / "sipsmith_records"
    pattern = re.compile(r"https?://(?!localhost|127\.0\.0\.1|0\.0\.0\.0)")
    violations = []
    for pyfile in pkg.rglob("*.py"):
        for i, line in enumerate(pyfile.read_text().splitlines(), 1):
            if pattern.search(line) and "noqa" not in line:
                violations.append(f"{pyfile.relative_to(pkg)}:{i}: {line.strip()}")
    assert not violations, "External URL(s) found:\n" + "\n".join(violations)
