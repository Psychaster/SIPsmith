"""Records Landing plugin — lifecycle management."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.records")


class RecordsPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_records.models import RecordsBase

        async with engine.begin() as conn:
            await conn.run_sync(RecordsBase.metadata.create_all)
        log.info("Records plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        await ctx.audit("enable")
        log.info("Records plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        await ctx.audit("disable")
        log.info("Records plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            from sqlalchemy import func

            from sipsmith.database import AsyncSessionLocal
            from sipsmith_records.models import CallRecord, RecordSource

            async with AsyncSessionLocal() as db:
                src_count = await db.scalar(func.count(RecordSource.id)) or 0
                rec_count = await db.scalar(func.count(CallRecord.id)) or 0
            detail = f"{src_count} source(s) · {rec_count:,} records"
            return PluginStatus(
                plugin_id="records",
                status=ServiceStatus.ok,
                enabled=True,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="records",
                status=ServiceStatus.unknown,
                enabled=True,
                detail="DB unavailable",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_records.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="Records", path="/plugins/records", icon="📞"),
        ]

    async def backup(self, ctx: PluginContext) -> None:
        log.debug("Records plugin has no file backup (DB-only state)")
