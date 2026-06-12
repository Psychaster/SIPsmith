"""GUI page routes — server-rendered Jinja2."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.deps import get_current_user
from sipsmith.database import get_db
from sipsmith.models.user import User

router = APIRouter(tags=["ui"])


def _templates() -> Jinja2Templates:
    from pathlib import Path

    # Resolve template dir relative to this file
    here = Path(__file__).parent
    return Jinja2Templates(directory=str(here / "templates"))


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Detect first-run state: zero users in the DB means the appliance has just
    # been installed and the login screen needs to render the "Create initial admin"
    # card instead of the sign-in form.
    try:
        user_count = await db.scalar(select(func.count(User.id))) or 0
    except Exception:  # noqa: BLE001
        user_count = 0  # table not yet migrated — treat as first run

    first_run = user_count == 0

    # If a bootstrap token file exists, surface a hint so the operator knows where
    # to find the value to paste into the setup card.
    from pathlib import Path as _P

    bootstrap_present = _P("/etc/sipsmith/.bootstrap_token").exists()

    # Suggested defaults — operator can change before submitting.
    default_username = "admin"
    default_email = "admin@localhost"

    templates = _templates()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "error": None,
            "first_run": first_run,
            "bootstrap_present": bootstrap_present,
            "default_username": default_username,
            "default_email": default_email,
        },
    )


# /account/change-password — landing page for the forced-reset flow. Reached on
# first login (must_change_password=True on the User row) or whenever an admin
# resets a user's password.
@router.get("/account/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
        request,
        "change_password.html",
        {"request": request, "user": user},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "user": user},
    )


@router.get("/system/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"request": request, "user": user},
    )


# §13 — base.html links to /system/plugins and /system/users; the routes existed
# only on the appliance hand-patch, not in the source tree. Without them, clicking
# either nav item returned JSON 404. The backing APIs already exist; these pages
# just provide an HTMX-driven shell over them.
@router.get("/system/plugins", response_class=HTMLResponse)
async def plugins_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
        request,
        "plugins.html",
        {"request": request, "user": user},
    )


@router.get("/system/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
        request,
        "users.html",
        {"request": request, "user": user},
    )
