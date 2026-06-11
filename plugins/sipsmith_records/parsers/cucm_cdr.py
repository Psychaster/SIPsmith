"""
CUCM CDR flat-file parser.
File format: comma-separated, first row is header.
Reference: CUCM CDR/CMR Administration Guide.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
from typing import Any

log = logging.getLogger("sipsmith.records.parser.cucm_cdr")

# Minimum CDR fields we care about (column names vary slightly by version)
_FIELD_MAP = {
    "globalCallID_callId": "global_call_id",
    "globalCallID_callManagerId": None,  # combined below
    "origLegCallIdentifier": None,
    "dateTimeConnect": "connect_time_raw",
    "dateTimeDisconnect": "end_time_raw",
    "dateTimeOrigination": "start_time_raw",
    "origNodeId": None,
    "origSpan": None,
    "origIpAddr": None,
    "callingPartyNumber": "calling_dn",
    "calledPartyNumber": "called_dn",
    "finalCalledPartyNumber": None,
    "origDeviceName": "calling_device",
    "destDeviceName": "called_device",
    "duration": "duration_raw",
    "origCause_value": "cause_code_raw",
    "destCause_value": None,
    "origCallTerminationOnBehalfOf": None,
    "lastRedirectDn": None,
    "pkid": None,
    "originalCalledPartyNumber": None,
    "callingPartyUnicodeLoginUserID": None,
    "finalCalledPartyUnicodeLoginUserID": None,
    "origMeddevName": None,
    "destMeddevName": None,
    "origMediaCap_maxFramesPerPacket": None,
    "destMediaCap_maxFramesPerPacket": None,
    "origVideoCap_Codec": None,
    "destVideoCap_Codec": None,
    "origRSVPAudioStat": None,
    "destRSVPAudioStat": None,
}


def _epoch_to_dt(val: str | None) -> datetime.datetime | None:
    """Convert CUCM Unix timestamp string to aware datetime."""
    if not val or not val.strip():
        return None
    try:
        ts = int(val.strip())
        if ts == 0:
            return None
        return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    except (ValueError, OSError):
        return None


def parse_cdr_content(content: str, source_id: int) -> list[dict[str, Any]]:
    """
    Parse CUCM CDR file content into a list of normalized dicts.
    Returns one dict per call leg suitable for bulk-inserting into CallRecord.
    """
    from sipsmith_records.parsers.q850 import label as q850_label

    records: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(content))
    for row in reader:
        try:
            rec = _parse_row(row, source_id, q850_label)
            if rec:
                records.append(rec)
        except Exception:  # noqa: BLE001
            log.debug("Skipping malformed CDR row")
    return records


def _parse_row(
    row: dict[str, str],
    source_id: int,
    q850_label: Any,
) -> dict[str, Any] | None:
    # Global call ID: callManagerId:callId
    mgr_id = row.get("globalCallID_callManagerId", "").strip()
    call_id = row.get("globalCallID_callId", "").strip()
    global_call_id = f"{mgr_id}:{call_id}" if mgr_id and call_id else call_id or None

    # Cause code
    cause_raw = row.get("origCause_value", "").strip()
    try:
        cause_code = int(cause_raw) if cause_raw else None
    except ValueError:
        cause_code = None

    # Duration
    dur_raw = row.get("duration", "").strip()
    try:
        duration_sec = int(dur_raw) if dur_raw else None
    except ValueError:
        duration_sec = None

    start_time = _epoch_to_dt(row.get("dateTimeOrigination"))
    connect_time = _epoch_to_dt(row.get("dateTimeConnect"))
    end_time = _epoch_to_dt(row.get("dateTimeDisconnect"))

    return {
        "source_id": source_id,
        "global_call_id": global_call_id,
        "sip_call_id": None,
        "calling_dn": row.get("callingPartyNumber", "").strip() or None,
        "called_dn": row.get("calledPartyNumber", "").strip() or None,
        "calling_device": row.get("origDeviceName", "").strip() or None,
        "called_device": row.get("destDeviceName", "").strip() or None,
        "start_time": start_time,
        "connect_time": connect_time,
        "end_time": end_time,
        "duration_sec": duration_sec,
        "cause_code": cause_code,
        "cause_label": q850_label(cause_code),
        "raw_source": "cucm_cdr",
        "raw_line": None,
    }
