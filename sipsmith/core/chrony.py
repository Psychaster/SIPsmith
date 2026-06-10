"""Chrony NTP service management — core service, not a plugin."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

CHRONY_CONF_PATH = "/etc/chrony/chrony.conf"

CHRONY_CONF_TEMPLATE = """\
# Managed by SIPsmith — do not edit manually.
# stratum {stratum}: SIPsmith acts as the lab's authoritative NTP server.

# Local clock reference (air-gapped lab has no upstream NTP)
local stratum {stratum}

# Serve NTP to lab networks
{allow_lines}

# Basic hardening
driftfile /var/lib/chrony/chrony.drift
rtcsync
makestep 1.0 3
logdir /var/log/chrony
"""


def render_chrony_conf(stratum: int, allow_networks: list[str]) -> str:
    allow_lines = "\n".join(f"allow {net}" for net in allow_networks)
    return CHRONY_CONF_TEMPLATE.format(stratum=stratum, allow_lines=allow_lines)


def chronyc_tracking() -> dict[str, str]:
    """Parse `chronyc tracking` into a dict. Returns empty dict on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/bin/chronyc", "tracking"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return {}
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                out[key.strip()] = val.strip()
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}


def time_sanity_ok(max_offset_seconds: float = 60.0) -> bool:
    """Return True if chrony reports the clock is within acceptable bounds."""
    tracking = chronyc_tracking()
    raw = tracking.get("System time", "")
    # Format: "0.000123456 seconds slow" or "0.000123456 seconds fast"
    try:
        offset = abs(float(raw.split()[0]))
        return offset < max_offset_seconds
    except (IndexError, ValueError):
        return False
