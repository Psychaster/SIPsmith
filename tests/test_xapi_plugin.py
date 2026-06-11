"""Unit tests for the xAPI plugin (sipsmith_xapi)."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLUGINS_DIR = Path(__file__).parent.parent / "plugins"
XAPI_DIR = PLUGINS_DIR / "sipsmith_xapi"


def _source(relpath: str) -> str:
    return (XAPI_DIR / relpath).read_text()


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------


def test_models_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_xapi.models")
        assert hasattr(mod, "XapiDevice")
        assert hasattr(mod, "XapiCommandLog")
    finally:
        sys.path.pop(0)


def test_plugin_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_xapi")
        assert hasattr(mod, "XapiPlugin")
    finally:
        sys.path.pop(0)


def test_xapi_client_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_xapi.xapi_client")
        assert hasattr(mod, "XapiClient")
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# XapiClient interface tests
# ---------------------------------------------------------------------------


def test_xapi_client_has_required_methods() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.xapi_client import XapiClient

        for method in (
            "dial",
            "hangup",
            "hold",
            "resume",
            "dtmf",
            "volume_set",
            "get_system_info",
            "get_active_calls",
        ):
            assert hasattr(XapiClient, method), f"XapiClient missing method: {method}"
    finally:
        sys.path.pop(0)


def test_xapi_client_uses_verify_false() -> None:
    """Lab mode: self-signed certs must be accepted."""
    src = _source("xapi_client.py")
    assert "verify=False" in src


# ---------------------------------------------------------------------------
# Model field tests
# ---------------------------------------------------------------------------


def test_xapi_device_model_fields() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.models import XapiDevice

        cols = {c.name for c in XapiDevice.__table__.columns}  # type: ignore[attr-defined]
        for required in (
            "id",
            "name",
            "ip_address",
            "username",
            "password",
            "transport",
            "reg_state",
            "poll_enabled",
        ):
            assert required in cols, f"XapiDevice missing column: {required}"
    finally:
        sys.path.pop(0)


def test_xapi_command_log_has_fk() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.models import XapiCommandLog

        fk_cols = {
            str(fk.target_fullname)
            for col in XapiCommandLog.__table__.columns  # type: ignore[attr-defined]
            for fk in col.foreign_keys
        }
        assert any("xapi_devices" in fk for fk in fk_cols)
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# API schema tests
# ---------------------------------------------------------------------------


def test_device_create_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.api import DeviceCreate

        d = DeviceCreate(
            name="BoardRoom", ip_address="10.0.0.50", username="admin", password="secret"
        )
        assert d.transport == "https"
    finally:
        sys.path.pop(0)


def test_dial_request_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.api import DialRequest

        r = DialRequest(number="1001")
        assert r.protocol == "SIP"
        assert r.number == "1001"
    finally:
        sys.path.pop(0)


def test_dtmf_request_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_xapi.api import DTMFRequest

        r = DTMFRequest(digits="1234")
        assert r.digits == "1234"
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Air-gap lint — no external URLs in Python sources
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def test_no_external_urls_in_python() -> None:
    for pyfile in XAPI_DIR.rglob("*.py"):
        src = pyfile.read_text()
        for line in src.splitlines():
            if "# noqa" in line:
                continue
            for m in _URL_RE.finditer(line):
                url = m.group(0)
                # f-strings with variable hosts are fine (device IPs), catch
                # literal hardcoded external domains only
                if not any(c in url for c in ("{", "}")):
                    pytest.fail(f"External URL in {pyfile.name}: {url}")


# ---------------------------------------------------------------------------
# Plugin YAML
# ---------------------------------------------------------------------------


def test_plugin_yaml_valid() -> None:
    import yaml

    data = yaml.safe_load((XAPI_DIR / "plugin.yaml").read_text())
    assert data["id"] == "xapi"
    assert "XapiPlugin" in data["entry_point"]


def test_plugin_yaml_no_required_packages() -> None:
    """xAPI plugin has no system package deps (uses httpx from Python deps)."""
    import yaml

    data = yaml.safe_load((XAPI_DIR / "plugin.yaml").read_text())
    assert not data.get("requires_packages")


# ---------------------------------------------------------------------------
# UTC timestamp discipline
# ---------------------------------------------------------------------------


def test_models_use_utc_not_utcnow() -> None:
    src = _source("models.py")
    assert "utcnow" not in src
    assert "datetime.UTC" in src or "timezone.utc" in src


def test_api_uses_utc_not_utcnow() -> None:
    src = _source("api.py")
    assert "utcnow" not in src
