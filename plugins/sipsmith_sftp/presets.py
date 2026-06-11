"""SFTP preset definitions with exact CUCM-side values."""

from __future__ import annotations

from dataclasses import dataclass, field

from sipsmith_sftp.models import SftpPreset


@dataclass(frozen=True)
class PresetDef:
    id: SftpPreset
    name: str
    description: str
    default_username_prefix: str
    cucm_config: dict = field(default_factory=dict)  # field_name -> value or placeholder
    default_keep_count: int | None = None


PRESETS: dict[SftpPreset, PresetDef] = {
    SftpPreset.drs_backup: PresetDef(
        id=SftpPreset.drs_backup,
        name="CUCM DRS Backup Target",
        description=(
            "Receives scheduled Disaster Recovery System backup archives from CUCM. "
            "Point CUCM's DRS Backup Device at this account."
        ),
        default_username_prefix="drs-",
        cucm_config={
            "Device Name": "(any descriptive name, e.g. SIPsmith-DRS)",
            "Host name/IP Address": "{fqdn}",
            "Path name": "/upload",
            "User name": "{username}",
            "Password": "(account password)",
            "Port Number": "22",
        },
        default_keep_count=5,
    ),
    SftpPreset.firmware_moh: PresetDef(
        id=SftpPreset.firmware_moh,
        name="Firmware / MoH Staging",
        description=(
            "Drop zone for firmware images, Music on Hold audio files, and TFTP-served content."
        ),
        default_username_prefix="staging-",
        cucm_config={
            "Host name/IP Address": "{fqdn}",
            "Path": "/upload",
            "User name": "{username}",
            "Password": "(account password)",
            "Port": "22",
        },
    ),
    SftpPreset.log_drop: PresetDef(
        id=SftpPreset.log_drop,
        name="Log Drop",
        description="General-purpose log collection target for CUCM, CMS, or Expressway.",
        default_username_prefix="logs-",
        cucm_config={
            "Host name/IP Address": "{fqdn}",
            "Path": "/upload",
            "User name": "{username}",
            "Password": "(account password)",
            "Port": "22",
        },
    ),
}


def render_cucm_config(preset: PresetDef, fqdn: str, username: str) -> dict[str, str]:
    """Return preset CUCM config with placeholders filled in."""
    return {
        k: v.replace("{fqdn}", fqdn).replace("{username}", username)
        for k, v in preset.cucm_config.items()
    }
