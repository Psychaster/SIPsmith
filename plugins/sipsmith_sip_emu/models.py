"""SIP Endpoint Emulator — SQLAlchemy async models."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class SipEmuBase(DeclarativeBase):
    pass


class Endpoint(SipEmuBase):
    """A configured SIP endpoint — one row equals one emulated SIP phone."""

    __tablename__ = "sip_emu_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    sip_user: Mapped[str] = mapped_column(String(64), nullable=False)
    sip_domain: Mapped[str] = mapped_column(String(128), nullable=False)
    sip_password: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # e.g. "sip:cucm.lab"; if null, derived from sip_domain
    registrar_uri: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # udp / tcp / tls
    transport: Mapped[str] = mapped_column(String(8), nullable=False, default="udp")
    group_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    use_srtp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    video_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # preferred codec
    audio_codec: Mapped[str] = mapped_column(String(32), nullable=False, default="PCMU")
    # unregistered / registering / registered / error
    reg_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unregistered")
    reg_expires: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reg_contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reg_error_detail: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    calls: Mapped[list[EndpointCall]] = relationship(
        "EndpointCall",
        back_populates="endpoint",
        cascade="all, delete-orphan",
    )


class EndpointCall(SipEmuBase):
    """One call instance on an emulated endpoint."""

    __tablename__ = "sip_emu_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sip_emu_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    # internal call ID from pjsua2
    pjsip_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    local_uri: Mapped[str | None] = mapped_column(String(256), nullable=True)
    remote_uri: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # outbound / inbound
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="outbound")
    # calling / incoming / early / connecting / confirmed / disconnected
    call_state: Mapped[str] = mapped_column(String(16), nullable=False, default="calling")
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connected_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cause_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cause_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    video_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # soft reference to records.call_journeys
    journey_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    endpoint: Mapped[Endpoint] = relationship("Endpoint", back_populates="calls")
    sip_events: Mapped[list[SipEvent]] = relationship(
        "SipEvent",
        back_populates="call",
        cascade="all, delete-orphan",
    )
    rtp_samples: Mapped[list[RtpStatsSample]] = relationship(
        "RtpStatsSample",
        back_populates="call",
        cascade="all, delete-orphan",
    )


class SipEvent(SipEmuBase):
    """One SIP message in a call — used to render the ladder diagram."""

    __tablename__ = "sip_emu_events"
    __table_args__ = (Index("ix_sip_evt_call_ts", "call_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sip_emu_calls.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # "tx" (we sent) or "rx" (we received)
    direction: Mapped[str] = mapped_column(String(4), nullable=False)
    # INVITE, ACK, BYE, CANCEL, OPTIONS …
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 100, 180, 183, 200, 4xx, 5xx …
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # display name for ladder, e.g. "emu-001"
    from_participant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # display name for ladder, e.g. "CUCM"
    to_participant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cseq: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_first_line: Mapped[str | None] = mapped_column(String(256), nullable=True)

    call: Mapped[EndpointCall] = relationship("EndpointCall", back_populates="sip_events")


class RtpStatsSample(SipEmuBase):
    """Periodic RTP statistics snapshot for a call."""

    __tablename__ = "sip_emu_rtp_samples"
    __table_args__ = (Index("ix_rtp_sample_call_ts", "call_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("sip_emu_calls.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    audio_tx_pkt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_rx_pkt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_tx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    audio_rx_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    jitter_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mos: Mapped[float | None] = mapped_column(Float, nullable=True)

    call: Mapped[EndpointCall] = relationship("EndpointCall", back_populates="rtp_samples")


class Scenario(SipEmuBase):
    """A YAML scenario definition."""

    __tablename__ = "sip_emu_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    yaml_content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    runs: Mapped[list[ScenarioRun]] = relationship(
        "ScenarioRun",
        back_populates="scenario",
        cascade="all, delete-orphan",
    )


class ScenarioRun(SipEmuBase):
    """An execution of a scenario."""

    __tablename__ = "sip_emu_scenario_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sip_emu_scenarios.id", ondelete="SET NULL"),
        nullable=True,
    )
    # snapshot of name at time of run
    scenario_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # pending / running / passed / failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    steps_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    scenario: Mapped[Scenario | None] = relationship("Scenario", back_populates="runs")
