"""
sipsmith-agent — privileged root helper.
Listens on a Unix socket, verifies peer credentials (must be sipsmith user),
executes an allowlisted vocabulary of commands, and returns JSON responses.
"""

from __future__ import annotations

import grp
import logging
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

from sipsmith.agent.protocol import (
    ALLOWED_WRITE_PREFIXES,
    AgentMessage,
    AgentResponse,
)

log = logging.getLogger("sipsmith-agent")
SOCKET_PATH = "/run/sipsmith-agent/agent.sock"
SIPSMITH_USER = "sipsmith"


def _get_peer_uid(conn: socket.socket) -> int:
    """Extract UID of connecting peer via SO_PEERCRED."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = (
        int.from_bytes(creds[0:4], "little"),
        int.from_bytes(creds[4:8], "little"),
        int.from_bytes(creds[8:12], "little"),
    )
    return uid


def _sipsmith_uid() -> int:
    return pwd.getpwnam(SIPSMITH_USER).pw_uid


def _handle_systemctl(verb: str, unit: str) -> AgentResponse:
    allowed = {"start", "stop", "restart", "reload", "enable", "disable", "status"}
    if verb not in allowed:
        return AgentResponse(ok=False, error=f"systemctl verb '{verb}' not allowed")
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/systemctl", verb, unit],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return AgentResponse(
        ok=result.returncode == 0,
        result={"stdout": result.stdout, "stderr": result.stderr},
        error=result.stderr if result.returncode != 0 else None,
    )


def _handle_write_config(path: str, content: str) -> AgentResponse:
    if not any(path.startswith(p) for p in ALLOWED_WRITE_PREFIXES):
        return AgentResponse(ok=False, error=f"Path '{path}' not in write allowlist")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    p.chmod(0o640)
    return AgentResponse(ok=True, result={"path": path})


def _handle_ufw(sub: str, params: dict) -> AgentResponse:
    port = params.get("port")
    proto = params.get("proto", "tcp")
    if sub == "allow":
        cmd = ["/usr/sbin/ufw", "allow", f"{port}/{proto}"]
    elif sub == "delete":
        cmd = ["/usr/sbin/ufw", "delete", "allow", f"{port}/{proto}"]
    elif sub == "status":
        cmd = ["/usr/sbin/ufw", "status", "verbose"]
    else:
        return AgentResponse(ok=False, error=f"Unknown ufw sub-verb {sub}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)  # noqa: S603 S607
    return AgentResponse(
        ok=result.returncode == 0,
        result={"stdout": result.stdout},
        error=result.stderr if result.returncode != 0 else None,
    )


def _handle_chrony_status(_params: dict) -> AgentResponse:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/chronyc", "tracking"], capture_output=True, text=True, timeout=10
    )
    return AgentResponse(
        ok=result.returncode == 0,
        result={"output": result.stdout},
        error=result.stderr if result.returncode != 0 else None,
    )


def _dispatch(verb: str, params: dict) -> AgentResponse:
    if verb.startswith("systemctl."):
        return _handle_systemctl(verb[len("systemctl.") :], params.get("unit", ""))
    if verb == "write_config":
        return _handle_write_config(params.get("path", ""), params.get("content", ""))
    if verb.startswith("ufw."):
        return _handle_ufw(verb[len("ufw.") :], params)
    if verb == "chrony.status":
        return _handle_chrony_status(params)
    return AgentResponse(ok=False, error=f"Unknown verb: {verb}")


def _handle_connection(conn: socket.socket, addr: object) -> None:
    try:
        peer_uid = _get_peer_uid(conn)
        if peer_uid != _sipsmith_uid() and peer_uid != 0:
            log.warning("Rejected connection from UID %d", peer_uid)
            conn.close()
            return

        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk

        if not data.strip():
            conn.close()
            return

        msg = AgentMessage.decode(data)
        log.info("verb=%s params=%s uid=%d", msg.verb, msg.params, peer_uid)
        resp = _dispatch(msg.verb, msg.params)
        conn.sendall(resp.encode())
    except Exception as exc:
        log.exception("Error handling connection: %s", exc)
        try:
            conn.sendall(AgentResponse(ok=False, error=str(exc)).encode())
        except Exception:  # noqa: S110
            pass
    finally:
        conn.close()


def main() -> None:
    if os.geteuid() != 0:
        print("sipsmith-agent must run as root", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    socket_dir = Path(SOCKET_PATH).parent
    socket_dir.mkdir(parents=True, exist_ok=True)
    socket_dir.chmod(0o750)
    sipsmith_gid = grp.getgrnam(SIPSMITH_USER).gr_gid
    os.chown(socket_dir, 0, sipsmith_gid)

    sock_path = Path(SOCKET_PATH)
    if sock_path.exists():
        sock_path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    sock_path.chmod(0o660)
    os.chown(SOCKET_PATH, 0, sipsmith_gid)
    srv.listen(16)

    log.info("sipsmith-agent listening on %s", SOCKET_PATH)

    try:
        while True:
            conn, addr = srv.accept()
            # Synchronous handling is fine for this low-frequency helper.
            _handle_connection(conn, addr)
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
