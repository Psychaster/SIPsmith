"""CA plugin DB models."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CaBase(DeclarativeBase):
    pass


class CertProfile(enum.StrEnum):
    server = "server"
    client = "client"
    server_client = "server_client"
    code_signing = "code_signing"
    custom = "custom"


class RevocationReason(enum.StrEnum):
    unspecified = "unspecified"
    key_compromise = "key_compromise"
    ca_compromise = "ca_compromise"
    affiliation_changed = "affiliation_changed"
    superseded = "superseded"
    cessation_of_operation = "cessation_of_operation"
    privilege_withdrawn = "privilege_withdrawn"


class CaCertificate(CaBase):
    __tablename__ = "ca_certificates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    serial_hex: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    subject_cn: Mapped[str] = mapped_column(String(256), nullable=False)
    subject_dn: Mapped[str] = mapped_column(Text, nullable=False)
    san_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    is_ca: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_lab_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[RevocationReason | None] = mapped_column(
        Enum(RevocationReason, name="ca_revoke_reason"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    issued_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
