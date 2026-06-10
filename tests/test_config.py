"""Settings loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sipsmith.config import Settings, _load_yaml


def test_settings_defaults():
    s = Settings()
    assert s.server.port == 8443
    assert s.security.session_max_age == 86400
    assert s.chrony.stratum == 3


def test_settings_partial_override():
    s = Settings.model_validate({"server": {"port": 9000, "fqdn": "mybox.lab"}})
    assert s.server.port == 9000
    assert s.server.fqdn == "mybox.lab"
    assert s.server.host == "0.0.0.0"  # default preserved


def test_settings_from_yaml(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "server": {"fqdn": "test.local", "port": 9443},
                "security": {"secret_key": "abc123"},
            }
        )
    )
    data = _load_yaml(str(config_file))
    s = Settings.model_validate(data)
    assert s.server.fqdn == "test.local"
    assert s.server.port == 9443
    assert s.security.secret_key == "abc123"


def test_load_yaml_missing_file():
    data = _load_yaml("/nonexistent/path/config.yaml")
    assert data == {}


def test_invalid_log_level():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"logging": {"level": "VERBOSE"}})


def test_chrony_allow_networks():
    s = Settings.model_validate({"chrony": {"allow_networks": ["192.168.1.0/24", "10.0.0.0/8"]}})
    assert "192.168.1.0/24" in s.chrony.allow_networks
