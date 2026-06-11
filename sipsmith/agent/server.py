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


# ── SFTP / sshd handlers ──────────────────────────────────────────────────

SFTP_CHROOT_BASE = "/var/lib/sipsmith/sftp"
SFTP_GROUP = "sipsmith-sftp"


def _sftp_chroot(username: str) -> Path:
    return Path(SFTP_CHROOT_BASE) / username


def _handle_sftp_add_account(params: dict) -> AgentResponse:
    """Create system user for SFTP chroot, set up directory tree."""
    username = params.get("username", "")
    password_hash = params.get("password_hash", "")
    auth_type = params.get("auth_type", "password")

    if not username or not username.isidentifier():
        return AgentResponse(ok=False, error=f"Invalid username: {username!r}")

    chroot = _sftp_chroot(username)
    upload = chroot / "upload"
    ssh_dir = chroot / ".ssh"

    try:
        # Create system user (no shell, no home creation — we manage dirs manually)
        result = subprocess.run(  # noqa: S603
            [
                "/usr/sbin/useradd",
                "--system",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "--gid",
                SFTP_GROUP,
                "--home-dir",
                str(chroot),
                username,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return AgentResponse(ok=False, error=f"useradd failed: {result.stderr.strip()}")

        # Look up UID after creation
        pw = pwd.getpwnam(username)
        uid = pw.pw_uid
        gid = grp.getgrnam(SFTP_GROUP).gr_gid

        # chroot root: root:root 0755 (sshd requirement)
        chroot.mkdir(parents=True, exist_ok=True)
        os.chown(str(chroot), 0, 0)
        chroot.chmod(0o755)

        # upload dir: username:sipsmith-sftp 0770
        upload.mkdir(exist_ok=True)
        os.chown(str(upload), uid, gid)
        upload.chmod(0o770)

        # .ssh dir only for key-based auth
        if auth_type in ("key", "both"):
            ssh_dir.mkdir(exist_ok=True)
            os.chown(str(ssh_dir), uid, uid)
            ssh_dir.chmod(0o700)
            ak = ssh_dir / "authorized_keys"
            ak.touch(exist_ok=True)
            os.chown(str(ak), uid, uid)
            ak.chmod(0o600)

        # Set password if provided
        if password_hash and auth_type in ("password", "both"):
            # password_hash is a pre-hashed shadow entry (from the web process)
            result2 = subprocess.run(  # noqa: S603
                ["/usr/sbin/usermod", "--password", password_hash, username],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result2.returncode != 0:
                return AgentResponse(
                    ok=False, error=f"usermod --password failed: {result2.stderr.strip()}"
                )

        return AgentResponse(ok=True, result={"username": username, "uid": uid})
    except Exception as exc:
        return AgentResponse(ok=False, error=str(exc))


def _handle_sftp_delete_account(params: dict) -> AgentResponse:
    """Delete system user and remove their chroot directory."""
    username = params.get("username", "")
    if not username or not username.isidentifier():
        return AgentResponse(ok=False, error=f"Invalid username: {username!r}")

    try:
        # Remove system user (no home removal; we handle dirs ourselves)
        result = subprocess.run(  # noqa: S603
            ["/usr/sbin/userdel", username],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode not in (0, 6):  # 6 = user does not exist (already gone)
            return AgentResponse(ok=False, error=f"userdel failed: {result.stderr.strip()}")

        # Remove chroot directory tree
        import shutil

        chroot = _sftp_chroot(username)
        if chroot.exists():
            shutil.rmtree(str(chroot))

        return AgentResponse(ok=True, result={"username": username})
    except Exception as exc:
        return AgentResponse(ok=False, error=str(exc))


def _handle_sftp_set_password(params: dict) -> AgentResponse:
    """Change a user's password via chpasswd (plaintext → hashed by chpasswd)."""
    username = params.get("username", "")
    password = params.get("password", "")
    if not username or not username.isidentifier():
        return AgentResponse(ok=False, error=f"Invalid username: {username!r}")
    if not password:
        return AgentResponse(ok=False, error="Password must not be empty")

    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/sbin/chpasswd"],
            input=f"{username}:{password}\n",
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return AgentResponse(ok=False, error=f"chpasswd failed: {result.stderr.strip()}")
        return AgentResponse(ok=True, result={"username": username})
    except Exception as exc:
        return AgentResponse(ok=False, error=str(exc))


def _handle_sftp_set_authorized_keys(params: dict) -> AgentResponse:
    """Write authorized_keys file for an SFTP user."""
    username = params.get("username", "")
    keys_content = params.get("keys_content", "")
    if not username or not username.isidentifier():
        return AgentResponse(ok=False, error=f"Invalid username: {username!r}")

    try:
        pw = pwd.getpwnam(username)
        uid = pw.pw_uid
        chroot = _sftp_chroot(username)
        ssh_dir = chroot / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chown(str(ssh_dir), uid, uid)
        ssh_dir.chmod(0o700)
        ak = ssh_dir / "authorized_keys"
        ak.write_text(keys_content, encoding="utf-8")
        os.chown(str(ak), uid, uid)
        ak.chmod(0o600)
        return AgentResponse(ok=True, result={"username": username})
    except Exception as exc:
        return AgentResponse(ok=False, error=str(exc))


def _handle_sshd_validate(config_content: str, config_path: str) -> AgentResponse:
    """Write a temporary sshd config and validate it with sshd -t."""
    import tempfile

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".conf",
            dir="/tmp",  # noqa: S108
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(config_content)
            tmp_path = tmp.name

        result = subprocess.run(  # noqa: S603
            ["/usr/sbin/sshd", "-t", "-f", "/dev/null", "-o", f"Include={tmp_path}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return AgentResponse(
            ok=result.returncode == 0,
            result={"stdout": result.stdout, "stderr": result.stderr},
            error=result.stderr.strip() if result.returncode != 0 else None,
        )
    except Exception as exc:
        return AgentResponse(ok=False, error=str(exc))
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 S110
                log.debug("Could not remove temp file %s", tmp_path)


def _handle_sshd_apply_config(params: dict) -> AgentResponse:
    """Validate-then-write sshd drop-in config and reload sshd."""
    config_content = params.get("config_content", "")
    config_path = params.get("config_path", "/etc/ssh/sshd_config.d/sipsmith-sftp.conf")

    # Validate against allowlist
    if not any(config_path.startswith(p) for p in ALLOWED_WRITE_PREFIXES):
        return AgentResponse(ok=False, error=f"Config path '{config_path}' not in write allowlist")

    # Validate first
    validate_resp = _handle_sshd_validate(config_content, config_path)
    if not validate_resp.ok:
        return AgentResponse(
            ok=False,
            error=f"sshd config validation failed: {validate_resp.error}",
        )

    # Write and reload
    write_resp = _handle_write_config(config_path, config_content)
    if not write_resp.ok:
        return write_resp

    reload_resp = _handle_systemctl("reload", "ssh")
    if not reload_resp.ok:
        # Try restart if reload fails (e.g. service wasn't running)
        reload_resp = _handle_systemctl("restart", "ssh")
    return reload_resp


def _dispatch(verb: str, params: dict) -> AgentResponse:
    if verb.startswith("systemctl."):
        return _handle_systemctl(verb[len("systemctl.") :], params.get("unit", ""))
    if verb == "write_config":
        return _handle_write_config(params.get("path", ""), params.get("content", ""))
    if verb.startswith("ufw."):
        return _handle_ufw(verb[len("ufw.") :], params)
    if verb == "chrony.status":
        return _handle_chrony_status(params)
    if verb == "sftp.add_account":
        return _handle_sftp_add_account(params)
    if verb == "sftp.delete_account":
        return _handle_sftp_delete_account(params)
    if verb == "sftp.set_password":
        return _handle_sftp_set_password(params)
    if verb == "sftp.set_authorized_keys":
        return _handle_sftp_set_authorized_keys(params)
    if verb == "sshd.apply_config":
        return _handle_sshd_apply_config(params)
    if verb == "sshd.validate":
        return _handle_sshd_validate(
            params.get("config_content", ""), params.get("config_path", "")
        )
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
