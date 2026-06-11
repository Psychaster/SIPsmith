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


NAMED_CONF_PATH = "/etc/bind/sipsmith.conf"
NAMED_CONF_LOCAL = "/etc/bind/named.conf.local"
DNS_ZONES_DIR = "/var/lib/sipsmith/dns/zones"


def _handle_named_apply_config(params: dict) -> AgentResponse:
    """Validate-then-write sipsmith.conf + zone files, then reload named."""
    import shutil
    import tempfile

    zones_conf = params.get("zones_conf_content", "")
    zone_files: dict[str, str] = params.get("zone_files", {})

    zones_dir = Path(DNS_ZONES_DIR)
    zones_dir.mkdir(parents=True, exist_ok=True)

    # Write zone files to temp paths first for validation
    tmp_zone_paths: dict[str, str] = {}
    try:
        for zone_name, content in zone_files.items():
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".zone",
                dir="/tmp",
                delete=False,
                encoding="utf-8",  # noqa: S108
            ) as tmp:
                tmp.write(content)
                tmp_zone_paths[zone_name] = tmp.name

        # Validate each zone with named-checkzone
        for zone_name, tmp_path in tmp_zone_paths.items():
            r = subprocess.run(  # noqa: S603
                ["/usr/sbin/named-checkzone", zone_name, tmp_path],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode != 0:
                return AgentResponse(
                    ok=False,
                    error=f"named-checkzone failed for {zone_name}: {r.stderr.strip()}",
                )

        # Move validated zone files to final locations
        for zone_name, tmp_path in tmp_zone_paths.items():
            final = zones_dir / f"{zone_name}.zone"
            shutil.move(tmp_path, str(final))
            final.chmod(0o644)
            tmp_zone_paths[zone_name] = ""  # mark as moved

        # Backup existing sipsmith.conf
        conf_path = Path(NAMED_CONF_PATH)
        backup_content = conf_path.read_text(encoding="utf-8") if conf_path.exists() else None

        # Write new sipsmith.conf
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        conf_path.write_text(zones_conf, encoding="utf-8")
        conf_path.chmod(0o644)

        # Validate full config
        r = subprocess.run(  # noqa: S603
            ["/usr/sbin/named-checkconf", "/etc/bind/named.conf"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            # Restore backup
            if backup_content is not None:
                conf_path.write_text(backup_content, encoding="utf-8")
            else:
                conf_path.unlink(missing_ok=True)
            return AgentResponse(ok=False, error=f"named-checkconf failed: {r.stderr.strip()}")

        # Reload named
        return _named_reload()

    finally:
        # Clean up any remaining temp files that weren't moved
        for tmp_path in tmp_zone_paths.values():
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)


def _handle_named_apply_zone(params: dict) -> AgentResponse:
    """Validate-then-write a single zone file and reload that zone."""
    zone_name = params.get("zone_name", "")
    zone_content = params.get("zone_content", "")
    if not zone_name:
        return AgentResponse(ok=False, error="zone_name required")

    import tempfile

    zones_dir = Path(DNS_ZONES_DIR)
    zones_dir.mkdir(parents=True, exist_ok=True)
    final = zones_dir / f"{zone_name}.zone"

    backup_content = final.read_text(encoding="utf-8") if final.exists() else None

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".zone",
            dir="/tmp",
            delete=False,
            encoding="utf-8",  # noqa: S108
        ) as tmp:
            tmp.write(zone_content)
            tmp_path = tmp.name

        r = subprocess.run(  # noqa: S603
            ["/usr/sbin/named-checkzone", zone_name, tmp_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            return AgentResponse(
                ok=False,
                error=f"named-checkzone failed: {r.stderr.strip()}",
            )

        import shutil

        shutil.move(tmp_path, str(final))
        final.chmod(0o644)
        tmp_path = None

        # Try rndc reload of specific zone
        r2 = subprocess.run(  # noqa: S603
            ["/usr/sbin/rndc", "reload", zone_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r2.returncode != 0:
            return _named_reload()
        return AgentResponse(ok=True, result={"zone": zone_name})

    except Exception as exc:
        if backup_content is not None:
            final.write_text(backup_content, encoding="utf-8")
        return AgentResponse(ok=False, error=str(exc))
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def _named_reload() -> AgentResponse:
    """Try rndc reload first, fall back to systemctl reload named."""
    r = subprocess.run(  # noqa: S603
        ["/usr/sbin/rndc", "reload"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode == 0:
        return AgentResponse(ok=True, result={"method": "rndc"})
    return _handle_systemctl("reload", "named")


# ── Active Directory / Samba handlers ────────────────────────────────────────

_SAMBA_TOOL = "/usr/bin/samba-tool"
_SAMBA_DB = "/var/lib/samba/private/sam.ldb"
_SAMBA_PRIVATE = "/var/lib/samba/private"
_SAMBA_TLS = "/var/lib/samba/private/tls"
_AD_KEY_FILE = "/var/lib/sipsmith/ad/admin.key"
_NAMED_AD_DLZ = "/etc/bind/named.conf.ad-dlz"


def _samba_tool(*args: str, timeout: int = 300) -> AgentResponse:
    """Run samba-tool with given args, return AgentResponse."""
    cmd = [_SAMBA_TOOL, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return AgentResponse(
            ok=False,
            error=f"samba-tool {' '.join(args[:2])} failed: {detail}",
        )
    return AgentResponse(ok=True, result={"stdout": result.stdout.strip()})


def _handle_ad_provision(params: dict) -> AgentResponse:
    import re

    realm = params.get("realm", "").upper()
    netbios = params.get("netbios", "").upper()
    adminpass = params.get("adminpass", "")
    dsrm_pass = params.get("dsrm_pass", "")

    if not all([realm, netbios, adminpass, dsrm_pass]):
        return AgentResponse(ok=False, error="realm, netbios, adminpass, dsrm_pass all required")
    if not re.match(r"^[A-Z][A-Z0-9\-]*(\.[A-Z][A-Z0-9\-]*)+$", realm):
        return AgentResponse(ok=False, error=f"Invalid realm: {realm!r}")
    if not re.match(r"^[A-Z][A-Z0-9\-]{0,14}$", netbios):
        return AgentResponse(ok=False, error=f"Invalid NetBIOS name: {netbios!r}")
    if len(adminpass) < 8:
        return AgentResponse(ok=False, error="adminpass must be at least 8 characters")

    r = subprocess.run(  # noqa: S603
        [
            _SAMBA_TOOL,
            "domain",
            "provision",
            f"--realm={realm}",
            f"--domain={netbios}",
            f"--adminpass={adminpass}",
            f"--dsrm-password={dsrm_pass}",
            "--dns-backend=BIND9_DLZ",
            "--use-rfc2307",
            "--server-role=dc",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        return AgentResponse(ok=False, error=f"samba-tool domain provision failed:\n{r.stderr}")

    # Store admin pass for LDAP operations (root-only file)
    key_dir = Path(_AD_KEY_FILE).parent
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chown(str(key_dir), 0, 0)
    key_dir.chmod(0o700)
    key_file = Path(_AD_KEY_FILE)
    key_file.write_text(adminpass, encoding="utf-8")
    os.chown(str(key_file), 0, 0)
    key_file.chmod(0o600)

    # Fix Samba private dir permissions for BIND9 DLZ
    private = Path(_SAMBA_PRIVATE)
    if private.exists():
        try:
            bind_gid = grp.getgrnam("bind").gr_gid
            for p in [
                private / "named.conf",
                private / "named.txt",
                private / "dns",
                private / "dns.keytab",
            ]:
                if p.exists():
                    os.chown(str(p), 0, bind_gid)
                    p.chmod(0o640 if p.is_file() else 0o750)
        except KeyError:
            pass  # bind group may not exist yet

    return AgentResponse(ok=True, result={"stdout": r.stdout.strip()})


def _handle_ad_deprovision(_params: dict) -> AgentResponse:
    import shutil

    for svc in ("samba-ad-dc", "smbd", "nmbd", "winbind"):
        subprocess.run(["/usr/bin/systemctl", "stop", svc], capture_output=True, timeout=30)  # noqa: S603

    for path in (
        "/var/lib/samba",
        "/etc/samba/smb.conf",
        "/etc/bind/named.conf.ad-dlz",
        _AD_KEY_FILE,
    ):
        p = Path(path)
        if p.exists():
            if p.is_dir():
                shutil.rmtree(str(p))
            else:
                p.unlink()

    return AgentResponse(ok=True, result={"deprovisioned": True})


def _handle_ad_info(_params: dict) -> AgentResponse:
    r = subprocess.run(  # noqa: S603
        [_SAMBA_TOOL, "domain", "info", "127.0.0.1"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return AgentResponse(
        ok=r.returncode == 0,
        result={"stdout": r.stdout.strip()},
        error=r.stderr.strip() if r.returncode != 0 else None,
    )


def _handle_ad_user_create(params: dict) -> AgentResponse:
    sam = params.get("sam_account", "")
    password = params.get("password", "")
    given = params.get("given_name", "")
    surname = params.get("surname", "")
    telephone = params.get("telephone_number", "")
    userou = params.get("ou", "")

    if not sam or not password:
        return AgentResponse(ok=False, error="sam_account and password are required")

    args = ["user", "create", sam, password]
    if given:
        args += [f"--given-name={given}"]
    if surname:
        args += [f"--surname={surname}"]
    if telephone:
        args += [f"--telephone-number={telephone}"]
    if userou:
        args += [f"--userou={userou}"]

    return _samba_tool(*args)


def _handle_ad_user_delete(params: dict) -> AgentResponse:
    sam = params.get("sam_account", "")
    if not sam:
        return AgentResponse(ok=False, error="sam_account required")
    return _samba_tool("user", "delete", sam)


def _handle_ad_group_create(params: dict) -> AgentResponse:
    name = params.get("name", "")
    desc = params.get("description", "")
    ou = params.get("ou", "")
    if not name:
        return AgentResponse(ok=False, error="name required")
    args = ["group", "add", name]
    if desc:
        args += [f"--description={desc}"]
    if ou:
        args += [f"--groupou={ou}"]
    return _samba_tool(*args)


def _handle_ad_group_delete(params: dict) -> AgentResponse:
    name = params.get("name", "")
    if not name:
        return AgentResponse(ok=False, error="name required")
    return _samba_tool("group", "delete", name)


def _handle_ad_ou_create(params: dict) -> AgentResponse:
    dn = params.get("dn", "")
    desc = params.get("description", "")
    if not dn:
        return AgentResponse(ok=False, error="dn (distinguished name) required")
    args = ["ou", "create", dn]
    if desc:
        args += [f"--description={desc}"]
    return _samba_tool(*args)


def _handle_ad_ou_delete(params: dict) -> AgentResponse:
    dn = params.get("dn", "")
    if not dn:
        return AgentResponse(ok=False, error="dn required")
    return _samba_tool("ou", "delete", dn)


def _handle_ad_ldb_set_attr(params: dict) -> AgentResponse:
    """Set an attribute on an object in Samba LDB via ldbmodify."""
    dn = params.get("dn", "")
    attr = params.get("attr", "")
    value = params.get("value", "")
    if not all([dn, attr]):
        return AgentResponse(ok=False, error="dn and attr required")

    db = Path(_SAMBA_DB)
    if not db.exists():
        return AgentResponse(ok=False, error=f"Samba LDB not found at {_SAMBA_DB}")

    ldif = f"dn: {dn}\nchangetype: modify\nreplace: {attr}\n{attr}: {value}\n"

    r = subprocess.run(  # noqa: S603
        ["/usr/bin/ldbmodify", "-H", str(db)],
        input=ldif,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return AgentResponse(
        ok=r.returncode == 0,
        result={"stdout": r.stdout.strip()},
        error=r.stderr.strip() if r.returncode != 0 else None,
    )


def _handle_ad_set_ldaps_cert(params: dict) -> AgentResponse:
    """Install LDAPS cert+key+ca into Samba's tls directory."""
    cert_pem = params.get("cert_pem", "")
    key_pem = params.get("key_pem", "")
    ca_pem = params.get("ca_pem", "")
    if not all([cert_pem, key_pem]):
        return AgentResponse(ok=False, error="cert_pem and key_pem required")

    tls_dir = Path(_SAMBA_TLS)
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)

    (tls_dir / "cert.pem").write_text(cert_pem, encoding="utf-8")
    (tls_dir / "key.pem").write_text(key_pem, encoding="utf-8")
    (tls_dir / "cert.pem").chmod(0o644)
    (tls_dir / "key.pem").chmod(0o600)
    if ca_pem:
        (tls_dir / "ca.pem").write_text(ca_pem, encoding="utf-8")
        (tls_dir / "ca.pem").chmod(0o644)

    # Restart samba to pick up new cert
    r = _handle_systemctl("restart", "samba-ad-dc")
    return r


def _handle_ad_dlz_bind_include(_params: dict) -> AgentResponse:
    """Write the DLZ include file for BIND and add include line to named.conf.local."""
    named_conf = Path("/var/lib/samba/private/named.conf")
    if not named_conf.exists():
        return AgentResponse(ok=False, error="Samba named.conf not found — domain not provisioned")

    # Write the wrapper include
    dlz_conf = Path(_NAMED_AD_DLZ)
    dlz_conf.write_text(f'include "{named_conf}";\n', encoding="utf-8")
    dlz_conf.chmod(0o644)

    # Ensure named.conf.local includes it
    local = Path(NAMED_CONF_LOCAL)
    include_line = f'include "{_NAMED_AD_DLZ}";\n'
    current = local.read_text(encoding="utf-8") if local.exists() else ""
    if include_line not in current:
        local.write_text(current + "\n" + include_line, encoding="utf-8")

    return _named_reload()


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
    if verb == "named.apply_config":
        return _handle_named_apply_config(params)
    if verb == "named.apply_zone":
        return _handle_named_apply_zone(params)
    if verb == "ad.provision":
        return _handle_ad_provision(params)
    if verb == "ad.deprovision":
        return _handle_ad_deprovision(params)
    if verb == "ad.info":
        return _handle_ad_info(params)
    if verb == "ad.user_create":
        return _handle_ad_user_create(params)
    if verb == "ad.user_delete":
        return _handle_ad_user_delete(params)
    if verb == "ad.group_create":
        return _handle_ad_group_create(params)
    if verb == "ad.group_delete":
        return _handle_ad_group_delete(params)
    if verb == "ad.ou_create":
        return _handle_ad_ou_create(params)
    if verb == "ad.ou_delete":
        return _handle_ad_ou_delete(params)
    if verb == "ad.ldb_set_attr":
        return _handle_ad_ldb_set_attr(params)
    if verb == "ad.set_ldaps_cert":
        return _handle_ad_set_ldaps_cert(params)
    if verb == "ad.dlz_bind_include":
        return _handle_ad_dlz_bind_include(params)
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
