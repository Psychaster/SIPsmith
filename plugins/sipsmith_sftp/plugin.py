"""SFTP plugin — lifecycle implementation."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter

from sipsmith.sdk.context import PluginContext
from sipsmith.sdk.plugin import PluginMeta, PluginStatus, ServiceStatus, SipsmithPlugin, UiPage

log = logging.getLogger("sipsmith.plugin.sftp")

SSHD_SNIPPET_PATH = "/etc/ssh/sshd_config.d/sipsmith-sftp.conf"
SFTP_GROUP = "sipsmith-sftp"


class SftpPlugin(SipsmithPlugin):
    meta: PluginMeta

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def install(self, ctx: PluginContext) -> None:  # noqa: B027
        """Create DB tables and ensure sipsmith-sftp group exists."""
        import subprocess

        from sipsmith.database import engine
        from sipsmith_sftp.models import SftpBase

        # Create plugin-owned tables via the async engine
        async with engine.begin() as conn:
            await conn.run_sync(SftpBase.metadata.create_all)

        log.info("SFTP plugin tables created")

        # Ensure sipsmith-sftp group exists (best-effort)
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/getent", "group", SFTP_GROUP],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            log.info("sipsmith-sftp group will be created on enable")

    async def enable(self, ctx: PluginContext) -> None:
        """Write sshd snippet, validate, reload SSH."""
        await self._ensure_group(ctx)
        await self._apply_sshd_config(ctx)
        await ctx.ufw_allow(22, "tcp")
        await ctx.audit("enable")
        log.info("SFTP plugin enabled")

    async def disable(self, ctx: PluginContext) -> None:
        """Remove sshd snippet and reload SSH."""
        try:
            await ctx._agent.sshd_apply_config(
                "# SIPsmith SFTP plugin — disabled\n", SSHD_SNIPPET_PATH
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not remove sshd snippet on disable: %s", exc)
        await ctx.ufw_delete(22, "tcp")
        await ctx.audit("disable")
        log.info("SFTP plugin disabled")

    async def status(self, ctx: PluginContext) -> PluginStatus:
        try:
            result = await ctx.systemctl("status", "ssh")
            sshd_ok = result.get("ok", False)
        except Exception:  # noqa: BLE001
            sshd_ok = False

        snippet = Path(SSHD_SNIPPET_PATH)
        configured = snippet.exists() and snippet.stat().st_size > 0

        if sshd_ok and configured:
            return PluginStatus(
                plugin_id="sftp", status=ServiceStatus.ok, enabled=True, detail="sshd running"
            )
        if not configured:
            return PluginStatus(
                plugin_id="sftp",
                status=ServiceStatus.disabled,
                enabled=False,
                detail="not configured",
            )
        return PluginStatus(
            plugin_id="sftp", status=ServiceStatus.error, enabled=True, detail="sshd not running"
        )

    async def apply_config(self, ctx: PluginContext) -> None:  # noqa: B027
        """Re-render and apply sshd config snippet."""
        await self._apply_sshd_config(ctx)

    # ── API / UI ──────────────────────────────────────────────────────────

    def api_router(self) -> APIRouter:
        from sipsmith_sftp.api import router

        return router

    def ui_pages(self) -> list[UiPage]:
        return [
            UiPage(title="SFTP", path="/plugins/sftp", icon="📂"),
        ]

    # ── Backup ────────────────────────────────────────────────────────────

    async def backup(self, ctx: PluginContext) -> Path | None:
        import shutil
        import tempfile

        import yaml
        from sqlalchemy import select

        from sipsmith_sftp.models import SftpAccount

        result = await ctx.db.execute(select(SftpAccount))
        accounts = result.scalars().all()

        backup_dir = ctx.data_dir / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)

        meta = [
            {
                "username": a.username,
                "display_name": a.display_name,
                "auth_type": str(a.auth_type),
                "preset": str(a.preset) if a.preset else None,
                "quota_mb": a.quota_mb,
                "enabled": a.enabled,
            }
            for a in accounts
        ]
        (backup_dir / "accounts.yaml").write_text(yaml.dump(meta))

        archive = Path(tempfile.mktemp(suffix=".tar.gz"))  # noqa: S306
        shutil.make_archive(str(archive).removesuffix(".tar.gz"), "gztar", backup_dir)
        return archive

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _ensure_group(self, ctx: PluginContext) -> None:
        # §15.2 — groupadd is a privileged operation; route it through the root agent
        # instead of trying to run it in-process (the web app is non-root).
        result = await ctx._agent.sftp_ensure_group()
        if not result.get("existed", True):
            log.info("Created system group: %s", SFTP_GROUP)

    async def _apply_sshd_config(self, ctx: PluginContext) -> None:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
        from sqlalchemy import select

        from sipsmith_sftp.models import AuthType, SftpAccount

        result = await ctx.db.execute(select(SftpAccount).where(SftpAccount.enabled))
        accounts = result.scalars().all()
        password_auth_enabled = any(
            a.auth_type in (AuthType.password, AuthType.both) for a in accounts
        )

        # Render template — daemon config files (not HTML), autoescape off intentionally
        here = Path(__file__).parent
        env = Environment(
            loader=FileSystemLoader(str(here / "templates")),
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701
        )
        tpl = env.get_template("sipsmith-sftp.conf.j2")
        config = tpl.render(password_auth_enabled=password_auth_enabled)

        await ctx._agent.sshd_apply_config(config, SSHD_SNIPPET_PATH)
        log.info("sshd config applied (validate-then-reload)")
