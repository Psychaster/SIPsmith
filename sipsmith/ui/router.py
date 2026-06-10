"""GUI page routes — server-rendered Jinja2."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from sipsmith.auth.deps import get_current_user
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
async def login_page(request: Request):
    templates = _templates()
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)] = None,
):
    if user is None:
        return RedirectResponse(url="/login")
    templates = _templates()
    return templates.TemplateResponse(
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
        "audit.html",
        {"request": request, "user": user},
    )
