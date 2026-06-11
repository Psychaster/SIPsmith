"""
Call journey correlation engine.
Stitches call legs from different sources into unified CallJourney rows
using globalCallID, SIP Call-ID, and time-window matching.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith_records.models import CallJourney, CallRecord

log = logging.getLogger("sipsmith.records.correlator")

_TIME_WINDOW = datetime.timedelta(seconds=30)


async def correlate_batch(
    record_ids: list[int],
    db: AsyncSession,
) -> int:
    """
    Attempt to correlate a batch of newly ingested CallRecord IDs into journeys.
    Returns the number of journeys created or updated.
    """
    if not record_ids:
        return 0

    result = await db.execute(
        select(CallRecord).where(
            CallRecord.id.in_(record_ids),
            CallRecord.journey_id.is_(None),
        )
    )
    records = list(result.scalars().all())
    updated = 0

    for rec in records:
        journey = await _find_matching_journey(rec, db)
        if journey is None:
            journey = CallJourney(
                anchor_global_call_id=rec.global_call_id,
                start_time=rec.start_time,
                end_time=rec.end_time,
                leg_count=1,
                cause_code=rec.cause_code,
            )
            db.add(journey)
            await db.flush()
            updated += 1
        else:
            # Extend journey time bounds
            if rec.start_time and (
                journey.start_time is None or rec.start_time < journey.start_time
            ):
                journey.start_time = rec.start_time
            if rec.end_time and (journey.end_time is None or rec.end_time > journey.end_time):
                journey.end_time = rec.end_time
            journey.leg_count = (journey.leg_count or 0) + 1
            if journey.cause_code is None and rec.cause_code is not None:
                journey.cause_code = rec.cause_code

        rec.journey_id = journey.id

    await db.flush()
    return updated


async def _find_matching_journey(rec: CallRecord, db: AsyncSession) -> CallJourney | None:
    """
    Find an existing journey that this record belongs to.
    Priority: globalCallID match > SIP Call-ID match > CMS call-ID > time window.
    """
    # 1. Global call ID (CUCM-specific, most reliable)
    if rec.global_call_id:
        result = await db.execute(
            select(CallJourney).where(CallJourney.anchor_global_call_id == rec.global_call_id)
        )
        journey = result.scalar_one_or_none()
        if journey:
            return journey

    # 2. SIP Call-ID match: find a leg with same call-id and link to its journey
    if rec.sip_call_id:
        result = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.sip_call_id == rec.sip_call_id,
                CallRecord.journey_id.isnot(None),
                CallRecord.id != rec.id,
            )
            .limit(1)
        )
        sibling = result.scalar_one_or_none()
        if sibling and sibling.journey_id:
            return await db.get(CallJourney, sibling.journey_id)

    # 3. CMS call-ID
    if rec.cms_call_id:
        result = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.cms_call_id == rec.cms_call_id,
                CallRecord.journey_id.isnot(None),
                CallRecord.id != rec.id,
            )
            .limit(1)
        )
        sibling = result.scalar_one_or_none()
        if sibling and sibling.journey_id:
            return await db.get(CallJourney, sibling.journey_id)

    # 4. Time-window: same parties within ±30 s
    if rec.start_time and rec.calling_dn and rec.called_dn:
        lo = rec.start_time - _TIME_WINDOW
        hi = rec.start_time + _TIME_WINDOW
        result = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.calling_dn == rec.calling_dn,
                CallRecord.called_dn == rec.called_dn,
                CallRecord.start_time >= lo,
                CallRecord.start_time <= hi,
                CallRecord.journey_id.isnot(None),
                CallRecord.id != rec.id,
            )
            .limit(1)
        )
        sibling = result.scalar_one_or_none()
        if sibling and sibling.journey_id:
            return await db.get(CallJourney, sibling.journey_id)

    return None
