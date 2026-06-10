"""Plugin SDK tests — manifest parsing, loader, dependency ordering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sipsmith.sdk.loader import PluginLoader, PluginLoadError
from sipsmith.sdk.plugin import PluginMeta, ServiceStatus

# ── PluginMeta validation ─────────────────────────────────────────────────


def test_plugin_meta_minimal():
    meta = PluginMeta(id="dns", name="DNS", version="1.0.0", entry_point="mod:Cls")
    assert meta.api_version == 1
    assert meta.requires_plugins == []


def test_plugin_meta_full():
    raw = {
        "id": "ca",
        "name": "Certificate Authority",
        "version": "1.0.0",
        "api_version": 1,
        "requires_packages": ["openssl"],
        "requires_plugins": [],
        "ports": [{"port": 80, "proto": "tcp"}],
        "entry_point": "sipsmith_ca:CaPlugin",
    }
    meta = PluginMeta.model_validate(raw)
    assert meta.ports[0].port == 80


def test_plugin_meta_missing_id_raises():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PluginMeta.model_validate({"name": "Missing ID", "version": "1.0.0", "entry_point": "x:X"})


# ── PluginLoader — manifest discovery ────────────────────────────────────


def _write_plugin(tmp_path: Path, plugin_id: str, requires: list[str] | None = None) -> Path:
    """Write a minimal plugin directory to tmp_path."""
    plugin_dir = tmp_path / plugin_id
    plugin_dir.mkdir()

    manifest = {
        "id": plugin_id,
        "name": plugin_id.upper(),
        "version": "0.1.0",
        "entry_point": f"tests.fake_plugins.{plugin_id}:FakePlugin",
        "requires_plugins": requires or [],
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest))
    return plugin_dir


def test_loader_no_dirs():
    loader = PluginLoader()
    loader.discover([])
    assert loader.all() == {}


def test_loader_invalid_manifest(tmp_path: Path):
    p = tmp_path / "bad_plugin"
    p.mkdir()
    (p / "plugin.yaml").write_text("id: missing_entry_point\n")

    loader = PluginLoader()
    loader.discover([tmp_path])  # should log error and skip, not raise
    assert "missing_entry_point" not in loader.all()


# ── Topological sort ──────────────────────────────────────────────────────


def test_topo_sort_no_deps():
    loader = PluginLoader()
    metas = {
        "a": PluginMeta(id="a", name="A", version="1.0", entry_point="x:X"),
        "b": PluginMeta(id="b", name="B", version="1.0", entry_point="x:X"),
    }
    result = loader._topo_sort(metas)
    assert set(result) == {"a", "b"}


def test_topo_sort_with_deps():
    loader = PluginLoader()
    metas = {
        "dns": PluginMeta(id="dns", name="DNS", version="1.0", entry_point="x:X"),
        "ad": PluginMeta(
            id="ad", name="AD", version="1.0", entry_point="x:X", requires_plugins=["dns"]
        ),
    }
    result = loader._topo_sort(metas)
    assert result.index("dns") < result.index("ad")


def test_topo_sort_circular_raises():
    loader = PluginLoader()
    metas = {
        "a": PluginMeta(id="a", name="A", version="1.0", entry_point="x:X", requires_plugins=["b"]),
        "b": PluginMeta(id="b", name="B", version="1.0", entry_point="x:X", requires_plugins=["a"]),
    }
    with pytest.raises(PluginLoadError, match="[Cc]ircular"):
        loader._topo_sort(metas)


# ── ServiceStatus enum ────────────────────────────────────────────────────


def test_service_status_values():
    assert ServiceStatus.ok == "ok"
    assert ServiceStatus.error == "error"
