from __future__ import annotations

import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AdBase(DeclarativeBase):
    pass


class AdDomain(AdBase):
    __tablename__ = "ad_domain"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    realm: Mapped[str] = mapped_column(String(253), nullable=False)
    netbios_name: Mapped[str] = mapped_column(String(15), nullable=False)
    dns_mode: Mapped[str] = mapped_column(String(16), default="dlz", nullable=False)
    provisioned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provisioned_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ldaps_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AdOU(AdBase):
    __tablename__ = "ad_ous"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    dn: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )


class AdGroup(AdBase):
    __tablename__ = "ad_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ou_dn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )


class AdUser(AdBase):
    __tablename__ = "ad_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sam_account: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    telephone_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ou_dn: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )


class AdSyncAccount(AdBase):
    __tablename__ = "ad_sync_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sam_account: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32), default="cucm_sync", nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.datetime.now(datetime.UTC),
        nullable=False,
    )
