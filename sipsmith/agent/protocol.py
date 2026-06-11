"""Wire protocol for sipsmith-agent: newline-delimited JSON over Unix socket."""

from __future__ import annotations

import json
from typing import Any

# Allowlisted verbs — the agent rejects anything not in this set.
ALLOWED_VERBS: frozenset[str] = frozenset(
    [
        "systemctl.start",
        "systemctl.stop",
        "systemctl.restart",
        "systemctl.reload",
        "systemctl.enable",
        "systemctl.disable",
        "systemctl.status",
        "write_config",
        "ufw.allow",
        "ufw.delete",
        "ufw.status",
        "chrony.status",
        # SFTP / sshd verbs (Phase 1)
        "sftp.add_account",
        "sftp.delete_account",
        "sftp.set_password",
        "sftp.set_authorized_keys",
        "sshd.apply_config",
        "sshd.validate",
        # DNS / BIND9 verbs (Phase 3)
        "named.apply_config",
        "named.apply_zone",
    ]
)

# Write-only paths the agent will accept for write_config.
# Plugins must declare the paths they need; the agent enforces this list.
ALLOWED_WRITE_PREFIXES: tuple[str, ...] = (
    "/etc/chrony/",
    "/etc/bind/",
    "/etc/ssh/sshd_config.d/sipsmith",
    "/etc/samba/smb.conf",
    "/var/lib/sipsmith/",
)


class AgentMessage:
    def __init__(self, verb: str, params: dict[str, Any]) -> None:
        if verb not in ALLOWED_VERBS:
            raise ValueError(f"Verb '{verb}' is not in the agent allowlist")
        self.verb = verb
        self.params = params

    def encode(self) -> bytes:
        return (json.dumps({"verb": self.verb, "params": self.params}) + "\n").encode()

    @classmethod
    def decode(cls, data: bytes) -> AgentMessage:
        obj = json.loads(data.decode().strip())
        return cls(obj["verb"], obj.get("params", {}))


class AgentResponse:
    def __init__(self, ok: bool, result: Any = None, error: str | None = None) -> None:
        self.ok = ok
        self.result = result
        self.error = error

    def encode(self) -> bytes:
        return (
            json.dumps({"ok": self.ok, "result": self.result, "error": self.error}) + "\n"
        ).encode()

    @classmethod
    def decode(cls, data: bytes) -> AgentResponse:
        obj = json.loads(data.decode().strip())
        return cls(obj["ok"], obj.get("result"), obj.get("error"))
