"""xAPI plugin — Cisco RoomOS device control lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.xapi")


class XapiPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_xapi.models import XapiBase

        async with engine.begin() as conn:
            await conn.run_sync(XapiBase.metadata.create_all)

        log.info("xAPI plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        await ctx.audit("enable")
        log.info("xAPI plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        await ctx.audit("disable")
        log.info("xAPI plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            from sqlalchemy import func, select

            from sipsmith_xapi.models import XapiDevice

            total = await ctx.db.scalar(func.count(XapiDevice.id)) or 0
            connected = (
                await ctx.db.scalar(
                    select(func.count(XapiDevice.id)).where(XapiDevice.reg_state == "Connected")
                )
                or 0
            )

            if total == 0:
                return PluginStatus(
                    plugin_id="xapi",
                    status=ServiceStatus.disabled,
                    enabled=False,
                    detail="no devices configured",
                )
            if connected > 0:
                return PluginStatus(
                    plugin_id="xapi",
                    status=ServiceStatus.ok,
                    enabled=True,
                    detail=f"{connected}/{total} device(s) connected",
                )
            return PluginStatus(
                plugin_id="xapi",
                status=ServiceStatus.degraded,
                enabled=True,
                detail=f"0/{total} device(s) connected",
            )
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="xapi",
                status=ServiceStatus.unknown,
                enabled=False,
                detail="status query failed",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_xapi.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="xAPI Devices", path="/plugins/xapi", icon="📺"),
        ]

    async def backup(self, ctx: PluginContext) -> Path | None:
        log.debug("xAPI plugin: no file state to back up")
        return None
