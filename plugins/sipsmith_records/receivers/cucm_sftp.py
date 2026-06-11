"""
CUCM CDR/DRF SFTP watcher.

Scans the chrooted SFTP directory for a configured RecordSource and ingests
any new CDR flat files. Called on demand from the API (trigger-ingest) or
by a periodic background task.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith_records.models import CallRecord, RecordSource

log = logging.getLogger("sipsmith.records.receiver.cucm_sftp")

_SFTP_ROOT = Path("/var/lib/sipsmith/sftp")


async def ingest_source(source: RecordSource, db: AsyncSession) -> int:
    """
    Scan the SFTP directory for a CUCM CDR source and ingest new files.
    Returns the number of records inserted.
    """
    from sipsmith_records.correlator import correlate_batch
    from sipsmith_records.parsers.cucm_cdr import parse_cdr_content

    if not source.sftp_account or not source.sftp_path:
        log.warning("Source %s has no SFTP path configured", source.name)
        return 0

    base = _SFTP_ROOT / source.sftp_account / source.sftp_path.lstrip("/")
    if not base.exists():
        log.warning("SFTP path does not exist: %s", base)
        return 0

    # Find unprocessed CDR files (extension .csv or no extension, non-empty)
    candidates = sorted(base.rglob("*.csv")) + sorted(
        p for p in base.rglob("*") if p.is_file() and p.suffix == "" and p.stat().st_size > 0
    )

    total = 0
    inserted_ids: list[int] = []

    for fpath in candidates:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Cannot read %s: %s", fpath, exc)
            continue

        parsed = parse_cdr_content(content, source.id)
        for rec_dict in parsed:
            rec = CallRecord(**rec_dict)
            db.add(rec)
            await db.flush()
            inserted_ids.append(rec.id)
            total += 1

    if inserted_ids:
        import datetime

        source.last_ingested_at = datetime.datetime.now(datetime.UTC)
        await db.commit()
        try:
            await correlate_batch(inserted_ids, db)
        except Exception:  # noqa: BLE001
            log.exception("Correlation error for CUCM SFTP batch")

    return total
