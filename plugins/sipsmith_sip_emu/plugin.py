"""SIP Endpoint Emulator plugin — lifecycle management."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.sip_emu")


class SipEmuPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_sip_emu.models import SipEmuBase

        async with engine.begin() as conn:
            await conn.run_sync(SipEmuBase.metadata.create_all)
        log.info("SIP Emulator plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        from sipsmith.database import get_settings
        from sipsmith_sip_emu.worker.manager import init_manager

        await ctx.audit("enable")
        settings = get_settings()
        await init_manager(settings.database_url)
        log.info("SIP Emulator plugin enabled; worker started")

    async def disable(self, ctx: PluginContext) -> None:
        from sipsmith_sip_emu.worker.manager import get_manager

        await ctx.audit("disable")
        mgr = get_manager()
        if mgr is not None:
            await mgr.stop()
        log.info("SIP Emulator plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            from sqlalchemy import func

            from sipsmith.database import AsyncSessionLocal
            from sipsmith_sip_emu.models import Endpoint, EndpointCall
            from sipsmith_sip_emu.worker.manager import get_manager

            mgr = get_manager()
            worker_ok = mgr is not None and mgr.is_available

            async with AsyncSessionLocal() as db:
                ep_count = await db.scalar(func.count(Endpoint.id)) or 0
                active_calls = (
                    await db.scalar(
                        func.count(EndpointCall.id).where(
                            EndpointCall.call_state.notin_(["disconnected"])
                        )
                    )
                    or 0
                )

            if not worker_ok:
                mgr_status = mgr.status if mgr else "stopped"
                if mgr_status == "pjsua2_unavailable":
                    detail = "pjsua2 not built — run scripts/build-pjsua2.sh"
                    svc = ServiceStatus.warn
                else:
                    detail = f"Worker {mgr_status} · {ep_count} endpoint(s) configured"
                    svc = ServiceStatus.warn
            else:
                detail = f"{ep_count} endpoint(s) · {active_calls} active call(s)"
                svc = ServiceStatus.ok

            return PluginStatus(
                plugin_id="sip-emu",
                status=svc,
                enabled=True,
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="sip-emu",
                status=ServiceStatus.unknown,
                enabled=True,
                detail="DB unavailable",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_sip_emu.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="SIP Emulator", path="/plugins/sip-emu", icon="📡"),
        ]

    async def backup(self, ctx: PluginContext) -> None:
        log.debug("SIP Emulator: no file backup (DB-only state)")
