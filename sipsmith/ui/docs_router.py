"""Documentation pages served at /docs — Markdown rendered to HTML."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import markdown
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from sipsmith.auth.deps import get_current_user
from sipsmith.models.user import User

log = logging.getLogger("sipsmith.docs")

router = APIRouter(prefix="/docs", tags=["docs"])

_here = Path(__file__).parent
_docs_dir = Path(__file__).parent.parent.parent / "docs"
_templates = Jinja2Templates(directory=str(_here / "templates"))

_MD_EXTENSIONS = ["tables", "fenced_code", "toc", "attr_list", "def_list", "abbr"]

_PAGE_TITLES = {
    "index": "SIPsmith Documentation",
    "install": "Installation Guide",
    "admin": "Administrator Guide",
    "user": "User Guide",
    "architecture": "Architecture",
}


def _render(slug: str) -> str:
    """Load docs/<slug>.md and convert to HTML."""
    md_file = _docs_dir / f"{slug}.md"
    if not md_file.exists():
        return f"<p><em>Documentation page <code>{slug}.md</code> not found.</em></p>"
    text = md_file.read_text(encoding="utf-8")
    return markdown.markdown(text, extensions=_MD_EXTENSIONS)


def _response(request: Request, user: User, slug: str) -> HTMLResponse:
    html = _render(slug)
    return _templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "user": user,
            "content": html,
            "page_title": _PAGE_TITLES.get(slug, slug.title()),
            "current_page": slug,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def docs_index(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    return _response(request, user, "index")


@router.get("/{slug}", response_class=HTMLResponse)
async def docs_page(
    slug: str,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
) -> HTMLResponse:
    if slug not in _PAGE_TITLES:
        slug = "index"
    return _response(request, user, slug)
