"""Async client for sipsmith-agent Unix socket."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sipsmith.agent.protocol import ALLOWED_WRITE_PREFIXES, AgentMessage, AgentResponse
from sipsmith.config import get_settings

log = logging.getLogger(__name__)


class AgentError(Exception):
    pass


class AgentClient:
    def __init__(self, socket_path: str | None = None) -> None:
        self._socket = socket_path or get_settings().agent.socket

    async def _call(self, verb: str, params: dict[str, Any]) -> Any:
        msg = AgentMessage(verb, params)
        try:
            reader, writer = await asyncio.open_unix_connection(self._socket)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise AgentError(f"Cannot connect to sipsmith-agent at {self._socket}: {exc}") from exc

        try:
            writer.write(msg.encode())
            await writer.drain()
            raw = await reader.readline()
            resp = AgentResponse.decode(raw)
        finally:
            writer.close()
            await writer.wait_closed()

        if not resp.ok:
            raise AgentError(f"Agent error for {verb}: {resp.error}")
        return resp.result

    async def systemctl(self, verb: str, unit: str) -> dict[str, Any]:
        allowed_verbs = {"start", "stop", "restart", "reload", "enable", "disable", "status"}
        if verb not in allowed_verbs:
            raise ValueError(f"systemctl verb '{verb}' not allowed")
        return await self._call(f"systemctl.{verb}", {"unit": unit})

    async def write_config(self, path: str, content: str) -> None:
        p = str(path)
        if not any(p.startswith(prefix) for prefix in ALLOWED_WRITE_PREFIXES):
            raise ValueError(f"Path '{p}' is not in the agent write allowlist")
        await self._call("write_config", {"path": p, "content": content})

    async def ufw_allow(self, port: int, proto: str = "tcp") -> None:
        await self._call("ufw.allow", {"port": port, "proto": proto})

    async def ufw_delete(self, port: int, proto: str = "tcp") -> None:
        await self._call("ufw.delete", {"port": port, "proto": proto})

    async def ufw_status(self) -> list[dict[str, Any]]:
        return await self._call("ufw.status", {})

    async def chrony_status(self) -> dict[str, Any]:
        return await self._call("chrony.status", {})

    # ── SFTP / sshd ───────────────────────────────────────────────────────

    async def sftp_add_account(
        self,
        username: str,
        password_hash: str = "",
        auth_type: str = "password",
    ) -> dict[str, Any]:
        """Create an SFTP system user with chroot directory layout."""
        return await self._call(
            "sftp.add_account",
            {"username": username, "password_hash": password_hash, "auth_type": auth_type},
        )

    async def sftp_delete_account(self, username: str) -> dict[str, Any]:
        """Delete an SFTP system user and chroot directory tree."""
        return await self._call("sftp.delete_account", {"username": username})

    async def sftp_set_password(self, username: str, password: str) -> dict[str, Any]:
        """Set (or change) an SFTP user's password via chpasswd."""
        return await self._call("sftp.set_password", {"username": username, "password": password})

    async def sftp_set_authorized_keys(self, username: str, keys_content: str) -> dict[str, Any]:
        """Write authorized_keys for an SFTP user."""
        return await self._call(
            "sftp.set_authorized_keys",
            {"username": username, "keys_content": keys_content},
        )

    async def sshd_apply_config(
        self,
        config_content: str,
        config_path: str = "/etc/ssh/sshd_config.d/sipsmith-sftp.conf",
    ) -> dict[str, Any]:
        """Validate sshd config then write and reload atomically."""
        return await self._call(
            "sshd.apply_config",
            {"config_content": config_content, "config_path": config_path},
        )

    async def sshd_validate(
        self,
        config_content: str,
        config_path: str = "/etc/ssh/sshd_config.d/sipsmith-sftp.conf",
    ) -> dict[str, Any]:
        """Validate an sshd config snippet without applying it."""
        return await self._call(
            "sshd.validate",
            {"config_content": config_content, "config_path": config_path},
        )

    # ── DNS / BIND9 ───────────────────────────────────────────────────────

    async def named_apply_config(
        self,
        zones_conf_content: str,
        zone_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate-then-write sipsmith.conf + zone files, then reload named."""
        return await self._call(
            "named.apply_config",
            {"zones_conf_content": zones_conf_content, "zone_files": zone_files or {}},
        )

    async def named_apply_zone(self, zone_name: str, zone_content: str) -> dict[str, Any]:
        """Validate-then-write a single zone file and reload that zone."""
        return await self._call(
            "named.apply_zone",
            {"zone_name": zone_name, "zone_content": zone_content},
        )
