"""UC Cert Orchestrator plugin — lifecycle management."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.uc_certs")


class UcCertsPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_uc_certs.models import UcCertsBase

        async with engine.begin() as conn:
            await conn.run_sync(UcCertsBase.metadata.create_all)
        log.info("UC Certs plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        await ctx.audit("enable")
        log.info("UC Certs plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        await ctx.audit("disable")
        log.info("UC Certs plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            from sqlalchemy import func

            from sipsmith.database import AsyncSessionLocal
            from sipsmith_uc_certs.models import UcCluster

            async with AsyncSessionLocal() as db:
                count = await db.scalar(func.count(UcCluster.id))
            detail = f"{count or 0} cluster(s) configured"
            return PluginStatus(
                plugin_id="uc-certs",
                status=ServiceStatus.ok,
                enabled=True,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="uc-certs",
                status=ServiceStatus.unknown,
                enabled=True,
                detail="DB unavailable",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_uc_certs.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="UC Certs", path="/plugins/uc-certs", icon="🔐"),
        ]

    async def backup(self, ctx: PluginContext) -> None:
        log.debug("UC Certs plugin has no file backup (DB-only state)")
