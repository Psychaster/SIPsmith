"""
CMS HTTP CDR receiver.

CMS POSTs XML CDR records to a per-cluster URL:
  POST /api/v1/plugins/records/receive/cms/<token>
Content-Type: text/xml  (or application/xml)

The token authenticates the request against the configured RecordSource.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.database import get_db
from sipsmith_records.correlator import correlate_batch
from sipsmith_records.models import CallRecord, RecordSource, SourceType
from sipsmith_records.parsers.cms_cdr import parse_cdr_content

log = logging.getLogger("sipsmith.records.receiver.cms")

router = APIRouter(prefix="/plugins/records", tags=["records-receiver"])


@router.post("/receive/cms/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def receive_cms_cdr(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Accept an XML CDR POST from CMS. Token authenticates the source."""
    result = await db.execute(
        select(RecordSource).where(
            RecordSource.receiver_token == token,
            RecordSource.source_type == SourceType.cms_cdr,
        )
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")

    body = await request.body()
    if not body:
        return

    try:
        xml_text = body.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("CMS CDR decode error: %s", exc)
        return

    parsed = parse_cdr_content(xml_text, source.id)
    if not parsed:
        return

    inserted_ids: list[int] = []
    for rec_dict in parsed:
        rec = CallRecord(**rec_dict)
        db.add(rec)
        await db.flush()
        inserted_ids.append(rec.id)

    import datetime

    source.last_ingested_at = datetime.datetime.now(datetime.UTC)
    await db.commit()

    try:
        async with db.begin():
            await correlate_batch(inserted_ids, db)
    except Exception:  # noqa: BLE001
        log.exception("Correlation error for CMS CDR batch")
