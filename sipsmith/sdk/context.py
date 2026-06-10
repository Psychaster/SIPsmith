"""Plugin context object — controlled access to platform services."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sipsmith.agent.client import AgentClient
    from sipsmith.core.audit import AuditWriter
    from sipsmith.sdk.loader import PluginLoader


class PluginContext:
    """
    Passed to every plugin lifecycle call. Gives plugins controlled access to
    platform services without importing them directly from core.
    """

    def __init__(
        self,
        plugin_id: str,
        db: AsyncSession,
        agent: AgentClient,
        audit: AuditWriter,
        registry: PluginLoader,
        template_dirs: list[Path],
        data_dir: Path,
    ) -> None:
        self.plugin_id = plugin_id
        self.db = db
        self._agent = agent
        self._audit = audit
        self._registry = registry
        self._data_dir = data_dir
        self._jinja = Environment(
            loader=FileSystemLoader([str(d) for d in template_dirs]),
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701 — renders daemon config files, not HTML
        )

    # ── Template rendering ─────────────────────────────────────────────────

    def render(self, template_name: str, **kwargs: Any) -> str:
        tpl = self._jinja.get_template(template_name)
        return tpl.render(**kwargs)

    # ── Systemd via agent ─────────────────────────────────────────────────

    async def systemctl(self, verb: str, unit: str) -> dict[str, Any]:
        return await self._agent.systemctl(verb, unit)

    async def write_config(self, path: str | Path, content: str) -> None:
        """Write a config file via the agent (bypasses web-process privilege)."""
        await self._agent.write_config(str(path), content)

    # ── Firewall ──────────────────────────────────────────────────────────

    async def ufw_allow(self, port: int, proto: str = "tcp") -> None:
        await self._agent.ufw_allow(port, proto)

    async def ufw_delete(self, port: int, proto: str = "tcp") -> None:
        await self._agent.ufw_delete(port, proto)

    # ── Audit ─────────────────────────────────────────────────────────────

    async def audit(
        self,
        action: str,
        resource: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        actor: str = "system",
    ) -> None:
        await self._audit.write(
            actor=actor,
            action=f"{self.plugin_id}.{action}",
            resource=resource,
            old_value=old_value,
            new_value=new_value,
        )

    # ── Plugin-to-plugin API ──────────────────────────────────────────────

    def get_plugin(self, plugin_id: str) -> Any:
        """Retrieve another loaded plugin's instance for direct API calls."""
        return self._registry.get(plugin_id)

    # ── Data dir ─────────────────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        """Plugin-private persistent data directory under /var/lib/sipsmith/."""
        d = self._data_dir / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d
