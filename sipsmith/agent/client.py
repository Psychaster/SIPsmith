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
