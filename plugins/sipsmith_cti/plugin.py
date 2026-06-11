"""CTI (JTAPI) plugin — lifecycle management."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.cti")


class CtiPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_cti.models import CtiBase

        async with engine.begin() as conn:
            await conn.run_sync(CtiBase.metadata.create_all)
        log.info("CTI plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        await ctx.audit("enable")
        # CTI has no system service; the sidecar is managed per-cluster by SidecarManager.
        log.info("CTI plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        from sipsmith_cti.manager import get_all_managers

        await ctx.audit("disable")
        managers = get_all_managers()
        for mgr in list(managers.values()):
            await mgr.stop()
        log.info("CTI plugin disabled; all sidecar processes stopped")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            from sqlalchemy import func, select

            from sipsmith.database import AsyncSessionLocal
            from sipsmith_cti.models import CtiCluster

            async with AsyncSessionLocal() as db:
                cluster_count = await db.scalar(func.count(CtiCluster.id)) or 0
                connected_count = (
                    await db.scalar(
                        select(func.count(CtiCluster.id)).where(CtiCluster.status == "connected")
                    )
                    or 0
                )

            if cluster_count == 0:
                return PluginStatus(
                    plugin_id="cti",
                    status=ServiceStatus.ok,
                    enabled=True,
                    detail="No clusters configured",
                )
            if connected_count > 0:
                return PluginStatus(
                    plugin_id="cti",
                    status=ServiceStatus.ok,
                    enabled=True,
                    detail=(f"{connected_count}/{cluster_count} cluster(s) connected"),
                )
            return PluginStatus(
                plugin_id="cti",
                status=ServiceStatus.warn,
                enabled=True,
                detail=(f"{cluster_count} cluster(s) configured but none connected"),
            )
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="cti",
                status=ServiceStatus.unknown,
                enabled=True,
                detail="DB unavailable",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_cti.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="CTI Control", path="/plugins/cti", icon="📞"),
        ]

    async def backup(self, ctx: PluginContext) -> None:
        log.debug("CTI plugin: no file backup (DB-only state)")
