"""Global plugin registry — single instance shared across the app."""

from __future__ import annotations

from pathlib import Path

from sipsmith.sdk.loader import PluginLoader

_loader: PluginLoader | None = None


def get_loader() -> PluginLoader:
    global _loader
    if _loader is None:
        _loader = PluginLoader()
        plugin_dirs = [Path(__file__).parent.parent / "plugins"]
        existing = [d for d in plugin_dirs if d.exists()]
        if existing:
            _loader.discover(existing)
    return _loader


def reset_loader() -> None:
    """For testing only."""
    global _loader
    _loader = None
