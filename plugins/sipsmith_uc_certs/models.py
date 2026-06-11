"""UC Cert Orchestrator database models."""

from __future__ import annotations

import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UcCertsBase(DeclarativeBase):
    pass


class UcProduct(StrEnum):
    cucm = "cucm"
    expressway = "expressway"
    cuc = "cuc"
    imp = "imp"
    cms = "cms"


class UcCluster(UcCertsBase):
    __tablename__ = "uc_clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    product: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSON lists/dicts stored as text
    domains: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    toggles: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    nodes: Mapped[list[UcNode]] = relationship(
        "UcNode",
        back_populates="cluster",
        cascade="all, delete-orphan",
        order_by="UcNode.sort_order",
    )
    plans: Mapped[list[UcCertPlan]] = relationship(
        "UcCertPlan",
        back_populates="cluster",
        cascade="all, delete-orphan",
    )


class UcNode(UcCertsBase):
    __tablename__ = "uc_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("uc_clusters.id", ondelete="CASCADE"), nullable=False
    )
    fqdn: Mapped[str] = mapped_column(String(253), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="node", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    cluster: Mapped[UcCluster] = relationship("UcCluster", back_populates="nodes")


class PlanStatus(StrEnum):
    pending = "pending"
    dns_ok = "dns_ok"
    dns_warn = "dns_warn"
    dns_fail = "dns_fail"
    signing = "signing"
    complete = "complete"


class ItemStatus(StrEnum):
    pending = "pending"
    csr_uploaded = "csr_uploaded"
    csr_invalid = "csr_invalid"
    signed = "signed"
    delivered = "delivered"


class UcCertPlan(UcCertsBase):
    __tablename__ = "uc_cert_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("uc_clusters.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    dns_preflight: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    generated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )

    cluster: Mapped[UcCluster] = relationship("UcCluster", back_populates="plans")
    items: Mapped[list[UcCertPlanItem]] = relationship(
        "UcCertPlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="UcCertPlanItem.restart_priority",
    )


class UcCertPlanItem(UcCertsBase):
    __tablename__ = "uc_cert_plan_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("uc_cert_plans.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="cluster", nullable=False)
    node_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("uc_nodes.id", ondelete="SET NULL"), nullable=True
    )
    sans: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON list
    eku: Mapped[str] = mapped_column(Text, default='["serverAuth"]', nullable=False)  # JSON list
    profile: Mapped[str] = mapped_column(String(32), default="server", nullable=False)
    trust_stores: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON list
    restart_priority: Mapped[int] = mapped_column(Integer, default=99, nullable=False)
    service_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    csr_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    cert_chain_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    csr_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    csr_errors: Mapped[str] = mapped_column(Text, default="[]", nullable=False)  # JSON list

    plan: Mapped[UcCertPlan] = relationship("UcCertPlan", back_populates="items")
    node: Mapped[UcNode | None] = relationship("UcNode")
