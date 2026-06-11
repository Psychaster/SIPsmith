"""FastAPI application factory."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sipsmith.config import get_settings


def _configure_logging() -> None:
    settings = get_settings()
    log_dir = Path(settings.logging.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / "sipsmith.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    handlers.append(file_handler)

    logging.basicConfig(
        level=settings.logging.level,
        format="%(asctime)s %(name)-24s %(levelname)-8s %(message)s",
        handlers=handlers,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    _configure_logging()
    log = logging.getLogger("sipsmith.app")
    log.info("SIPsmith starting up")

    # Run Alembic migrations on startup

    from alembic.config import Config as AlembicConfig

    from alembic import command as alembic_cmd

    alembic_ini = Path(__file__).parent.parent / "alembic.ini"
    if alembic_ini.exists():
        try:
            cfg = AlembicConfig(str(alembic_ini))
            alembic_cmd.upgrade(cfg, "head")
            log.info("Database migrations applied")
        except Exception as exc:
            log.warning("Alembic migration error (non-fatal in dev): %s", exc)

    yield

    log.info("SIPsmith shutting down")


def create_app() -> FastAPI:

    app = FastAPI(
        title="SIPsmith",
        description="Lab Services Appliance",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # ── Static files ──────────────────────────────────────────────────────
    static_dir = Path(__file__).parent / "ui" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── API routers ───────────────────────────────────────────────────────
    from sipsmith.api.plugins import router as plugins_router
    from sipsmith.api.system import router as system_router
    from sipsmith.auth.router import router as auth_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(plugins_router, prefix="/api/v1")

    # ── GUI routes ────────────────────────────────────────────────────────
    from sipsmith.ui.router import router as ui_router

    app.include_router(ui_router)

    # ── Plugin UI routes (loaded from discovered plugins) ─────────────────
    _register_plugin_ui(app)

    return app


def _register_plugin_ui(app: FastAPI) -> None:
    """Mount UI routers from discovered plugins, best-effort."""
    import importlib
    import logging
    from pathlib import Path as P

    log = logging.getLogger("sipsmith.app")
    plugins_dir = P(__file__).parent.parent / "plugins"
    if not plugins_dir.exists():
        return

    import sys

    plugins_parent = str(plugins_dir)
    if plugins_parent not in sys.path:
        sys.path.insert(0, plugins_parent)

    for plugin_dir in sorted(plugins_dir.iterdir()):
        ui_router_mod = plugin_dir / "ui_router.py"
        if not ui_router_mod.exists():
            continue
        pkg_name = plugin_dir.name  # e.g. "sipsmith_sftp"
        mod_name = f"{pkg_name}.ui_router"
        try:
            mod = importlib.import_module(mod_name)
            app.include_router(mod.router)
            log.info("Registered UI router for plugin: %s", pkg_name)
        except Exception as exc:
            log.warning("Could not load UI router for plugin %s: %s", pkg_name, exc)
