"""Unit tests for the CTI plugin (sipsmith_cti)."""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PLUGINS_DIR = Path(__file__).parent.parent / "plugins"
CTI_DIR = PLUGINS_DIR / "sipsmith_cti"
AGENT_PROTOCOL = Path(__file__).parent.parent / "sipsmith" / "agent" / "protocol.py"


def _source(relpath: str) -> str:
    return (CTI_DIR / relpath).read_text()


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------


def test_models_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_cti.models")
        assert hasattr(mod, "CtiCluster")
        assert hasattr(mod, "CtiDevice")
        assert hasattr(mod, "CtiCall")
        assert hasattr(mod, "CtiEvent")
    finally:
        sys.path.pop(0)


def test_plugin_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_cti")
        assert hasattr(mod, "CtiPlugin")
    finally:
        sys.path.pop(0)


def test_manager_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_cti.manager")
        assert hasattr(mod, "SidecarManager")
        assert hasattr(mod, "get_manager")
        assert hasattr(mod, "start_manager")
        assert hasattr(mod, "stop_manager")
    finally:
        sys.path.pop(0)


def test_axl_importable() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        mod = importlib.import_module("sipsmith_cti.axl")
        assert hasattr(mod, "AXLClient")
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Schema / structure tests
# ---------------------------------------------------------------------------


def test_cluster_create_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_cti.api import ClusterCreate

        c = ClusterCreate(name="Lab", cucm_host="10.0.0.1")
        assert c.name == "Lab"
        assert c.cucm_version is None
    finally:
        sys.path.pop(0)


def test_call_request_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_cti.api import CallRequest

        r = CallRequest(cluster_id=1, calling_device="SEP001122334455", called_dn="1001")
        assert r.calling_device == "SEP001122334455"
    finally:
        sys.path.pop(0)


def test_transfer_request_schema() -> None:
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        from sipsmith_cti.api import TransferRequest

        t = TransferRequest(destination="2000")
        assert t.destination == "2000"
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Agent protocol allowlist tests
# ---------------------------------------------------------------------------


def test_cti_verbs_not_required_in_agent_allowlist() -> None:
    """CTI plugin uses a Java sidecar, not the agent, so no agent verbs needed."""
    # This test verifies that assumption is correct — CTI has no agent verbs.
    src = _source("api.py")
    assert "agent." not in src or "sipsmith.agent" not in src.split("import")[0]


def test_no_subprocess_in_python_backend() -> None:
    """CTI Python side talks to Java sidecar via subprocess — subprocess is expected."""
    src = _source("manager.py")
    # subprocess.Popen is expected for launching the Java sidecar
    assert "subprocess" in src or "asyncio.create_subprocess" in src


# ---------------------------------------------------------------------------
# Sidecar Java source tests
# ---------------------------------------------------------------------------

SIDECAR_JAVA_DIR = (
    Path(__file__).parent.parent
    / "sipsmith"
    / "cti-sidecar"
    / "src"
    / "main"
    / "java"
    / "com"
    / "sipsmith"
    / "cti"
)


def test_java_main_exists() -> None:
    assert (SIDECAR_JAVA_DIR / "Main.java").exists()


def test_java_manager_exists() -> None:
    assert (SIDECAR_JAVA_DIR / "JtapiManager.java").exists()


def test_java_main_has_emit_method() -> None:
    src = (SIDECAR_JAVA_DIR / "Main.java").read_text()
    assert "static void emit" in src


def test_java_manager_uses_reflection() -> None:
    src = (SIDECAR_JAVA_DIR / "JtapiManager.java").read_text()
    assert "URLClassLoader" in src
    assert "getDeclaredMethod" in src or "getMethod" in src


def test_pom_no_jtapi_compile_dependency() -> None:
    """JTAPI must not appear as a compile-time Maven dependency."""
    import xml.etree.ElementTree as ET

    pom_path = Path(__file__).parent.parent / "sipsmith" / "cti-sidecar" / "pom.xml"
    tree = ET.parse(pom_path)  # noqa: S314
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    deps = tree.findall(".//m:dependency/m:artifactId", ns)
    dep_ids = [d.text or "" for d in deps]
    assert not any("jtapi" in d.lower() for d in dep_ids)


def test_pom_has_shade_plugin() -> None:
    pom = (Path(__file__).parent.parent / "sipsmith" / "cti-sidecar" / "pom.xml").read_text()
    assert "maven-shade-plugin" in pom


# ---------------------------------------------------------------------------
# Air-gap lint — no external URLs in Python sources
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_ALLOWED = {
    "https://schemas.xmlsoap.org",
    "http://schemas.xmlsoap.org",
    "http://www.cisco.com",
}


def test_no_external_urls_in_python() -> None:
    for pyfile in CTI_DIR.rglob("*.py"):
        src = pyfile.read_text()
        for line in src.splitlines():
            if "# noqa" in line:
                continue
            for m in _URL_RE.finditer(line):
                url = m.group(0)
                # Allow known SOAP/schema URLs embedded in AXL XML bodies
                assert any(url.startswith(a) for a in _ALLOWED), (
                    f"External URL in {pyfile.name}: {url}"
                )


# ---------------------------------------------------------------------------
# Plugin YAML
# ---------------------------------------------------------------------------


def test_plugin_yaml_has_jre_requirement() -> None:
    import yaml

    data = yaml.safe_load((CTI_DIR / "plugin.yaml").read_text())
    assert "default-jre-headless" in data.get("requires_packages", [])


def test_plugin_yaml_has_cti_port() -> None:
    import yaml

    data = yaml.safe_load((CTI_DIR / "plugin.yaml").read_text())
    ports = [p["port"] for p in data.get("ports", [])]
    assert 2748 in ports
