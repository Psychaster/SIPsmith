"""Records Landing database models."""

from __future__ import annotations

import datetime
from enum import StrEnum

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


class RecordsBase(DeclarativeBase):
    pass


class SourceType(StrEnum):
    cucm_cdr = "cucm_cdr"
    cucm_drf = "cucm_drf"
    cms_cdr = "cms_cdr"
    expressway = "expressway"


class SourceStatus(StrEnum):
    active = "active"
    disabled = "disabled"


class RecordSource(RecordsBase):
    """A configured data source (one per cluster × type)."""

    __tablename__ = "record_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cluster_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # SFTP sources: path under /var/lib/sipsmith/sftp/<account>
    sftp_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sftp_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # HTTP receiver sources: generated token for auth
    receiver_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Retention
    retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    last_ingested_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    calls: Mapped[list[CallRecord]] = relationship(
        "CallRecord", back_populates="source", cascade="all, delete-orphan"
    )
    drf_sets: Mapped[list[DrfSet]] = relationship(
        "DrfSet", back_populates="source", cascade="all, delete-orphan"
    )


class CallRecord(RecordsBase):
    """
    A single call leg, normalized from CUCM CDR, CMS CDR, or Expressway record.
    Indexed for search and journey correlation.
    """

    __tablename__ = "call_records"
    __table_args__ = (
        Index("ix_call_global_id", "global_call_id"),
        Index("ix_call_sip_id", "sip_call_id"),
        Index("ix_call_start", "start_time"),
        Index("ix_call_source", "source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("record_sources.id", ondelete="CASCADE"), nullable=False
    )
    # Correlation keys
    global_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sip_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    cms_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exp_call_serial: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Parties
    calling_dn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    called_dn: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calling_device: Mapped[str | None] = mapped_column(String(128), nullable=True)
    called_device: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Timing
    start_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    connect_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Outcome
    cause_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cause_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Media
    codec_rx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    codec_tx: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mos: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Metadata
    raw_source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    journey_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("call_journeys.id", ondelete="SET NULL"), nullable=True
    )
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    source: Mapped[RecordSource] = relationship("RecordSource", back_populates="calls")
    journey: Mapped[CallJourney | None] = relationship(
        "CallJourney", back_populates="legs", foreign_keys=[journey_id]
    )


class CallJourney(RecordsBase):
    """
    Stitched cross-product call journey — one row per unique call event
    spanning multiple legs from different sources.
    """

    __tablename__ = "call_journeys"
    __table_args__ = (Index("ix_journey_global", "anchor_global_call_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    anchor_global_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    start_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    leg_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cause_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    legs: Mapped[list[CallRecord]] = relationship(
        "CallRecord",
        back_populates="journey",
        foreign_keys="CallRecord.journey_id",
    )


class DrfSet(RecordsBase):
    """A DRF/DRS backup set from CUCM."""

    __tablename__ = "drf_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("record_sources.id", ondelete="CASCADE"), nullable=False
    )
    cluster_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    backup_date: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    source: Mapped[RecordSource] = relationship("RecordSource", back_populates="drf_sets")
