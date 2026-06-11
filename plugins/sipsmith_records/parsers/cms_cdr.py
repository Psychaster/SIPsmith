"""
CMS (Meeting Server) CDR XML parser.
CMS POSTs CDR records as XML to a configured receiver URI.
"""

from __future__ import annotations

import datetime
import logging
import xml.etree.ElementTree as ET
from typing import Any

log = logging.getLogger("sipsmith.records.parser.cms_cdr")


def parse_cms_cdr_xml(xml_body: str | bytes, source_id: int) -> list[dict[str, Any]]:
    """
    Parse a CMS CDR XML payload (single record or batch).
    Returns list of normalized dicts suitable for CallRecord insertion.
    """
    from sipsmith_records.parsers.q850 import label as q850_label

    if isinstance(xml_body, bytes):
        xml_body = xml_body.decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(xml_body)  # noqa: S314
    except ET.ParseError as exc:
        log.warning("CMS CDR XML parse error: %s", exc)
        return []

    records: list[dict[str, Any]] = []

    # CMS may wrap multiple records in a <records> or <cdrRecords> envelope,
    # or POST a single <record> element directly.
    if root.tag in ("records", "cdrRecords"):
        items = list(root)
    else:
        items = [root]

    for item in items:
        try:
            rec = _parse_cms_element(item, source_id, q850_label)
            if rec:
                records.append(rec)
        except Exception:  # noqa: BLE001
            log.debug("Skipping malformed CMS CDR element")

    return records


def _iso(val: str | None) -> datetime.datetime | None:
    if not val:
        return None
    val = val.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.datetime.strptime(val, fmt).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    return None


def _parse_cms_element(
    el: ET.Element,
    source_id: int,
    q850_label: Any,
) -> dict[str, Any] | None:
    def g(tag: str) -> str | None:
        child = el.find(tag)
        return child.text.strip() if child is not None and child.text else None

    call_id = g("callId") or g("id") or el.get("id")
    caller = g("callerID") or g("callerName") or g("callerNumber")
    callee = g("calleeID") or g("calleeName") or g("calleeNumber")
    start = _iso(g("startTime") or g("callStart"))
    end = _iso(g("endTime") or g("callEnd"))
    duration_raw = g("duration")
    try:
        duration_sec = int(duration_raw) if duration_raw else None
    except ValueError:
        duration_sec = None
    if duration_sec is None and start and end:
        duration_sec = int((end - start).total_seconds())

    cause_raw = g("causeCode") or g("terminationCause")
    try:
        cause_code = int(cause_raw) if cause_raw else None
    except ValueError:
        cause_code = None

    return {
        "source_id": source_id,
        "global_call_id": None,
        "sip_call_id": g("sipCallId") or g("sipCallID"),
        "cms_call_id": call_id,
        "calling_dn": caller,
        "called_dn": callee,
        "calling_device": g("callerDevice"),
        "called_device": g("calleeDevice"),
        "start_time": start,
        "connect_time": start,
        "end_time": end,
        "duration_sec": duration_sec,
        "cause_code": cause_code,
        "cause_label": q850_label(cause_code),
        "raw_source": "cms_cdr",
        "raw_line": None,
    }
