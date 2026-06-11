"""Plugin loader — discovers, validates, and dependency-orders plugins."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from sipsmith.sdk.plugin import PluginMeta, SipsmithPlugin

log = logging.getLogger(__name__)


class PluginLoadError(Exception):
    pass


class PluginLoader:
    def __init__(self) -> None:
        self._plugins: dict[str, SipsmithPlugin] = {}
        self._metas: dict[str, PluginMeta] = {}

    def discover(self, plugin_dirs: list[Path]) -> None:
        """Scan plugin directories, load manifests, and instantiate plugins."""
        raw_metas: dict[str, PluginMeta] = {}
        raw_instances: dict[str, SipsmithPlugin] = {}

        for base_dir in plugin_dirs:
            for manifest_path in sorted(base_dir.glob("*/plugin.yaml")):
                plugin_dir = manifest_path.parent
                try:
                    meta, instance = self._load_one(manifest_path)
                    raw_metas[meta.id] = meta
                    raw_instances[meta.id] = instance
                except PluginLoadError as exc:
                    log.error("Skipping plugin at %s: %s", plugin_dir, exc)

        # Topological sort by requires_plugins
        ordered = self._topo_sort(raw_metas)
        for plugin_id in ordered:
            self._metas[plugin_id] = raw_metas[plugin_id]
            self._plugins[plugin_id] = raw_instances[plugin_id]
            log.info("Loaded plugin: %s v%s", plugin_id, raw_metas[plugin_id].version)

    def _load_one(self, manifest_path: Path) -> tuple[PluginMeta, SipsmithPlugin]:
        with manifest_path.open() as fh:
            raw = yaml.safe_load(fh)
        try:
            meta = PluginMeta.model_validate(raw)
        except ValidationError as exc:
            raise PluginLoadError(f"Invalid manifest: {exc}") from exc

        module_name, class_name = meta.entry_point.rsplit(":", 1)

        # Add plugin's parent directory to sys.path so that a plugin package
        # named e.g. "sipsmith_sftp" (living in plugins/sftp/__init__.py) can
        # be imported by inserting the plugins/ directory on the path.
        plugin_parent = str(manifest_path.parent.parent)
        if plugin_parent not in sys.path:
            sys.path.insert(0, plugin_parent)

        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, class_name)
        except (ImportError, AttributeError) as exc:
            raise PluginLoadError(f"Cannot load entry point {meta.entry_point}: {exc}") from exc

        if not issubclass(cls, SipsmithPlugin):
            raise PluginLoadError(f"{meta.entry_point} is not a SipsmithPlugin subclass")

        instance: SipsmithPlugin = cls()
        instance.meta = meta
        return meta, instance

    def _topo_sort(self, metas: dict[str, PluginMeta]) -> list[str]:
        """Kahn's algorithm for dependency-ordered loading."""
        in_degree: dict[str, int] = {pid: 0 for pid in metas}
        dependents: dict[str, list[str]] = {pid: [] for pid in metas}

        for pid, meta in metas.items():
            for dep in meta.requires_plugins:
                if dep not in metas:
                    log.warning(
                        "Plugin %s requires missing plugin %s — skipping dependency", pid, dep
                    )
                    continue
                in_degree[pid] += 1
                dependents[dep].append(pid)

        queue = [pid for pid, deg in in_degree.items() if deg == 0]
        result: list[str] = []
        while queue:
            pid = queue.pop(0)
            result.append(pid)
            for dependent in dependents[pid]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(metas):
            raise PluginLoadError("Circular dependency detected in plugin manifests")
        return result

    def get(self, plugin_id: str) -> SipsmithPlugin | None:
        return self._plugins.get(plugin_id)

    def all(self) -> dict[str, SipsmithPlugin]:
        return dict(self._plugins)

    def meta(self, plugin_id: str) -> PluginMeta | None:
        return self._metas.get(plugin_id)
