"""Plugin management API — status, enable, disable."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
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


@router.get("/status", response_class=HTMLResponse)
async def all_plugin_statuses(
    _user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return HTML fragment with one status card per loaded plugin.

    Each card is a link: enabled plugins jump to /plugins/<id> (their config page);
    disabled plugins jump to /system/plugins (where the operator enables them). This
    is the dashboard-side answer to "click an activated module → configuration page"."""
    from html import escape

    from sipsmith.registry import get_loader

    loader = get_loader()

    # One query to find which plugins are enabled (so cards can route accordingly).
    enabled_ids: set[str] = set()
    try:
        rows = (await db.execute(select(PluginRecord))).scalars().all()
        enabled_ids = {r.plugin_id for r in rows if r.enabled}
    except Exception:  # noqa: BLE001
        pass

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
        except Exception:  # noqa: BLE001
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

        is_enabled = plugin_id in enabled_ids
        href = f"/plugins/{plugin_id}" if is_enabled else "/system/plugins"
        cta = "Configure →" if is_enabled else "Enable →"

        cards.append(
            f'<a class="status-card status-card-link" href="{escape(href, quote=True)}">'
            f'<div class="card-header">'
            f'<span class="card-title">{escape(plugin.meta.name)}</span>'
            f'<span class="status-badge {badge_class}">'
            f"{escape(status.status.value.upper())}</span>"
            f"</div>"
            f'<div class="card-body">{escape(status.detail) if status.detail else "&nbsp;"}</div>'
            f'<div class="card-footer"><span class="card-cta">{cta}</span></div>'
            f"</a>"
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

    # §14 — upsert so the toggle persists even if install_all() hasn't seeded the row.
    result = await db.execute(select(PluginRecord).where(PluginRecord.plugin_id == plugin_id))
    rec = result.scalar_one_or_none()
    if rec is None:
        rec = PluginRecord(plugin_id=plugin_id, version=plugin.meta.version, enabled=True)
        db.add(rec)
    else:
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

    # §14 — upsert: ensure a row exists before flipping so the state is recorded.
    result = await db.execute(select(PluginRecord).where(PluginRecord.plugin_id == plugin_id))
    rec = result.scalar_one_or_none()
    if rec is None:
        rec = PluginRecord(plugin_id=plugin_id, version=plugin.meta.version, enabled=False)
        db.add(rec)
    else:
        rec.enabled = False
    await db.commit()

    return {"detail": f"Plugin {plugin_id} disabled"}
