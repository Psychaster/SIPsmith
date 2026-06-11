"""CA plugin — lifecycle implementation."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.ca")


class CaPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:  # noqa: B027
        from sipsmith.database import engine
        from sipsmith_ca.models import CaBase

        async with engine.begin() as conn:
            await conn.run_sync(CaBase.metadata.create_all)
        log.info("CA plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        from sipsmith_ca.crypto import is_initialized

        if not is_initialized():
            log.info("CA not yet initialized — skipping enable (use /setup to generate)")
        await ctx.audit("enable")
        log.info("CA plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        await ctx.audit("disable")
        log.info("CA plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        from sipsmith_ca.crypto import ISSUING_CERT_PATH, is_initialized

        if not is_initialized():
            return PluginStatus(
                plugin_id="ca",
                status=ServiceStatus.disabled,
                enabled=False,
                detail="CA not initialized — use Setup to generate",
            )

        try:
            from cryptography import x509

            issuing = x509.load_pem_x509_certificate(ISSUING_CERT_PATH.read_bytes())
            import datetime

            days_left = (issuing.not_valid_after_utc - datetime.datetime.now(datetime.UTC)).days
            detail = f"Issuing CA expires in {days_left}d"
            status = ServiceStatus.ok if days_left > 30 else ServiceStatus.degraded
        except Exception as exc:  # noqa: BLE001
            return PluginStatus(
                plugin_id="ca", status=ServiceStatus.error, enabled=True, detail=str(exc)
            )

        return PluginStatus(plugin_id="ca", status=status, enabled=True, detail=detail)

    def api_router(self) -> APIRouter:
        from sipsmith_ca.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [UiPage(title="Certificate Authority", path="/plugins/ca", icon="🔐")]

    async def backup(self, ctx: PluginContext) -> Path | None:
        import shutil
        import tempfile

        from sipsmith_ca.crypto import CA_DIR

        if not CA_DIR.exists():
            return None
        archive = Path(tempfile.mktemp(suffix=".tar.gz"))  # noqa: S306
        shutil.make_archive(str(archive).removesuffix(".tar.gz"), "gztar", CA_DIR)
        return archive
