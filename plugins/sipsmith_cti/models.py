"""CTI (JTAPI) plugin — SQLAlchemy async models."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class CtiBase(DeclarativeBase):
    pass


class CtiCluster(CtiBase):
    """A CUCM cluster that the CTI sidecar connects to via JTAPI."""

    __tablename__ = "cti_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    cucm_host: Mapped[str] = mapped_column(String(253), nullable=False)
    cucm_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    axl_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    axl_password: Mapped[str | None] = mapped_column(String(256), nullable=True)
    app_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_password: Mapped[str | None] = mapped_column(String(256), nullable=True)
    jtapi_jar_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # unconfigured / starting / connected / error / disconnected / jtapi_unavailable
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unconfigured")
    last_connected_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    devices: Mapped[list[CtiDevice]] = relationship(
        "CtiDevice",
        back_populates="cluster",
        cascade="all, delete-orphan",
    )
    calls: Mapped[list[CtiCall]] = relationship(
        "CtiCall",
        back_populates="cluster",
        cascade="all, delete-orphan",
    )
    events: Mapped[list[CtiEvent]] = relationship(
        "CtiEvent",
        back_populates="cluster",
        cascade="all, delete-orphan",
    )


class CtiDevice(CtiBase):
    """A JTAPI-observed phone device on a CUCM cluster."""

    __tablename__ = "cti_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cti_clusters.id", ondelete="CASCADE"),
        nullable=False,
    )
    # e.g. SEP001122334455
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    directory_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # unknown / registered / unregistered / rejected
    reg_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    active_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.UTC),
    )

    cluster: Mapped[CtiCluster] = relationship("CtiCluster", back_populates="devices")


class CtiCall(CtiBase):
    """A call observed via JTAPI."""

    __tablename__ = "cti_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cti_clusters.id", ondelete="CASCADE"),
        nullable=False,
    )
    calling_device: Mapped[str] = mapped_column(String(64), nullable=False)
    called_dn: Mapped[str] = mapped_column(String(32), nullable=False)
    # calling / connected / held / queued / failed / dropped
    call_state: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cause_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    cluster: Mapped[CtiCluster] = relationship("CtiCluster", back_populates="calls")


class CtiEvent(CtiBase):
    """A raw JTAPI event received from the sidecar."""

    __tablename__ = "cti_events"
    __table_args__ = (Index("ix_cti_event_cluster_ts", "cluster_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cti_clusters.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    cluster: Mapped[CtiCluster] = relationship("CtiCluster", back_populates="events")
