"""File browser helpers — path validation and directory listing."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

SFTP_BASE = Path("/var/lib/sipsmith/sftp")


def upload_dir(username: str) -> Path:
    return SFTP_BASE / username / "upload"


def safe_path(username: str, rel_path: str) -> Path:
    """
    Resolve rel_path within the user's upload dir and verify it doesn't
    escape via traversal. Raises ValueError if the resolved path is outside.
    """
    base = upload_dir(username).resolve()
    # Strip any leading slashes so the user can pass "/subdir/file.txt"
    stripped = rel_path.lstrip("/")
    candidate = (base / stripped).resolve()
    if not str(candidate).startswith(str(base)):
        raise ValueError(f"Path traversal detected: {rel_path!r}")
    return candidate


def list_files(username: str, rel_path: str = "") -> list[dict]:
    """Return a list of file-info dicts for the given directory."""
    target = safe_path(username, rel_path)
    if not target.exists():
        return []
    entries = []
    for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name)):
        stat = entry.stat()
        entries.append(
            {
                "name": entry.name,
                "path": str(entry.relative_to(upload_dir(username))),
                "is_dir": entry.is_dir(),
                "size": stat.st_size if entry.is_file() else None,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return entries


def dir_usage_bytes(username: str) -> int:
    """Return total bytes used in the upload directory."""
    base = upload_dir(username)
    if not base.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(base):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def delete_file(username: str, rel_path: str) -> None:
    """Delete a file (not a directory) within the upload dir."""
    target = safe_path(username, rel_path)
    if not target.exists():
        raise FileNotFoundError(f"Not found: {rel_path!r}")
    if target.is_dir():
        raise IsADirectoryError(f"Cannot delete a directory via this endpoint: {rel_path!r}")
    target.unlink()


def run_retention(username: str, keep_count: int | None, keep_days: int | None) -> list[str]:
    """
    Apply retention policy. Returns list of deleted file paths (relative).
    keep_count: keep only the N most-recent files (by mtime).
    keep_days: delete files older than N days.
    Both rules applied independently; a file is deleted if either triggers.
    """

    base = upload_dir(username)
    if not base.exists():
        return []

    # Only act on top-level files (DRS backups are single files or small dirs)
    entries = sorted(
        [e for e in base.iterdir() if e.is_file()],
        key=lambda e: e.stat().st_mtime,
    )
    to_delete: set[Path] = set()

    if keep_count is not None and len(entries) > keep_count:
        excess = entries[: len(entries) - keep_count]
        to_delete.update(excess)

    if keep_days is not None:
        cutoff = datetime.now(tz=UTC).timestamp() - keep_days * 86400
        for e in entries:
            if e.stat().st_mtime < cutoff:
                to_delete.add(e)

    deleted = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(str(path.relative_to(base)))
        except OSError:
            pass
    return deleted
