"""Owner-only analytics for the user's own channel.

This is the part no competitor-analysis tool can do for you, at any price:
impressions, click-through rate, and retention are visible only to the channel
owner, through an authenticated request. Everything else in this app is
inference from public numbers. This module is measurement.

**It takes two different Google APIs, and this is the surprising part.**

* The **YouTube Analytics API v2** (``youtubeanalytics.googleapis.com``) answers
  queries on demand — views, watch time, average view duration and percentage,
  subscribers, and the traffic-source split. It does *not* expose impressions
  or CTR, and never has.
* The **YouTube Reporting API** (``youtubereporting.googleapis.com``) is the
  only source of thumbnail impressions and CTR, added on 15 January 2026 as the
  ``channel_reach_basic_a1`` report. It is a bulk system, not a query system:
  you register a job, Google generates a daily CSV, and you download it. The
  first report lands **up to 48 hours** after the job is created, with 30 days
  of backfill.

That 48-hour delay is a property of Google's system, not a bug here, so the UI
states it plainly rather than looking broken while a user waits.

Neither API spends YouTube Data API quota — they have their own, much larger
allowances — so nothing here counts against the 10,000 units that constrain the
rest of the app.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from ..config import app_home, oauth_token_path
from .errors import YouTubeError

log = logging.getLogger(__name__)

ANALYTICS_ROOT = "https://youtubeanalytics.googleapis.com/v2"
REPORTING_ROOT = "https://youtubereporting.googleapis.com/v1"

#: The single scope this app needs.
#:
#: It covers both hosts we authenticate against — the Analytics API v2 and the
#: Reporting API — because every OAuth call in this module targets one of those
#: two. Nothing here touches the Data API over OAuth; that runs on the API key.
#:
#: Deliberately excludes two scopes it would be easy to add by reflex.
#: ``yt-analytics-monetary.readonly`` would hand over revenue data this app has
#: no use for. ``youtube.readonly`` was requested in an earlier version and
#: never actually used — an unused permission is pure downside, and dropping it
#: also removes a second "sensitive" scope from the consent screen.
SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]

#: The Reporting API report type carrying thumbnail impressions and CTR.
REACH_REPORT_TYPE = "channel_reach_basic_a1"

#: Metrics the Analytics API v2 will return for a channel report. Verified
#: against the published metric list — an unsupported name fails the whole
#: request, so this list is conservative rather than aspirational.
VIDEO_METRICS = [
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "subscribersLost",
    "likes",
    "comments",
    "shares",
]

TRAFFIC_METRICS = ["views", "estimatedMinutesWatched"]


class AnalyticsError(YouTubeError):
    """An owner-analytics request failed."""


class NotAuthorised(AnalyticsError):
    """No stored OAuth credentials, or they can no longer be refreshed."""

    status_code = 401

    def __init__(self, message: str = "Not connected to your YouTube account.") -> None:
        super().__init__(
            message,
            "Open Settings and press Connect. You'll be sent to Google to grant "
            "read-only access to your own channel's analytics.",
        )


class ReportNotReady(AnalyticsError):
    """The reporting job exists but Google hasn't produced a report yet."""

    status_code = 202


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------


@dataclass
class OAuthState:
    """What the app knows about its connection to the user's Google account."""

    connected: bool
    channel_id: str | None = None
    channel_title: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": self.scopes or [],
            "error": self.error,
        }


def _client_config(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


#: How long a half-finished authorisation stays valid. Google's own codes expire
#: well inside this; the window only needs to cover a human reading a consent
#: screen.
_PENDING_TTL_SECONDS = 900


def _pending_path():
    return app_home() / "oauth_pending.json"


def _store_verifier(state: str, verifier: str | None) -> None:
    """Remember the PKCE verifier for an in-flight authorisation.

    The library generates a ``code_verifier`` when the authorisation URL is
    built, sends only its hash to Google, and then requires the original at the
    token exchange. Those are two separate HTTP requests here, handled by two
    separate ``Flow`` objects, so the verifier has to outlive the first one or
    the exchange fails with "Missing code verifier".

    Persisted to disk rather than kept in memory so the handshake survives the
    app being restarted while the consent screen is open.
    """
    if not verifier:
        return
    payload = {"state": state, "verifier": verifier, "created": time.time()}
    _pending_path().write_text(json.dumps(payload), encoding="utf-8")


def _take_verifier(state: str | None) -> str | None:
    """Retrieve and consume the stored verifier. Single use, like the code."""
    path = _pending_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        path.unlink(missing_ok=True)

    if time.time() - payload.get("created", 0) > _PENDING_TTL_SECONDS:
        return None
    # Google echoes `state` back; a mismatch means this callback belongs to a
    # different authorisation attempt and its verifier would not fit anyway.
    if state and payload.get("state") and payload["state"] != state:
        return None
    return payload.get("verifier")


def build_auth_url(
    client_id: str, client_secret: str, redirect_uri: str
) -> tuple[str, str]:
    """Start the OAuth flow. Returns ``(authorization_url, state)``.

    ``access_type=offline`` plus ``prompt=consent`` is what makes Google issue a
    refresh token. Without both, re-authorising an account that has already
    granted access returns an access token only, and the connection silently
    dies an hour later.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    # Deliberately NOT passing include_granted_scopes. That turns on incremental
    # authorisation, which makes Google return every scope the account has ever
    # granted this client — not just the one asked for here. oauthlib then sees
    # the returned scope set differ from the requested one and refuses the
    # exchange with "Scope has changed". This app wants exactly one scope, so
    # incremental auth buys nothing and costs a confusing failure.
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    # Generated by authorization_url() above; needed again at the exchange.
    _store_verifier(state, getattr(flow, "code_verifier", None))
    return url, state


def _explain_exchange_failure(exc: Exception) -> AnalyticsError:
    """Turn an oauthlib/Google token-exchange failure into a specific diagnosis.

    Each of these needs a different fix and they are indistinguishable from the
    single generic message this used to report. The raw text is always appended,
    because a confident wrong guess that hides the real error is worse than no
    guess at all.
    """
    text = str(exc)
    lowered = text.lower()

    if "scope has changed" in lowered:
        return AnalyticsError(
            "Google granted a different set of permissions than were requested.",
            f"Usually harmless and now tolerated automatically — try connecting again. "
            f"If it repeats, revoke access at myaccount.google.com/permissions "
            f"and retry. ({text})",
        )
    if "redirect_uri_mismatch" in lowered:
        return AnalyticsError(
            "The redirect address didn't match what Google expected.",
            f"Confirm the OAuth client's application type is 'Desktop app'. A "
            f"'Web application' client requires the exact redirect URI to be "
            f"registered up front. ({text})",
        )
    if "code verifier" in lowered or "code_verifier" in lowered:
        return AnalyticsError(
            "The security check for this authorisation could not be completed.",
            f"The app was restarted, or the consent screen sat open for more than "
            f"15 minutes, between starting the connection and finishing it. Press "
            f"Connect again and complete it in one go. ({text})",
        )
    if "invalid_grant" in lowered:
        return AnalyticsError(
            "Google rejected the authorisation code as already used or expired.",
            f"Codes are single-use and short-lived. Press Connect again and complete "
            f"the consent screen without reloading it. ({text})",
        )
    if "invalid_client" in lowered or "unauthorized_client" in lowered:
        return AnalyticsError(
            "Google rejected the client ID or secret.",
            f"Re-copy both from the Cloud console into Settings. The secret is shown "
            f"only once at creation, so a rotated client needs its new secret. ({text})",
        )
    if "access_denied" in lowered:
        return AnalyticsError(
            "Access was declined on the Google consent screen.",
            "Press Connect again and choose Allow. If an 'unverified app' warning "
            "appears, use Advanced → Go to Channel Lens first.",
        )
    return AnalyticsError(
        f"The token exchange with Google failed: {text}",
        "If that isn't obviously a credential problem, the full traceback is in the "
        "terminal running Channel Lens.",
    )


def exchange_code(
    client_id: str, client_secret: str, redirect_uri: str, code: str,
    state: str | None = None,
) -> dict:
    """Trade an authorisation code for tokens and persist them."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(client_id, client_secret, redirect_uri),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    # This is a different Flow object from the one that built the auth URL, so
    # it has no PKCE verifier of its own. Restore the stored one, or Google
    # rejects the exchange with "Missing code verifier".
    verifier = _take_verifier(state)
    if verifier:
        flow.code_verifier = verifier
    # Google routinely returns the granted scopes in a different order from the
    # request, and may fold in `openid`. oauthlib treats any difference as
    # tampering and aborts. Relaxing this is safe here because authorisation is
    # carried by the token itself, not by our expectations about it — and the
    # scopes actually granted are recorded below.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 - oauthlib raises a wide variety
        log.exception("OAuth token exchange failed")
        raise _explain_exchange_failure(exc) from exc

    credentials = flow.credentials
    payload = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or SCOPES),
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
    }
    oauth_token_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_credentials():
    """Return refreshed credentials, or raise :class:`NotAuthorised`.

    Refreshes in place and rewrites the token file, so a long-lived install
    never needs the user to reconnect unless Google actually revokes access.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = oauth_token_path()
    if not path.exists():
        raise NotAuthorised()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NotAuthorised("Stored credentials are unreadable.") from exc

    credentials = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes") or SCOPES,
    )

    if not credentials.valid:
        if not credentials.refresh_token:
            raise NotAuthorised(
                "The stored connection has no refresh token, so it can't be renewed."
            )
        try:
            credentials.refresh(Request())
        except Exception as exc:  # noqa: BLE001
            raise NotAuthorised(
                "Google refused to renew the connection — access may have been revoked."
            ) from exc
        data["token"] = credentials.token
        data["expiry"] = credentials.expiry.isoformat() if credentials.expiry else None
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return credentials


def disconnect() -> None:
    """Forget the stored credentials."""
    path = oauth_token_path()
    if path.exists():
        path.unlink()


def oauth_state() -> OAuthState:
    """Describe the current connection without raising."""
    try:
        credentials = load_credentials()
    except NotAuthorised as exc:
        return OAuthState(connected=False, error=exc.message)
    return OAuthState(
        connected=True,
        expires_at=credentials.expiry.replace(tzinfo=timezone.utc)
        if credentials.expiry
        else None,
        scopes=list(credentials.scopes or []),
    )


def _authorised_headers() -> dict[str, str]:
    credentials = load_credentials()
    return {"Authorization": f"Bearer {credentials.token}"}


def _raise_for_google_error(response: httpx.Response, what: str) -> None:
    if response.status_code == 200:
        return
    try:
        body = response.json().get("error", {})
        message = body.get("message", response.text[:300])
    except (ValueError, AttributeError):
        message = response.text[:300]

    if response.status_code in (401, 403):
        raise NotAuthorised(f"{what} was refused: {message}")
    raise AnalyticsError(
        f"{what} failed ({response.status_code}): {message}",
        "If this persists, disconnect and reconnect in Settings.",
    )


# --------------------------------------------------------------------------
# Analytics API v2 — on-demand queries
# --------------------------------------------------------------------------


def _query(params: dict[str, Any], *, http: httpx.Client | None = None) -> dict:
    client = http or httpx.Client(timeout=30.0)
    try:
        response = client.get(
            f"{ANALYTICS_ROOT}/reports", params=params, headers=_authorised_headers()
        )
    except httpx.HTTPError as exc:
        raise AnalyticsError(
            "Could not reach the YouTube Analytics API.", "Check your connection."
        ) from exc
    finally:
        if http is None:
            client.close()
    _raise_for_google_error(response, "The analytics query")
    return response.json()


def _rows_to_dicts(payload: dict) -> list[dict]:
    """Convert the API's column-header + row-array shape into dicts.

    >>> payload = {
    ...     "columnHeaders": [{"name": "day"}, {"name": "views"}],
    ...     "rows": [["2026-03-01", 120], ["2026-03-02", 340]],
    ... }
    >>> _rows_to_dicts(payload)
    [{'day': '2026-03-01', 'views': 120}, {'day': '2026-03-02', 'views': 340}]
    >>> _rows_to_dicts({"columnHeaders": [{"name": "day"}]})
    []
    """
    headers = [h["name"] for h in payload.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in payload.get("rows", []) or []]


def channel_daily(
    start: date, end: date, *, http: httpx.Client | None = None
) -> list[dict]:
    """Channel-wide metrics per day."""
    payload = _query(
        {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(VIDEO_METRICS),
            "dimensions": "day",
            "sort": "day",
        },
        http=http,
    )
    return _rows_to_dicts(payload)


def video_totals(
    start: date, end: date, *, limit: int = 200, http: httpx.Client | None = None
) -> list[dict]:
    """Per-video totals across the range, most-viewed first.

    One request for the whole channel, which is what makes the dashboard cheap
    enough to refresh freely.
    """
    payload = _query(
        {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(VIDEO_METRICS),
            "dimensions": "video",
            "sort": "-views",
            "maxResults": min(limit, 200),
        },
        http=http,
    )
    return _rows_to_dicts(payload)


def video_daily(
    video_id: str, start: date, end: date, *, http: httpx.Client | None = None
) -> list[dict]:
    """One video's metrics per day."""
    payload = _query(
        {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(VIDEO_METRICS),
            "dimensions": "day",
            "filters": f"video=={video_id}",
            "sort": "day",
        },
        http=http,
    )
    return _rows_to_dicts(payload)


def traffic_sources(
    start: date,
    end: date,
    *,
    video_id: str | None = None,
    http: httpx.Client | None = None,
) -> list[dict]:
    """Views split by where they came from.

    This split is what separates a packaging problem from a topic problem.
    ``SUGGESTED`` and ``BROWSE`` traffic is won or lost on the thumbnail and
    title against neighbouring videos; ``YT_SEARCH`` traffic is won on the topic
    matching what someone typed.
    """
    params: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "metrics": ",".join(TRAFFIC_METRICS),
        "dimensions": "insightTrafficSourceType",
        "sort": "-views",
    }
    if video_id:
        params["filters"] = f"video=={video_id}"
    return _rows_to_dicts(_query(params, http=http))


# --------------------------------------------------------------------------
# Reporting API — the only source of impressions and CTR
# --------------------------------------------------------------------------


def list_reporting_jobs(*, http: httpx.Client | None = None) -> list[dict]:
    client = http or httpx.Client(timeout=30.0)
    try:
        response = client.get(f"{REPORTING_ROOT}/jobs", headers=_authorised_headers())
    finally:
        if http is None:
            client.close()
    _raise_for_google_error(response, "Listing reporting jobs")
    return response.json().get("jobs", []) or []


def ensure_reach_job(*, http: httpx.Client | None = None) -> dict:
    """Find or create the job that produces thumbnail impressions and CTR.

    Idempotent: an existing job for this report type is reused. Creating a
    second job for the same report type would duplicate the data and the wait
    for nothing.
    """
    for job in list_reporting_jobs(http=http):
        if job.get("reportTypeId") == REACH_REPORT_TYPE:
            return job

    client = http or httpx.Client(timeout=30.0)
    try:
        response = client.post(
            f"{REPORTING_ROOT}/jobs",
            headers={**_authorised_headers(), "Content-Type": "application/json"},
            json={"reportTypeId": REACH_REPORT_TYPE, "name": "Channel Lens reach"},
        )
    finally:
        if http is None:
            client.close()
    _raise_for_google_error(response, "Creating the CTR reporting job")
    return response.json()


def list_reports(
    job_id: str, *, created_after: datetime | None = None,
    http: httpx.Client | None = None,
) -> list[dict]:
    params: dict[str, Any] = {"pageSize": 100}
    if created_after:
        params["createdAfter"] = created_after.strftime("%Y-%m-%dT%H:%M:%SZ")

    client = http or httpx.Client(timeout=30.0)
    try:
        response = client.get(
            f"{REPORTING_ROOT}/jobs/{job_id}/reports",
            params=params,
            headers=_authorised_headers(),
        )
    finally:
        if http is None:
            client.close()
    _raise_for_google_error(response, "Listing generated reports")
    return response.json().get("reports", []) or []


def download_report(download_url: str, *, http: httpx.Client | None = None) -> list[dict]:
    """Fetch and parse one generated CSV report."""
    client = http or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        response = client.get(download_url, headers=_authorised_headers())
    finally:
        if http is None:
            client.close()
    _raise_for_google_error(response, "Downloading a report")
    return list(csv.DictReader(io.StringIO(response.text)))


def parse_reach_rows(rows: Iterable[dict]) -> list[dict]:
    """Normalise ``channel_reach_basic_a1`` rows into the app's own shape.

    The Reporting API uses snake_case column names and returns everything as
    strings, unlike the Analytics API's camelCase and native types — so this is
    where the two are reconciled into one vocabulary.

    >>> rows = [{"date": "20260301", "video_id": "abc",
    ...          "video_thumbnail_impressions": "1500",
    ...          "video_thumbnail_impressions_ctr": "4.2"}]
    >>> parse_reach_rows(rows)
    [{'day': datetime.date(2026, 3, 1), 'video_id': 'abc', 'impressions': 1500, 'impressions_ctr': 4.2}]

    Malformed rows are dropped rather than crashing the import:

    >>> parse_reach_rows([{"date": "bad", "video_id": "x"}])
    []
    """
    parsed: list[dict] = []
    for row in rows:
        raw_date = (row.get("date") or "").strip()
        video_id = (row.get("video_id") or "").strip()
        if not raw_date or not video_id:
            continue
        try:
            day = datetime.strptime(raw_date, "%Y%m%d").date()
        except ValueError:
            continue
        try:
            impressions = int(float(row.get("video_thumbnail_impressions") or 0))
            ctr = float(row.get("video_thumbnail_impressions_ctr") or 0.0)
        except (TypeError, ValueError):
            continue
        parsed.append(
            {
                "day": day,
                "video_id": video_id,
                "impressions": impressions,
                "impressions_ctr": ctr,
            }
        )
    return parsed


def reach_job_status(*, http: httpx.Client | None = None) -> dict:
    """Whether CTR data is available yet, and when to expect it if not.

    The 48-hour wait after job creation is Google's, and a UI that doesn't say
    so reads as broken for two days.
    """
    jobs = [j for j in list_reporting_jobs(http=http) if j.get("reportTypeId") == REACH_REPORT_TYPE]
    if not jobs:
        return {
            "job_exists": False,
            "reports_available": 0,
            "message": (
                "CTR and impressions aren't set up yet. Google delivers these as a daily "
                "bulk report, which has to be requested before it starts generating."
            ),
            "action": "Enable CTR reporting",
        }

    job = jobs[0]
    reports = list_reports(job["id"], http=http)
    if not reports:
        created = job.get("createTime", "")
        return {
            "job_exists": True,
            "job_id": job["id"],
            "reports_available": 0,
            "created_at": created,
            "message": (
                "CTR reporting is set up and Google is generating the first report. "
                "This takes up to 48 hours from when it was requested, then arrives "
                "daily with 30 days of history backfilled."
            ),
            "action": None,
        }

    return {
        "job_exists": True,
        "job_id": job["id"],
        "reports_available": len(reports),
        "message": f"{len(reports)} daily report{'s' if len(reports) != 1 else ''} available.",
        "action": None,
    }
