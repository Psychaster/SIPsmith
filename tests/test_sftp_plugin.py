"""Phase 1 SFTP plugin tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure plugins/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins"))

from sipsmith_sftp.files import run_retention, safe_path
from sipsmith_sftp.models import AuthType, SftpPreset
from sipsmith_sftp.presets import PRESETS, render_cucm_config

# ── Preset definitions ────────────────────────────────────────────────────


def test_all_presets_defined():
    assert SftpPreset.drs_backup in PRESETS
    assert SftpPreset.firmware_moh in PRESETS
    assert SftpPreset.log_drop in PRESETS


def test_drs_preset_has_required_cucm_fields():
    p = PRESETS[SftpPreset.drs_backup]
    config = render_cucm_config(p, fqdn="sipsmith.lab", username="drs-pub")
    assert "sipsmith.lab" in config["Host name/IP Address"]
    assert "drs-pub" in config["User name"]
    assert config["Port Number"] == "22"
    assert config["Path name"] == "/upload"


def test_all_presets_have_name_and_description():
    for pid, preset in PRESETS.items():
        assert preset.name, f"Preset {pid} missing name"
        assert preset.description, f"Preset {pid} missing description"
        assert preset.cucm_config, f"Preset {pid} missing cucm_config"


def test_render_cucm_config_replaces_placeholders():
    p = PRESETS[SftpPreset.drs_backup]
    config = render_cucm_config(p, fqdn="mybox.lab.local", username="drs-cucm")
    for v in config.values():
        assert "{fqdn}" not in v
        assert "{username}" not in v


# ── Path traversal prevention ─────────────────────────────────────────────


def test_safe_path_normal(tmp_path):
    # Monkey-patch SFTP_BASE for testing
    import sipsmith_sftp.files as files_mod

    orig = files_mod.SFTP_BASE
    files_mod.SFTP_BASE = tmp_path
    try:
        (tmp_path / "alice" / "upload").mkdir(parents=True)
        result = safe_path("alice", "somefile.txt")
        assert str(result).startswith(str(tmp_path / "alice" / "upload"))
    finally:
        files_mod.SFTP_BASE = orig


def test_safe_path_rejects_traversal(tmp_path):
    import sipsmith_sftp.files as files_mod

    orig = files_mod.SFTP_BASE
    files_mod.SFTP_BASE = tmp_path
    try:
        (tmp_path / "alice" / "upload").mkdir(parents=True)
        with pytest.raises(ValueError, match="traversal"):
            safe_path("alice", "../../etc/passwd")
    finally:
        files_mod.SFTP_BASE = orig


def test_safe_path_rejects_absolute_escape(tmp_path):
    import sipsmith_sftp.files as files_mod

    orig = files_mod.SFTP_BASE
    files_mod.SFTP_BASE = tmp_path
    try:
        (tmp_path / "alice" / "upload").mkdir(parents=True)
        with pytest.raises(ValueError, match="traversal"):
            safe_path("alice", "../../../root/.ssh/id_rsa")
    finally:
        files_mod.SFTP_BASE = orig


# ── sshd config template ──────────────────────────────────────────────────


def test_sshd_config_template_renders():
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    template_dir = Path(__file__).parent.parent / "plugins" / "sipsmith_sftp" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701
    )
    tpl = env.get_template("sipsmith-sftp.conf.j2")
    conf = tpl.render(password_auth_enabled=True)
    assert "Match Group sipsmith-sftp" in conf
    assert "ForceCommand internal-sftp" in conf
    assert "ChrootDirectory" in conf
    assert "PasswordAuthentication yes" in conf
    # Air-gap: no external URLs
    assert "http://" not in conf
    assert "https://" not in conf


def test_sshd_config_template_password_disabled():
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    template_dir = Path(__file__).parent.parent / "plugins" / "sipsmith_sftp" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701
    )
    conf = env.get_template("sipsmith-sftp.conf.j2").render(password_auth_enabled=False)
    assert "PasswordAuthentication no" in conf


# ── Retention logic ───────────────────────────────────────────────────────


def test_retention_keep_count(tmp_path):
    import sipsmith_sftp.files as files_mod

    orig = files_mod.SFTP_BASE
    files_mod.SFTP_BASE = tmp_path

    try:
        upload = tmp_path / "alice" / "upload"
        upload.mkdir(parents=True)
        # Create 5 files with different mtimes
        for i in range(5):
            f = upload / f"backup_{i:02d}.tar"
            f.write_text(f"data {i}")
            import os  # noqa: E401
            import time

            os.utime(f, (time.time() - (5 - i) * 3600, time.time() - (5 - i) * 3600))

        deleted = run_retention("alice", keep_count=3, keep_days=None)
        assert len(deleted) == 2
        remaining = list(upload.iterdir())
        assert len(remaining) == 3
    finally:
        files_mod.SFTP_BASE = orig


def test_retention_keep_days(tmp_path):
    import os
    import time

    import sipsmith_sftp.files as files_mod

    orig = files_mod.SFTP_BASE
    files_mod.SFTP_BASE = tmp_path

    try:
        upload = tmp_path / "bob" / "upload"
        upload.mkdir(parents=True)
        # One old file, one recent file
        old = upload / "old.tar"
        old.write_text("old")
        os.utime(old, (time.time() - 10 * 86400, time.time() - 10 * 86400))
        recent = upload / "recent.tar"
        recent.write_text("recent")

        deleted = run_retention("bob", keep_count=None, keep_days=7)
        assert len(deleted) == 1
        assert "old.tar" in deleted[0]
        assert recent.exists()
    finally:
        files_mod.SFTP_BASE = orig


# ── AuthType enum ─────────────────────────────────────────────────────────


def test_auth_type_values():
    assert AuthType.password == "password"
    assert AuthType.key == "key"
    assert AuthType.both == "both"
