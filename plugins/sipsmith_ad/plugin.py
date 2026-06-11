from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.ad")

_SMB_CONF = Path("/etc/samba/smb.conf")


class AdPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_ad.models import AdBase

        async with engine.begin() as conn:
            await conn.run_sync(AdBase.metadata.create_all)
        log.info("AD plugin tables created")

    async def enable(self, ctx: PluginContext) -> None:
        await ctx.ufw_allow(88, "tcp")
        await ctx.ufw_allow(88, "udp")
        await ctx.ufw_allow(389, "tcp")
        await ctx.ufw_allow(636, "tcp")
        await ctx.ufw_allow(445, "tcp")
        await ctx.audit("enable")
        try:
            await ctx.systemctl("enable", "samba-ad-dc")
        except Exception:  # noqa: BLE001
            log.debug("samba-ad-dc enable skipped (not installed yet)")

    async def disable(self, ctx: PluginContext) -> None:
        await ctx.ufw_delete(88, "tcp")
        await ctx.ufw_delete(88, "udp")
        await ctx.ufw_delete(389, "tcp")
        await ctx.ufw_delete(636, "tcp")
        await ctx.ufw_delete(445, "tcp")
        await ctx.audit("disable")
        try:
            await ctx.systemctl("stop", "samba-ad-dc")
        except Exception:  # noqa: BLE001
            log.debug("samba-ad-dc stop skipped (not running)")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            samba_running = False
            try:
                result = await ctx.systemctl("status", "samba-ad-dc")
                samba_running = result.get("ok", False)
            except Exception:  # noqa: BLE001, S110
                pass

            if not _SMB_CONF.exists():
                return PluginStatus(
                    plugin_id="ad",
                    status=ServiceStatus.disabled,
                    enabled=False,
                    detail="not provisioned",
                )

            from sqlalchemy import select

            from sipsmith.database import AsyncSessionLocal
            from sipsmith_ad.models import AdDomain

            realm = None
            async with AsyncSessionLocal() as db:
                row = await db.scalar(select(AdDomain).where(AdDomain.id == 1))
                if row and row.provisioned:
                    realm = row.realm

            detail = f"realm: {realm}" if realm else "smb.conf present, not provisioned in DB"
            status = ServiceStatus.ok if samba_running else ServiceStatus.degraded
            return PluginStatus(plugin_id="ad", status=status, enabled=True, detail=detail)
        except Exception:  # noqa: BLE001
            return PluginStatus(
                plugin_id="ad",
                status=ServiceStatus.unknown,
                enabled=True,
                detail="status unavailable",
            )

    def api_router(self) -> APIRouter | None:
        from sipsmith_ad.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="Active Directory", path="/plugins/ad", icon="🏢"),
        ]

    async def backup(self, ctx: PluginContext) -> Path | None:
        import datetime
        import shutil
        import subprocess

        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
        dst = Path("/var/lib/sipsmith/backups") / f"ad-{ts}.tar.gz"
        dst.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(  # noqa: S603
            ["/usr/bin/which", "samba-tool"],
            capture_output=True,
        )
        if result.returncode == 0 and _SMB_CONF.exists():
            backup_dir = Path("/var/lib/sipsmith/backups/ad-online")
            backup_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(  # noqa: S603
                [
                    "/usr/bin/samba-tool",
                    "domain",
                    "backup",
                    "online",
                    "--targetdir",
                    str(backup_dir),
                    "--server",
                    "localhost",
                ],
                capture_output=True,
                timeout=300,
            )
            shutil.make_archive(
                base_name=str(dst).removesuffix(".tar.gz"),
                format="gztar",
                root_dir=str(backup_dir.parent),
                base_dir=backup_dir.name,
            )
        elif _SMB_CONF.exists():
            shutil.make_archive(
                base_name=str(dst).removesuffix(".tar.gz"),
                format="gztar",
                root_dir="/",
                base_dir="etc/samba/smb.conf",
            )
        else:
            return None

        return dst
