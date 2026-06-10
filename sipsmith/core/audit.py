"""Audit log writer and FastAPI middleware."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.models.audit import AuditLog

log = logging.getLogger(__name__)


class AuditWriter:
    """Write audit rows. Shared instance injected into PluginContext."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def write(
        self,
        actor: str,
        action: str,
        resource: str | None = None,
        old_value: Any = None,
        new_value: Any = None,
        ip_address: str | None = None,
        result: str = "ok",
    ) -> None:
        def _serialize(v: Any) -> str | None:
            if v is None:
                return None
            if isinstance(v, str):
                return v
            return json.dumps(v, default=str)

        entry = AuditLog(
            actor=actor,
            action=action,
            resource=resource,
            old_value=_serialize(old_value),
            new_value=_serialize(new_value),
            ip_address=ip_address,
            result=result,
        )
        self._db.add(entry)
        await self._db.commit()
        log.info("AUDIT actor=%s action=%s resource=%s result=%s", actor, action, resource, result)
