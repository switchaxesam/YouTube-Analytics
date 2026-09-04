"""Your own channel: owner analytics, CTR setup, and the packaging diagnosis."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..services import ingest, owned, titles
from ..services.owned import QUADRANT_LABELS
from ..youtube import analytics
from ..youtube.errors import NotFound
from .deps import get_db, youtube_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/owned", tags=["owned"])


@router.get("/status")
def status(session: Session = Depends(get_db)) -> dict[str, Any]:
    """What's connected, what's syncing, and what's still waiting on Google."""
    config = get_settings()
    channel = owned.owned_channel(session)

    state = analytics.oauth_state()
    reach: dict[str, Any]
    if state.connected:
        try:
            reach = analytics.reach_job_status()
        except analytics.AnalyticsError as exc:
            reach = {"job_exists": False, "message": exc.message, "action": None}
    else:
        reach = {
            "job_exists": False,
            "message": "Connect your Google account to enable CTR reporting.",
            "action": None,
        }

    return {
        "channel_id": config.owned_channel_id,
        "channel": (
            {
                "id": channel.id, "title": channel.title,
                "thumbnail_url": channel.thumbnail_url,
                "subscriber_count": channel.subscriber_count,
                "video_count": channel.video_count,
            }
            if channel else None
        ),
        "oauth": state.to_dict(),
        "ctr_reporting": reach,
    }


@router.post("/link")
def link_channel(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch and store the channel named in Settings, marking it as owned."""
    config = get_settings()
    if not config.owned_channel_id:
        raise NotFound(
            "No channel is set as yours.",
        )

    client = youtube_client(session, config)
    try:
        raw = client.resolve_channel(config.owned_channel_id)
        channel = ingest.upsert_channel(session, raw, is_owned=True, is_tracked=True)
        # Store the canonical id, so a handle typed in Settings resolves once.
        if config.owned_channel_id != channel.id:
            config.owned_channel_id = channel.id
            config.save()
            get_settings(refresh=True)

        videos, _ = ingest.ingest_channel_uploads(
            client, session, channel, limit=200,
            shorts_max_seconds=config.shorts_max_seconds,
        )
        titles.analyse_and_store(session, videos)
        return {
            "ok": True, "channel_id": channel.id, "title": channel.title,
            "videos_imported": len(videos), "units_spent": client.units_spent,
        }
    finally:
        client.close()


@router.post("/sync")
def sync(
    days: int = Query(90, ge=7, le=365), session: Session = Depends(get_db)
) -> dict[str, Any]:
    """Pull owner analytics. Costs no Data API quota."""
    result = owned.sync_analytics(session, days=days)
    ctr = owned.sync_ctr(session)
    return {
        "analytics": result,
        "ctr": ctr,
        "note": "Owner analytics use a separate Google quota — this spends none of your 10,000 Data API units.",
    }


@router.post("/ctr/enable")
def enable_ctr() -> dict[str, Any]:
    """Register the Reporting API job that produces impressions and CTR.

    Google takes up to 48 hours to generate the first report, then backfills 30
    days. Saying so up front is the difference between a considered wait and an
    app that looks broken.
    """
    job = analytics.ensure_reach_job()
    return {
        "ok": True,
        "job_id": job.get("id"),
        "created_at": job.get("createTime"),
        "message": (
            "CTR reporting requested. Google generates the first daily report within "
            "48 hours and backfills the previous 30 days. Nothing more to do — "
            "Channel Lens will pick it up on the next sync."
        ),
    }


@router.get("/diagnosis")
def diagnosis(
    days: int = Query(90, ge=7, le=365),
    min_impressions: int = Query(500, ge=0),
    quadrant: str | None = Query(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """CTR × retention, read against your own channel's medians."""
    results, meta = owned.diagnose(session, days=days, min_impressions=min_impressions)
    if quadrant:
        results = [d for d in results if d.quadrant == quadrant]

    counts: dict[str, int] = {}
    for item in results:
        counts[item.quadrant] = counts.get(item.quadrant, 0) + 1

    return {
        "results": [d.to_dict() for d in results],
        "meta": meta,
        "counts": counts,
        "labels": QUADRANT_LABELS,
    }


@router.get("/traffic")
def traffic(days: int = Query(90, ge=7, le=365)) -> dict[str, Any]:
    """Where views come from, and what that implies about packaging."""
    end = date.today()
    start = end - timedelta(days=days)
    rows = analytics.traffic_sources(start, end)

    total = sum(int(r.get("views", 0) or 0) for r in rows) or 1
    enriched = []
    for row in rows:
        source = row.get("insightTrafficSourceType", "")
        views = int(row.get("views", 0) or 0)
        enriched.append(
            {
                "source": source,
                "views": views,
                "share": round(views / total * 100, 1),
                "minutes_watched": row.get("estimatedMinutesWatched"),
                "packaging_sensitive": source in owned.PACKAGING_SOURCES,
            }
        )

    packaging_share = sum(r["share"] for r in enriched if r["packaging_sensitive"])
    return {
        "sources": enriched,
        "total_views": total,
        "packaging_share": round(packaging_share, 1),
        "reading": (
            f"{packaging_share:.0f}% of your views come from surfaces where your "
            f"thumbnail competes directly against other videos (browse, suggested, "
            f"related). That is the share of your traffic packaging can actually move."
        ),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
    }
