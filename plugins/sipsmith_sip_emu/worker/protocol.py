"""IPC message dataclasses for the pjsua2 worker subprocess.

Commands flow from the control plane → worker via stdin (JSON lines).
Events flow from the worker → control plane via stdout (JSON lines).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ── Commands (control-plane → worker) ────────────────────────────────────────


@dataclass
class Command:
    """A JSON-RPC-style command sent to the worker on stdin."""

    id: str
    method: str
    params: dict[str, Any]

    @classmethod
    def from_json(cls, line: str) -> Command:
        data = json.loads(line)
        return cls(
            id=str(data.get("id", "")),
            method=str(data.get("method", "")),
            params=data.get("params") or {},
        )


# ── Events (worker → control-plane) ──────────────────────────────────────────


@dataclass
class WorkerEvent:
    """Base class for all worker events."""

    event: str = field(init=False)

    def to_json(self) -> str:
        return json.dumps(self.__dict__)


@dataclass
class WorkerReadyEvent(WorkerEvent):
    account_count: int = 0

    def __post_init__(self) -> None:
        self.event = "worker_ready"


@dataclass
class RegStateEvent(WorkerEvent):
    ep_id: int = 0
    state: str = ""
    expires: int | None = None
    contact: str | None = None
    error_detail: str | None = None
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "reg_state"


@dataclass
class CallCreatedEvent(WorkerEvent):
    ep_id: int = 0
    pjsip_call_id: str = ""
    direction: str = "outbound"
    remote_uri: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "call_created"


@dataclass
class CallStateEvent(WorkerEvent):
    pjsip_call_id: str = ""
    state: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "call_state"


@dataclass
class CallEndedEvent(WorkerEvent):
    pjsip_call_id: str = ""
    cause_code: int = 0
    cause_label: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "call_ended"


@dataclass
class SipMsgEvent(WorkerEvent):
    pjsip_call_id: str = ""
    direction: str = "tx"
    method: str | None = None
    status_code: int | None = None
    from_participant: str | None = None
    to_participant: str | None = None
    cseq: str | None = None
    raw_first_line: str | None = None
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "sip_msg"


@dataclass
class MediaStateEvent(WorkerEvent):
    pjsip_call_id: str = ""
    audio_active: bool = False
    video_active: bool = False
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "media_state"


@dataclass
class RtpStatsEvent(WorkerEvent):
    pjsip_call_id: str = ""
    audio_tx_pkt: int | None = None
    audio_rx_pkt: int | None = None
    audio_tx_bytes: int | None = None
    audio_rx_bytes: int | None = None
    jitter_ms: float | None = None
    rtt_ms: float | None = None
    loss_pct: float | None = None
    mos: float | None = None
    ts: str = ""

    def __post_init__(self) -> None:
        self.event = "rtp_stats"


@dataclass
class ErrorEvent(WorkerEvent):
    message: str = ""

    def __post_init__(self) -> None:
        self.event = "error"
