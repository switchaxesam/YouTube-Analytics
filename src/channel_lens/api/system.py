"""Settings, quota, jobs, and the Google OAuth handshake."""

from __future__ import annotations

import logging
from html import escape
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import SECRET_KEYS, Settings, get_settings
from ..models import JobRun
from ..youtube import analytics
from ..youtube.client import YouTubeClient
from ..youtube.errors import YouTubeError
from .deps import get_db, ledger

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])


class SettingsUpdate(BaseModel):
    """Partial settings update.

    Every field is optional, and a secret sent as an empty string is treated as
    "leave unchanged" rather than "clear" — otherwise the UI, which never
    receives the real values, would blank them on every save. Clearing is done
    through the explicit ``clear_secrets`` list.
    """

    youtube_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    owned_channel_id: str | None = None
    daily_quota_budget: int | None = None
    per_operation_quota_cap: int | None = None
    baseline_window: int | None = None
    baseline_min_age_days: int | None = None
    baseline_min_videos: int | None = None
    outlier_threshold: float | None = None
    tracker_interval_hours: int | None = None
    tracker_enabled: bool | None = None
    tracker_max_video_age_days: int | None = None
    anthropic_model: str | None = None
    anthropic_effort: str | None = None
    shorts_max_seconds: int | None = None
    theme: str | None = None
    clear_secrets: list[str] = []


@router.get("/status")
def status(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Everything the shell needs on load: config state, quota, and gaps."""
    config = get_settings()
    quota = ledger(config).status(session)
    try:
        oauth = analytics.oauth_state().to_dict()
    except Exception as exc:  # noqa: BLE001 - never let this break the shell
        oauth = {"connected": False, "error": str(exc)}

    return {
        "settings": config.redacted(),
        "quota": quota.to_dict(),
        "missing": config.missing_requirements(),
        # The sidebar badge counts only genuine blockers. Counting optional
        # extras made the app look like it needed a paid API key it does not.
        "required_missing": len(config.required_gaps()),
        "oauth": oauth,
        "ready": config.has_data_api,
    }


@router.get("/settings")
def read_settings() -> dict[str, Any]:
    return get_settings().redacted()


@router.put("/settings")
def write_settings(update: SettingsUpdate) -> dict[str, Any]:
    config = get_settings()
    payload = update.model_dump(exclude_unset=True)
    clear = set(payload.pop("clear_secrets", []) or [])

    for key, value in payload.items():
        if value is None:
            continue
        # An empty secret means "unchanged"; use clear_secrets to actually wipe.
        if key in SECRET_KEYS and value == "":
            continue
        if isinstance(value, str) and key in SECRET_KEYS:
            value = value.strip()
        setattr(config, key, value)

    for key in clear & SECRET_KEYS:
        setattr(config, key, "")

    config.save()
    refreshed = get_settings(refresh=True)
    return {"settings": refreshed.redacted(), "missing": refreshed.missing_requirements()}


@router.post("/settings/test-youtube-key")
def test_youtube_key(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Spend one quota unit to prove the key works, and say what it proved."""
    config = get_settings()
    if not config.youtube_api_key:
        return {"ok": False, "message": "No API key is set."}

    client = YouTubeClient(config.youtube_api_key, ledger(config), session)
    try:
        # A known-permanent channel (YouTube's own) — 1 unit, always resolvable.
        items = client.get_channels(["UCBR8-60-B28hp2BmDPdntcQ"])
    except YouTubeError as exc:
        return {"ok": False, "message": exc.message, "hint": exc.hint}
    finally:
        client.close()

    return {
        "ok": bool(items),
        "message": "Key works. The YouTube Data API accepted it and returned data.",
        "units_spent": client.units_spent,
    }


@router.post("/settings/test-anthropic-key")
def test_anthropic_key() -> dict[str, Any]:
    """Confirm the Anthropic key is accepted, with the smallest possible call."""
    config = get_settings()
    if not config.anthropic_api_key:
        return {"ok": False, "message": "No Anthropic API key is set."}

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        client.messages.create(
            model=config.anthropic_model,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with OK."}],
        )
    except Exception as exc:  # noqa: BLE001 - report whatever the SDK said
        return {"ok": False, "message": f"The key was not accepted: {exc}"}

    return {"ok": True, "message": f"Key works with {config.anthropic_model}."}


@router.get("/quota")
def quota(session: Session = Depends(get_db)) -> dict[str, Any]:
    config = get_settings()
    return ledger(config).status(session).to_dict()


@router.get("/jobs")
def jobs(
    limit: int = Query(25, ge=1, le=200), session: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": j.id,
            "job": j.job,
            "status": j.status,
            "started_at": j.started_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            "detail": j.detail,
            "error": j.error,
            "units_spent": j.units_spent,
            "items_processed": j.items_processed,
        }
        for j in rows
    ]


# --------------------------------------------------------------------------
# OAuth for owner analytics
# --------------------------------------------------------------------------


def _redirect_uri() -> str:
    """Loopback redirect for the OAuth handshake.

    ``127.0.0.1`` rather than ``localhost`` on Google's own recommendation —
    ``localhost`` resolution can be intercepted by client firewalls, and it may
    resolve to IPv6 ``::1`` while the server is listening on IPv4. The literal
    address has neither failure mode.

    Desktop-app OAuth clients accept any loopback port without registering it,
    which is what lets the app fall back to a different port when 8730 is taken
    and still authenticate.
    """
    return f"http://127.0.0.1:{get_settings().port}/api/auth/callback"


@router.get("/auth/url")
def auth_url() -> dict[str, Any]:
    config = get_settings()
    if not config.has_oauth_client:
        return {
            "ok": False,
            "message": "No OAuth client is configured.",
            "hint": (
                "In Google Cloud, create an OAuth 2.0 Client ID of type 'Desktop app', "
                "then paste its client ID and secret into Settings."
            ),
        }
    try:
        url, state = analytics.build_auth_url(
            config.google_client_id, config.google_client_secret, _redirect_uri()
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Could not start the OAuth flow: {exc}"}
    return {"ok": True, "url": url, "state": state, "redirect_uri": _redirect_uri()}


@router.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    """Google redirects here after consent.

    Returns a small self-closing page rather than JSON, because this URL is
    opened in a real browser tab that the user should not be left staring at.
    """
    if error:
        body = f"<h1>Authorisation cancelled</h1><p>Google said: {escape(error)}</p>"
        return HTMLResponse(_callback_page(body, ok=False), status_code=400)
    if not code:
        return HTMLResponse(
            _callback_page("<h1>Missing authorisation code</h1>", ok=False),
            status_code=400,
        )

    config = get_settings()
    try:
        # `state` is echoed back by Google and is what pairs this callback with
        # the PKCE verifier stored when the authorisation URL was built.
        analytics.exchange_code(
            config.google_client_id, config.google_client_secret, _redirect_uri(),
            code, state=state,
        )
    except YouTubeError as exc:
        # The hint carries the actual diagnosis, so it has to reach the page.
        # Both halves are escaped: they can embed raw text from Google.
        body = (
            f"<h1>Connection failed</h1><p>{escape(exc.message)}</p>"
            f"<p class='hint'>{escape(exc.hint)}</p>"
        )
        return HTMLResponse(_callback_page(body, ok=False), status_code=400)

    return HTMLResponse(
        _callback_page(
            "<h1>Connected</h1><p>Your channel's analytics are now available in "
            "Channel Lens. You can close this tab.</p>",
            ok=True,
        )
    )


def _callback_page(body: str, *, ok: bool) -> str:
    accent = "#16a34a" if ok else "#dc2626"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Channel Lens</title>
<style>
  body {{ font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: #0b0d10; color: #e6e8eb; }}
  .card {{ max-width: 30rem; padding: 2.5rem; text-align: center;
           border: 1px solid #232830; border-radius: 14px; background: #12151a; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .5rem; color: {accent}; }}
  p {{ margin: 0; color: #9aa3ad; }}
  p.hint {{ margin-top: .75rem; font-size: .875rem; color: #6b7480; line-height: 1.5; }}
</style></head>
<body><div class="card">{body}</div>
<script>setTimeout(() => window.close(), 2500);</script>
</body></html>"""


@router.post("/auth/disconnect")
def auth_disconnect() -> dict[str, Any]:
    analytics.disconnect()
    return {"ok": True, "message": "Disconnected. Owner analytics are no longer available."}
