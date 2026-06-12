"""Plugin SDK — ABC, manifest model, status types."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel, model_validator

if TYPE_CHECKING:
    from sipsmith.sdk.context import PluginContext


class ServiceStatus(enum.StrEnum):
    ok = "ok"
    degraded = "degraded"
    error = "error"
    disabled = "disabled"
    unknown = "unknown"


class PortDeclaration(BaseModel):
    port: int
    proto: str = "tcp"  # tcp | udp | both

    # §2.4 — accept a bare integer in plugin.yaml as a convenience: `ports: [5080, 5081]`.
    # The full object form is still preferred when a non-tcp proto is needed.
    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_int(cls, value):
        if isinstance(value, int):
            return {"port": value}
        return value


class PluginMeta(BaseModel):
    id: str
    name: str
    version: str
    api_version: int = 1
    description: str = ""
    requires_packages: list[str] = []
    requires_plugins: list[str] = []
    ports: list[PortDeclaration] = []
    entry_point: str  # "module:ClassName"


class PluginStatus(BaseModel):
    plugin_id: str
    status: ServiceStatus
    enabled: bool
    detail: str = ""


class UiPage(BaseModel):
    title: str
    path: str  # URL path, e.g. "/plugins/dns"
    icon: str = ""  # icon name or SVG snippet for nav


class SipsmithPlugin(ABC):
    """Base class for all SIPsmith plugins."""

    meta: PluginMeta

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def install(self, ctx: PluginContext) -> None:  # noqa: B027
        """Install system packages and create initial directory/config layout."""

    @abstractmethod
    async def enable(self, ctx: PluginContext) -> None:
        """Enable and start the underlying service."""

    @abstractmethod
    async def disable(self, ctx: PluginContext) -> None:
        """Stop the underlying service (without removing data)."""

    @abstractmethod
    async def status(self, ctx: PluginContext) -> PluginStatus:
        """Return live service health status."""

    async def apply_config(self, ctx: PluginContext) -> None:  # noqa: B027
        """Render config templates, validate, reload service."""

    # ── FastAPI integration ────────────────────────────────────────────────

    def api_router(self) -> APIRouter | None:
        """Return an APIRouter to be mounted at /api/v1/plugins/{id}."""
        return None

    def ui_pages(self) -> list[UiPage]:
        """Return nav entries and template routes for the GUI."""
        return []

    # ── Backup / restore ──────────────────────────────────────────────────

    async def backup(self, ctx: PluginContext) -> Path | None:
        """Produce a backup archive; return its path."""
        return None

    async def restore(self, ctx: PluginContext, path: Path) -> None:  # noqa: B027
        """Restore from a backup archive."""
