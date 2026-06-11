"""SFTP plugin DB models — prefixed sftp_ to avoid collisions."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class SftpBase(DeclarativeBase):
    pass


class AuthType(enum.StrEnum):
    password = "password"
    key = "key"
    both = "both"


class SftpPreset(enum.StrEnum):
    drs_backup = "drs_backup"
    firmware_moh = "firmware_moh"
    log_drop = "log_drop"


class SftpAccount(SftpBase):
    __tablename__ = "sftp_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    auth_type: Mapped[AuthType] = mapped_column(
        Enum(AuthType, name="sftp_auth_type"), nullable=False, default=AuthType.password
    )
    preset: Mapped[SftpPreset | None] = mapped_column(
        Enum(SftpPreset, name="sftp_preset"), nullable=True
    )
    quota_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    retention: Mapped[RetentionPolicy | None] = relationship(
        "RetentionPolicy", back_populates="account", uselist=False, cascade="all, delete-orphan"
    )


class RetentionPolicy(SftpBase):
    __tablename__ = "sftp_retention_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sftp_accounts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    keep_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keep_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    account: Mapped[SftpAccount] = relationship("SftpAccount", back_populates="retention")
