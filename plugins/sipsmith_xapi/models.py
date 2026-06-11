"""xAPI plugin database models."""

from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class XapiBase(DeclarativeBase):
    pass


class XapiDevice(XapiBase):
    __tablename__ = "xapi_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    password: Mapped[str | None] = mapped_column(String(256), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sw_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sip_uri: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reg_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_seen: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    poll_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="https")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    command_logs: Mapped[list[XapiCommandLog]] = relationship(
        "XapiCommandLog", back_populates="device", cascade="all, delete-orphan"
    )


class XapiCommandLog(XapiBase):
    __tablename__ = "xapi_command_logs"

    __table_args__ = (Index("ix_xapi_command_logs_device_ts", "device_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("xapi_devices.id", ondelete="CASCADE"), nullable=False
    )
    command_path: Mapped[str] = mapped_column(String(256), nullable=False)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    device: Mapped[XapiDevice] = relationship("XapiDevice", back_populates="command_logs")
