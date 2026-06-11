"""DNS plugin — BIND9 lifecycle management."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.dns")

SIPSMITH_CONF = "/etc/bind/sipsmith.conf"
NAMED_CONF_LOCAL = "/etc/bind/named.conf.local"
_INCLUDE_LINE = 'include "/etc/bind/sipsmith.conf";\n'
DNS_ZONES_DIR = Path("/var/lib/sipsmith/dns/zones")


class DnsPlugin(SipsmithPlugin):
    meta: PluginMeta

    async def install(self, ctx: PluginContext) -> None:
        from sipsmith.database import engine
        from sipsmith_dns.models import DnsBase

        async with engine.begin() as conn:
            await conn.run_sync(DnsBase.metadata.create_all)

        DNS_ZONES_DIR.mkdir(parents=True, exist_ok=True)
        log.info("DNS plugin tables and zone directory created")

    async def enable(self, ctx: PluginContext) -> None:
        await self._write_initial_conf(ctx)
        await self._ensure_include(ctx)
        await ctx.ufw_allow(53, "tcp")
        await ctx.ufw_allow(53, "udp")
        await ctx.audit("enable")
        log.info("DNS plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        await self._remove_include(ctx)
        await ctx.ufw_delete(53, "tcp")
        await ctx.ufw_delete(53, "udp")
        await ctx.audit("disable")
        log.info("DNS plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            result = await ctx.systemctl("status", "named")
            named_ok = result.get("ok", False)
        except Exception:  # noqa: BLE001
            named_ok = False

        configured = Path(SIPSMITH_CONF).exists()
        if named_ok and configured:
            return PluginStatus(
                plugin_id="dns", status=ServiceStatus.ok, enabled=True, detail="named running"
            )
        if not configured:
            return PluginStatus(
                plugin_id="dns",
                status=ServiceStatus.degraded,
                enabled=False,
                detail="not configured",
            )
        return PluginStatus(
            plugin_id="dns",
            status=ServiceStatus.error,
            enabled=True,
            detail="named not running",
        )

    def api_router(self) -> APIRouter | None:
        from sipsmith_dns.api import router

        return router

    async def backup(self, ctx: PluginContext) -> dict:
        import datetime
        import shutil

        ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
        dst = Path("/var/lib/sipsmith/backups") / f"dns-{ts}.tar.gz"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.make_archive(
            base_name=str(dst).removesuffix(".tar.gz"),
            format="gztar",
            root_dir="/",
            base_dir="var/lib/sipsmith/dns",
        )
        return {"backup": str(dst)}

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(label="DNS", url="/plugins/dns", icon="🌐"),
        ]

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _write_initial_conf(self, ctx: PluginContext) -> None:
        from sipsmith_dns.zones import _SIPSMITH_CONF_HEADER

        try:
            await ctx._agent.named_apply_config(_SIPSMITH_CONF_HEADER, {})
        except Exception:  # noqa: BLE001
            # On a dev box without named, fall back to write_config
            await ctx._agent.write_config(SIPSMITH_CONF, _SIPSMITH_CONF_HEADER)

    async def _ensure_include(self, ctx: PluginContext) -> None:
        p = Path(NAMED_CONF_LOCAL)
        current = p.read_text(encoding="utf-8") if p.exists() else ""
        if _INCLUDE_LINE not in current:
            await ctx._agent.write_config(NAMED_CONF_LOCAL, current + "\n" + _INCLUDE_LINE)
        try:
            await ctx.systemctl("reload", "named")
        except Exception:  # noqa: BLE001
            log.debug("named reload skipped (not running yet)")

    async def _remove_include(self, ctx: PluginContext) -> None:
        p = Path(NAMED_CONF_LOCAL)
        if not p.exists():
            return
        current = p.read_text(encoding="utf-8")
        updated = current.replace(_INCLUDE_LINE, "")
        if updated != current:
            await ctx._agent.write_config(NAMED_CONF_LOCAL, updated)
        try:
            await ctx.systemctl("reload", "named")
        except Exception:  # noqa: BLE001
            log.debug("named reload skipped")
