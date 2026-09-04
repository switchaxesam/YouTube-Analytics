"""FastAPI application: API routes plus the static frontend, on one port.

Serving the UI from the same origin as the API is what keeps this a
double-click desktop app — no CORS, no second process, no build step, no
``node_modules``. The frontend is plain ES modules and CSS, so what ships is
what was written.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .api import owned, research, system
from .api.deps import unhandled_error_handler, youtube_error_handler
from .config import get_settings
from .db import init_db
from .youtube.errors import YouTubeError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    config = get_settings()
    if config.tracker_enabled:
        scheduler.start()
    yield
    scheduler.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Channel Lens",
        description="Local YouTube strategy workbench.",
        version="0.1.0",
        lifespan=lifespan,
        # The docs are genuinely useful here — this is a local app whose owner
        # may well want to script against it.
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_exception_handler(YouTubeError, youtube_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(system.router)
    app.include_router(research.router)
    app.include_router(owned.router)

    app.mount(
        "/static", StaticFiles(directory=STATIC_DIR), name="static"
    )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app


app = create_app()
