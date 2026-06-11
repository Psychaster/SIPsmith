"""DNS plugin database models."""

from __future__ import annotations

import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class DnsBase(DeclarativeBase):
    pass


class ZoneType(StrEnum):
    primary = "primary"
    forward = "forward"
    reverse = "reverse"


class RecordType(StrEnum):
    A = "A"
    AAAA = "AAAA"
    PTR = "PTR"
    CNAME = "CNAME"
    MX = "MX"
    TXT = "TXT"
    SRV = "SRV"
    NS = "NS"
    CAA = "CAA"


class DnsZone(DnsBase):
    __tablename__ = "dns_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    zone_type: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)
    ttl: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    serial: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    refresh: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)
    retry: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    expire: Mapped[int] = mapped_column(Integer, default=604800, nullable=False)
    ns_primary: Mapped[str] = mapped_column(String(253), nullable=False)
    admin_email: Mapped[str] = mapped_column(String(253), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        onupdate=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    records: Mapped[list[DnsRecord]] = relationship(
        "DnsRecord", back_populates="zone", cascade="all, delete-orphan"
    )


class DnsRecord(DnsBase):
    __tablename__ = "dns_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("dns_zones.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(253), nullable=False)
    record_type: Mapped[str] = mapped_column(String(8), nullable=False)
    ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rdata: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight: Mapped[int | None] = mapped_column(Integer, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    zone: Mapped[DnsZone] = relationship("DnsZone", back_populates="records")


class DnsSettings(DnsBase):
    __tablename__ = "dns_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    forwarders: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    allow_recursion: Mapped[str] = mapped_column(Text, default='["any"]', nullable=False)
    dlz_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dlz_module_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
