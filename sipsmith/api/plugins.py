"""Plugin management API — status, enable, disable."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user, require_role
from sipsmith.database import get_db
from sipsmith.models.plugin import PluginRecord
from sipsmith.models.user import Role, User
from sipsmith.sdk.plugin import PluginStatus, ServiceStatus

router = APIRouter(prefix="/plugins", tags=["plugins"])


class PluginOut(BaseModel):
    plugin_id: str
    version: str
    enabled: bool

    model_config = {"from_attributes": True}


@router.get("/status")
async def all_plugin_statuses(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return HTML fragment with status cards for each loaded plugin."""
    from sipsmith.registry import get_loader

    loader = get_loader()
    cards: list[str] = []
    for plugin_id, plugin in loader.all().items():
        try:
            from pathlib import Path

            from sipsmith.agent.client import AgentClient
            from sipsmith.core.audit import AuditWriter
            from sipsmith.sdk.context import PluginContext

            ctx = PluginContext(
                plugin_id=plugin_id,
                db=db,
                agent=AgentClient(),
                audit=AuditWriter(db),
                registry=loader,
                template_dirs=[],
                data_dir=Path("/var/lib/sipsmith"),
            )
            status = await plugin.status(ctx)
        except Exception:
            status = PluginStatus(
                plugin_id=plugin_id,
                status=ServiceStatus.unknown,
                enabled=False,
                detail="Status check failed",
            )

        badge_class = {
            ServiceStatus.ok: "status-ok",
            ServiceStatus.degraded: "status-warn",
            ServiceStatus.error: "status-error",
            ServiceStatus.disabled: "",
            ServiceStatus.unknown: "",
        }.get(status.status, "")

        cards.append(
            f'<div class="status-card">'
            f'<div class="card-header">'
            f'<span class="card-title">{plugin.meta.name}</span>'
            f'<span class="status-badge {badge_class}">{status.status.value.upper()}</span>'
            f"</div>"
            f'<div class="card-body">{status.detail or "&nbsp;"}</div>'
            f"</div>"
        )

    return "\n".join(cards) if cards else '<p style="color:var(--text-muted)">No plugins loaded</p>'


@router.get("", response_model=list[PluginOut])
async def list_plugins(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(PluginRecord).order_by(PluginRecord.plugin_id))
    return result.scalars().all()


@router.post("/{plugin_id}/enable", status_code=202)
async def enable_plugin(
    plugin_id: str,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from sipsmith.registry import get_loader

    loader = get_loader()
    plugin = loader.get(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not loaded")

    from pathlib import Path

    from sipsmith.agent.client import AgentClient
    from sipsmith.core.audit import AuditWriter
    from sipsmith.sdk.context import PluginContext

    ctx = PluginContext(
        plugin_id=plugin_id,
        db=db,
        agent=AgentClient(),
        audit=AuditWriter(db),
        registry=loader,
        template_dirs=[],
        data_dir=Path("/var/lib/sipsmith"),
    )
    await plugin.enable(ctx)

    result = await db.execute(select(PluginRecord).where(PluginRecord.plugin_id == plugin_id))
    rec = result.scalar_one_or_none()
    if rec:
        rec.enabled = True
        await db.commit()

    return {"detail": f"Plugin {plugin_id} enabled"}


@router.post("/{plugin_id}/disable", status_code=202)
async def disable_plugin(
    plugin_id: str,
    user: Annotated[User, Depends(require_role(Role.admin))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from sipsmith.registry import get_loader

    loader = get_loader()
    plugin = loader.get(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_id}' not loaded")

    from pathlib import Path

    from sipsmith.agent.client import AgentClient
    from sipsmith.core.audit import AuditWriter
    from sipsmith.sdk.context import PluginContext

    ctx = PluginContext(
        plugin_id=plugin_id,
        db=db,
        agent=AgentClient(),
        audit=AuditWriter(db),
        registry=loader,
        template_dirs=[],
        data_dir=Path("/var/lib/sipsmith"),
    )
    await plugin.disable(ctx)

    result = await db.execute(select(PluginRecord).where(PluginRecord.plugin_id == plugin_id))
    rec = result.scalar_one_or_none()
    if rec:
        rec.enabled = False
        await db.commit()

    return {"detail": f"Plugin {plugin_id} disabled"}
