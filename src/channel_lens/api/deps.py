"""Shared request plumbing: sessions, clients, and error translation.

The error handling here is the reason this module exists. Every failure in the
app is either a :class:`YouTubeError` subclass or an unexpected exception, and
both need to reach the browser as something a person can act on. A bare 500
with a stack trace in the terminal is the failure mode this is built to avoid —
the app runs on the user's own machine, so *they* are the only support channel
there is.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_session_factory
from ..youtube.client import YouTubeClient
from ..youtube.errors import NotConfigured, YouTubeError
from ..youtube.quota import QuotaLedger

log = logging.getLogger(__name__)


def get_db() -> Iterator[Session]:
    """One transactional session per request."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def settings() -> Settings:
    return get_settings()


def ledger(config: Settings | None = None) -> QuotaLedger:
    config = config or get_settings()
    return QuotaLedger(config.daily_quota_budget)


def youtube_client(session: Session, config: Settings | None = None) -> YouTubeClient:
    """Build a Data API client bound to this request's session.

    Raises :class:`NotConfigured` rather than returning ``None``, so a missing
    key surfaces as a 409 with setup instructions instead of an AttributeError
    three frames deeper.
    """
    config = config or get_settings()
    if not config.youtube_api_key:
        raise NotConfigured()
    return YouTubeClient(config.youtube_api_key, ledger(config), session)


def error_payload(exc: YouTubeError) -> dict[str, Any]:
    return exc.to_dict()


async def youtube_error_handler(_request: Request, exc: YouTubeError) -> JSONResponse:
    """Turn a typed YouTube failure into a response the UI can render directly."""
    return JSONResponse(status_code=exc.status_code, content=error_payload(exc))


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Says something true and useful rather than nothing.

    The full traceback goes to the log; the browser gets the exception's own
    message, because on a local single-user app the person reading the error is
    also the person who can fix it.
    """
    log.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "error": type(exc).__name__,
            "message": f"Something went wrong: {exc}",
            "hint": (
                "This is a bug. The terminal running Channel Lens has the full "
                "traceback if you want to report it."
            ),
        },
    )
