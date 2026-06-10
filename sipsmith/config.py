"""Settings loader — reads /etc/sipsmith/config.yaml (or path from env)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator

_CONFIG_PATH_ENV = "SIPSMITH_CONFIG"
_DEFAULT_CONFIG_PATH = "/etc/sipsmith/config.yaml"


class DatabaseSettings(BaseModel):
    url: str = "postgresql+asyncpg://sipsmith:sipsmith@localhost/sipsmith"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8443
    fqdn: str = "localhost"
    tls_cert: str = "/var/lib/sipsmith/tls/gui.crt"
    tls_key: str = "/var/lib/sipsmith/tls/gui.key"


class SecuritySettings(BaseModel):
    secret_key: str = "dev-insecure-key-change-in-production"
    session_max_age: int = 86400
    token_expiry_days: int = 365


class LoggingSettings(BaseModel):
    level: str = "INFO"
    log_dir: str = "/var/log/sipsmith"

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log level must be one of {valid}")
        return upper


class AgentSettings(BaseModel):
    socket: str = "/run/sipsmith-agent/agent.sock"


class ChronySettings(BaseModel):
    enabled: bool = True
    stratum: int = 3
    allow_networks: list[str] = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


class Settings(BaseModel):
    database: DatabaseSettings = DatabaseSettings()
    server: ServerSettings = ServerSettings()
    security: SecuritySettings = SecuritySettings()
    logging: LoggingSettings = LoggingSettings()
    agent: AgentSettings = AgentSettings()
    chrony: ChronySettings = ChronySettings()


def _load_yaml(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    config_path = os.environ.get(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)
    data = _load_yaml(config_path)
    return Settings.model_validate(data)
