"""Competitor research: channels, outliers, tracking, thumbnails, titles."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Channel,
    JobRun,
    ThumbnailAnalysis,
    Video,
    VideoRevision,
    WatchlistEntry,
)
from ..services import ingest, outliers, thumbnails, titles, tracker
from ..services.ingest import estimate_channel_ingest_cost
from ..youtube.client import extract_video_id
from ..youtube.errors import NotFound, OperationTooExpensive, YouTubeError
from .deps import get_db, ledger, youtube_client

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["research"])


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


class AddChannel(BaseModel):
    #: Anything a user might paste: an id, an @handle, or any YouTube URL.
    reference: str
    #: How many recent uploads to pull. Drives the quota estimate shown first.
    video_limit: int = Field(default=50, ge=10, le=500)
    tags: list[str] = []


def _channel_dict(session: Session, channel: Channel) -> dict[str, Any]:
    video_count = session.scalar(
        select(func.count(Video.id)).where(Video.channel_id == channel.id)
    )
    latest = session.scalar(
        select(func.max(Video.published_at)).where(Video.channel_id == channel.id)
    )
    return {
        "id": channel.id,
        "title": channel.title,
        "handle": channel.handle,
        "thumbnail_url": channel.thumbnail_url,
        "subscriber_count": channel.subscriber_count,
        "subscriber_count_hidden": channel.subscriber_count_hidden,
        "video_count": channel.video_count,
        "view_count": channel.view_count,
        "is_owned": channel.is_owned,
        "is_tracked": channel.is_tracked,
        "tags": channel.tags or [],
        "stored_videos": video_count or 0,
        "latest_upload": latest.isoformat() if latest else None,
        "fetched_at": channel.fetched_at.isoformat(),
        "url": f"https://www.youtube.com/channel/{channel.id}",
    }


@router.get("/channels")
def list_channels(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = session.scalars(select(Channel).order_by(Channel.title)).all()
    return [_channel_dict(session, c) for c in rows]


@router.post("/channels/estimate")
def estimate_add(payload: AddChannel, session: Session = Depends(get_db)) -> dict[str, Any]:
    """What adding this channel would cost, before spending anything.

    Shown as a confirmation step because a 500-video pull is 20 units and a
    careless sweep of twenty channels is 400 — worth seeing first.
    """
    config = get_settings()
    cost = estimate_channel_ingest_cost(payload.video_limit) + 1  # +1 to resolve
    status = ledger(config).status(session)
    return {
        "estimated_units": cost,
        "remaining": status.remaining,
        "affordable": cost <= status.remaining,
        "cap": config.per_operation_quota_cap,
        "within_cap": cost <= config.per_operation_quota_cap,
    }


@router.post("/channels")
def add_channel(payload: AddChannel, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Resolve a channel reference, then pull and score its recent uploads."""
    config = get_settings()
    cost = estimate_channel_ingest_cost(payload.video_limit) + 1
    if cost > config.per_operation_quota_cap:
        raise OperationTooExpensive(
            f"Importing {payload.video_limit} videos would cost about {cost:,} units, "
            f"over the {config.per_operation_quota_cap:,}-unit per-operation cap.",
            "Lower the video count, or raise the cap in Settings.",
        )

    job = JobRun(job="channel.add", status="running", detail=payload.reference)
    session.add(job)
    session.flush()

    client = youtube_client(session, config)
    try:
        ledger(config).check(session, cost, what="Importing this channel")
        raw = client.resolve_channel(payload.reference)
        channel = ingest.upsert_channel(session, raw, is_tracked=True)
        if payload.tags:
            channel.tags = payload.tags

        videos, _revisions = ingest.ingest_channel_uploads(
            client, session, channel,
            limit=payload.video_limit, shorts_max_seconds=config.shorts_max_seconds,
        )
        titles.analyse_and_store(session, videos)
        outliers.score_channel(
            session, channel.id,
            window=config.baseline_window, min_age_days=config.baseline_min_age_days,
            min_videos=config.baseline_min_videos,
            outlier_threshold=config.outlier_threshold,
        )

        job.status = "ok"
        job.items_processed = len(videos)
        job.units_spent = client.units_spent
        job.detail = f"Imported {len(videos)} videos from {channel.title}."
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        return {"channel": _channel_dict(session, channel), "videos_imported": len(videos),
                "units_spent": client.units_spent, "cache_hits": client.cache_hits}
    except YouTubeError as exc:
        job.status = "error"
        job.error = exc.message
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        raise
    finally:
        client.close()


@router.post("/channels/{channel_id}/refresh")
def refresh_channel(
    channel_id: str,
    video_limit: int = Query(50, ge=10, le=500),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    config = get_settings()
    channel = session.get(Channel, channel_id)
    if channel is None:
        raise NotFound(f"No stored channel with id {channel_id}.")

    client = youtube_client(session, config)
    try:
        raw = client.get_channels([channel_id])
        if raw:
            ingest.upsert_channel(session, raw[0])
        videos, revisions = ingest.ingest_channel_uploads(
            client, session, channel,
            limit=video_limit, shorts_max_seconds=config.shorts_max_seconds,
        )
        titles.analyse_and_store(session, videos)
        outliers.score_channel(
            session, channel_id,
            window=config.baseline_window, min_age_days=config.baseline_min_age_days,
            min_videos=config.baseline_min_videos,
            outlier_threshold=config.outlier_threshold,
        )
        return {
            "channel": _channel_dict(session, channel),
            "videos_refreshed": len(videos),
            "changes_detected": len(revisions),
            "units_spent": client.units_spent,
        }
    finally:
        client.close()


@router.delete("/channels/{channel_id}")
def remove_channel(
    channel_id: str,
    purge: bool = Query(False, description="Also delete stored videos and history."),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Untrack a channel, optionally deleting everything fetched for it.

    Untracking alone keeps the data — it was paid for in quota, and re-adding
    the channel later would cost that again.
    """
    channel = session.get(Channel, channel_id)
    if channel is None:
        raise NotFound(f"No stored channel with id {channel_id}.")
    if channel.is_owned:
        raise HTTPException(
            status_code=400,
            detail="This is your own channel. Change it in Settings instead.",
        )

    channel.is_tracked = False
    removed = 0
    if purge:
        removed = session.scalar(
            select(func.count(Video.id)).where(Video.channel_id == channel_id)
        ) or 0
        session.execute(delete(Video).where(Video.channel_id == channel_id))
        session.delete(channel)

    return {"ok": True, "purged": purge, "videos_removed": removed}


# --------------------------------------------------------------------------
# Outliers
# --------------------------------------------------------------------------


@router.get("/outliers")
def list_outliers(
    min_multiplier: float = Query(1.0, ge=0),
    fmt: Literal["all", "long", "short"] = Query("all", alias="format"),
    max_age_days: int | None = Query(None, ge=1),
    min_age_days: int | None = Query(None, ge=0),
    min_views: int = Query(0, ge=0),
    channel_id: list[str] | None = Query(None),
    reliable_only: bool = Query(False),
    search: str | None = Query(None),
    sort: Literal["multiplier", "views", "recent", "percentile"] = Query("multiplier"),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Ranked outliers across every stored channel.

    Reads only from the database, so filters can be changed freely without
    spending a unit — the scores were computed at ingest time.
    """
    config = get_settings()

    query = select(Video).where(Video.outlier_multiplier.is_not(None))
    if fmt == "long":
        query = query.where(Video.is_short.is_(False))
    elif fmt == "short":
        query = query.where(Video.is_short.is_(True))
    if channel_id:
        query = query.where(Video.channel_id.in_(channel_id))
    if min_views:
        query = query.where(Video.latest_view_count >= min_views)
    if search:
        query = query.where(Video.title.ilike(f"%{search}%"))

    now = datetime.now(timezone.utc)
    if max_age_days:
        query = query.where(
            Video.published_at >= (now - timedelta(days=max_age_days)).replace(tzinfo=None)
        )
    if min_age_days:
        query = query.where(
            Video.published_at <= (now - timedelta(days=min_age_days)).replace(tzinfo=None)
        )

    videos = list(session.scalars(query).all())
    if not videos:
        return {"results": [], "total": 0, "baselines": {}}

    channel_ids = {v.channel_id for v in videos}
    channels = {
        c.id: c for c in session.scalars(select(Channel).where(Channel.id.in_(channel_ids))).all()
    }

    # Rebuild baselines from the full stored history per channel, not just the
    # filtered subset — filtering to "last 30 days" must not redefine normal.
    baselines: dict[str, outliers.Baseline] = {}
    for cid in channel_ids:
        all_videos = list(session.scalars(select(Video).where(Video.channel_id == cid)).all())
        for is_short in (False, True):
            baseline = outliers.compute_baseline(
                all_videos, channel_id=cid, is_short=is_short,
                min_age_days=config.baseline_min_age_days,
                min_videos=config.baseline_min_videos, window=config.baseline_window,
            )
            if baseline:
                baselines[f"{cid}:{'short' if is_short else 'long'}"] = baseline

    scores = outliers.score_videos(
        videos, baselines, {cid: c.title for cid, c in channels.items()},
        outlier_threshold=config.outlier_threshold,
        min_age_days=config.baseline_min_age_days,
    )
    scores = [s for s in scores if s.multiplier >= min_multiplier]
    if reliable_only:
        scores = [s for s in scores if s.reliable]

    if sort == "views":
        scores.sort(key=lambda s: s.views, reverse=True)
    elif sort == "recent":
        scores.sort(key=lambda s: s.published_at, reverse=True)
    elif sort == "percentile":
        scores.sort(key=lambda s: s.percentile, reverse=True)

    total = len(scores)
    return {
        "results": [s.to_dict() for s in scores[:limit]],
        "total": total,
        "shown": min(total, limit),
        "baselines": {k: b.to_dict() for k, b in baselines.items()},
        "threshold": config.outlier_threshold,
    }


@router.post("/outliers/rescore")
def rescore(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Recompute every score from stored data. Free — no API calls."""
    config = get_settings()
    results = outliers.score_all_tracked(
        session, window=config.baseline_window,
        min_age_days=config.baseline_min_age_days,
        min_videos=config.baseline_min_videos,
        outlier_threshold=config.outlier_threshold,
    )
    return {
        "channels_scored": len(results),
        "videos_scored": sum(len(v) for v in results.values()),
        "units_spent": 0,
    }


# --------------------------------------------------------------------------
# Videos and tracking
# --------------------------------------------------------------------------


@router.get("/videos/{video_id}")
def video_detail(video_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    video = session.get(Video, video_id)
    if video is None:
        raise NotFound(f"No stored video with id {video_id}.")
    channel = session.get(Channel, video.channel_id)
    analysis = session.scalar(
        select(ThumbnailAnalysis)
        .where(ThumbnailAnalysis.video_id == video_id)
        .order_by(ThumbnailAnalysis.analyzed_at.desc())
        .limit(1)
    )

    return {
        "video": {
            "id": video.id,
            "title": video.title,
            "description": video.description[:2000],
            "channel_id": video.channel_id,
            "channel_title": channel.title if channel else "",
            "published_at": video.published_at.isoformat(),
            "duration_seconds": video.duration_seconds,
            "is_short": video.is_short,
            "thumbnail_url": video.thumbnail_url,
            "tags": video.tags or [],
            "views": video.latest_view_count,
            "likes": video.latest_like_count,
            "comments": video.latest_comment_count,
            "outlier_multiplier": video.outlier_multiplier,
            "url": f"https://www.youtube.com/watch?v={video.id}",
        },
        "title_features": titles.extract_features(video.title).to_dict(),
        "thumbnail_analysis": _thumbnail_dict(analysis) if analysis else None,
        "history": tracker.video_history(session, video_id),
    }


class WatchVideo(BaseModel):
    reference: str
    label: str = ""


@router.post("/watchlist/videos")
def watch_video(payload: WatchVideo, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Add a single video to the tracker, fetching it if not already stored."""
    video_id = extract_video_id(payload.reference)
    if not video_id:
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like a YouTube video URL or ID.",
        )

    config = get_settings()
    client = youtube_client(session, config)
    try:
        videos, _ = ingest.ingest_videos(
            client, session, [video_id], shorts_max_seconds=config.shorts_max_seconds
        )
        if not videos:
            raise NotFound("YouTube returned nothing for that video — it may be private.")
        video = videos[0]

        raw = client.get_channels([video.channel_id])
        if raw:
            ingest.upsert_channel(session, raw[0])

        existing = session.scalar(
            select(WatchlistEntry).where(
                WatchlistEntry.kind == "video", WatchlistEntry.value == video_id
            )
        )
        if existing is None:
            session.add(
                WatchlistEntry(
                    kind="video", value=video_id,
                    label=payload.label or video.title, active=True,
                )
            )
        else:
            existing.active = True

        return {"ok": True, "video_id": video_id, "title": video.title,
                "units_spent": client.units_spent}
    finally:
        client.close()


@router.get("/watchlist")
def list_watchlist(session: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(WatchlistEntry).order_by(WatchlistEntry.added_at.desc())
    ).all()
    return [
        {
            "id": e.id, "kind": e.kind, "value": e.value, "label": e.label,
            "active": e.active, "added_at": e.added_at.isoformat(),
            "last_polled_at": e.last_polled_at.isoformat() if e.last_polled_at else None,
            "last_error": e.last_error,
        }
        for e in rows
    ]


@router.delete("/watchlist/{entry_id}")
def remove_watch(entry_id: int, session: Session = Depends(get_db)) -> dict[str, Any]:
    entry = session.get(WatchlistEntry, entry_id)
    if entry is None:
        raise NotFound("No such watchlist entry.")
    session.delete(entry)
    return {"ok": True}


@router.get("/changes")
def changes(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return tracker.recent_changes(session, days=days, limit=limit)


@router.post("/tracker/run")
def run_tracker(session: Session = Depends(get_db)) -> dict[str, Any]:
    """Poll the watchlist now, rather than waiting for the next interval."""
    config = get_settings()
    client = youtube_client(session, config)
    try:
        job = tracker.run_poll_cycle(
            client, session,
            shorts_max_seconds=config.shorts_max_seconds,
            max_video_age_days=config.tracker_max_video_age_days,
        )
        return {
            "status": job.status, "detail": job.detail, "error": job.error,
            "items_processed": job.items_processed, "units_spent": job.units_spent,
        }
    finally:
        client.close()


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------


def _thumbnail_dict(row: ThumbnailAnalysis) -> dict[str, Any]:
    return {
        "video_id": row.video_id,
        "thumbnail_url": row.thumbnail_url,
        "analyzed_at": row.analyzed_at.isoformat(),
        "width": row.width, "height": row.height,
        "dominant_colors": row.dominant_colors or [],
        "mean_brightness": row.mean_brightness,
        "mean_saturation": row.mean_saturation,
        "contrast": row.contrast,
        "edge_density": row.edge_density,
        "has_vision": bool(row.model),
        "model": row.model,
        "face_count": row.face_count,
        "dominant_emotion": row.dominant_emotion,
        "has_text": row.has_text,
        "text_content": row.text_content,
        "text_area_fraction": row.text_area_fraction,
        "has_arrow_or_circle": row.has_arrow_or_circle,
        "subject": row.subject,
        "composition": row.composition,
        "clarity_score": row.clarity_score,
        "notes": row.notes,
    }


class AnalyseThumbnails(BaseModel):
    video_ids: list[str]
    use_vision: bool = True


@router.post("/thumbnails/analyse")
def analyse_thumbnails(
    payload: AnalyseThumbnails, session: Session = Depends(get_db)
) -> dict[str, Any]:
    """Analyse a set of thumbnails, reusing anything already cached.

    Local measurements always run. Vision runs per uncached image and costs
    money, so the response reports exactly how many calls were actually made.
    """
    config = get_settings()
    if not payload.video_ids:
        return {"results": [], "analysed": 0, "vision_calls": 0}

    videos = list(
        session.scalars(select(Video).where(Video.id.in_(payload.video_ids))).all()
    )
    use_vision = payload.use_vision and config.has_vision

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    vision_calls = 0
    for video in videos:
        had_vision = session.scalar(
            select(ThumbnailAnalysis.model).where(
                ThumbnailAnalysis.video_id == video.id,
                ThumbnailAnalysis.thumbnail_url == video.thumbnail_url,
            )
        )
        try:
            row = thumbnails.analyse_video_thumbnail(
                session, video,
                api_key=config.anthropic_api_key, model=config.anthropic_model,
                effort=config.anthropic_effort, use_vision=use_vision,
            )
        except thumbnails.ThumbnailError as exc:
            errors.append({"video_id": video.id, "message": str(exc)})
            continue
        if use_vision and not had_vision and row.model:
            vision_calls += 1
        results.append(_thumbnail_dict(row))

    return {
        "results": results,
        "analysed": len(results),
        "vision_calls": vision_calls,
        "vision_available": config.has_vision,
        "errors": errors,
    }


@router.get("/thumbnails/patterns")
def thumbnail_patterns(
    min_multiplier: float = Query(3.0, ge=0),
    channel_id: list[str] | None = Query(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """What the thumbnails of high performers have in common."""
    query = (
        select(ThumbnailAnalysis)
        .join(Video, Video.id == ThumbnailAnalysis.video_id)
        .where(Video.outlier_multiplier >= min_multiplier)
    )
    if channel_id:
        query = query.where(Video.channel_id.in_(channel_id))
    high = list(session.scalars(query).all())

    rest_query = (
        select(ThumbnailAnalysis)
        .join(Video, Video.id == ThumbnailAnalysis.video_id)
        .where(Video.outlier_multiplier < min_multiplier)
    )
    if channel_id:
        rest_query = rest_query.where(Video.channel_id.in_(channel_id))
    rest = list(session.scalars(rest_query).all())

    return {
        "high_performers": thumbnails.summarise_pattern(high),
        "everything_else": thumbnails.summarise_pattern(rest),
        "min_multiplier": min_multiplier,
    }


# --------------------------------------------------------------------------
# Titles
# --------------------------------------------------------------------------


@router.get("/titles/compare")
def compare_titles(
    min_multiplier: float = Query(3.0, ge=0),
    channel_id: list[str] | None = Query(None),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Contrast the titles of outliers against your own channel's titles."""
    config = get_settings()

    outlier_query = select(Video).where(Video.outlier_multiplier >= min_multiplier)
    if channel_id:
        outlier_query = outlier_query.where(Video.channel_id.in_(channel_id))
    outlier_videos = list(session.scalars(outlier_query).all())

    mine: list[Video] = []
    if config.owned_channel_id:
        mine = list(
            session.scalars(
                select(Video).where(Video.channel_id == config.owned_channel_id)
            ).all()
        )

    outlier_titles = [v.title for v in outlier_videos if v.title]
    my_titles = [v.title for v in mine if v.title]

    return {
        "outlier_sample": len(outlier_titles),
        "own_sample": len(my_titles),
        "comparisons": [
            c.to_dict()
            for c in titles.compare_title_sets(outlier_titles, my_titles)
        ],
        "outlier_openings": titles.common_openings(outlier_titles),
        "own_openings": titles.common_openings(my_titles),
        "outlier_terms": titles.frequent_terms(outlier_titles),
        "own_terms": titles.frequent_terms(my_titles),
        "has_own_channel": bool(config.owned_channel_id),
    }


# --------------------------------------------------------------------------
# Discovery (search — the only expensive endpoint)
# --------------------------------------------------------------------------


class DiscoverQuery(BaseModel):
    query: str
    limit: int = Field(default=25, ge=5, le=50)
    published_within_days: int | None = Field(default=None, ge=1)


@router.post("/discover/estimate")
def discover_estimate(payload: DiscoverQuery, session: Session = Depends(get_db)) -> dict[str, Any]:
    config = get_settings()
    from ..youtube.quota import calls_for_items, cost_of

    search_cost = cost_of("search.list", calls_for_items(payload.limit))
    fetch_cost = cost_of("videos.list", calls_for_items(payload.limit))
    total = search_cost + fetch_cost
    status = ledger(config).status(session)
    return {
        "estimated_units": total,
        "search_units": search_cost,
        "fetch_units": fetch_cost,
        "remaining": status.remaining,
        "affordable": total <= status.remaining,
        "note": (
            "Search is the only expensive call YouTube offers — 100 units per 50 "
            "results, versus 1 unit per 50 for everything else. Results are cached "
            "for 12 hours, so repeating this exact search today is free."
        ),
    }


@router.post("/discover")
def discover(payload: DiscoverQuery, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Find videos by keyword, then price them against their channels' baselines.

    Two-step by design: ``search.list`` returns ids only (100 units), then
    ``videos.list`` fetches the actual data (1 unit per 50). Asking search for
    the full snippet would cost the same 100 units and return less.
    """
    config = get_settings()
    client = youtube_client(session, config)
    try:
        published_after = None
        if payload.published_within_days:
            published_after = datetime.now(timezone.utc) - timedelta(
                days=payload.published_within_days
            )

        video_ids = client.search(
            payload.query, limit=payload.limit, published_after=published_after,
            order="viewCount",
        )
        if not video_ids:
            return {"results": [], "units_spent": client.units_spent,
                    "message": "YouTube returned no videos for that search."}

        videos, _ = ingest.ingest_videos(
            client, session, video_ids, shorts_max_seconds=config.shorts_max_seconds
        )

        # Fetch the channels behind the results so multipliers have a denominator.
        channel_ids = list({v.channel_id for v in videos if v.channel_id})
        for raw in client.get_channels(channel_ids):
            ingest.upsert_channel(session, raw)

        titles.analyse_and_store(session, videos)

        channels = {
            c.id: c
            for c in session.scalars(select(Channel).where(Channel.id.in_(channel_ids))).all()
        }
        baselines: dict[str, outliers.Baseline] = {}
        for cid in channel_ids:
            stored = list(session.scalars(select(Video).where(Video.channel_id == cid)).all())
            for is_short in (False, True):
                baseline = outliers.compute_baseline(
                    stored, channel_id=cid, is_short=is_short,
                    min_age_days=config.baseline_min_age_days,
                    min_videos=config.baseline_min_videos, window=config.baseline_window,
                )
                if baseline:
                    baselines[f"{cid}:{'short' if is_short else 'long'}"] = baseline

        scores = outliers.score_videos(
            videos, baselines, {cid: c.title for cid, c in channels.items()},
            outlier_threshold=config.outlier_threshold,
            min_age_days=config.baseline_min_age_days,
        )

        unscored = [
            {
                "video_id": v.id, "title": v.title, "views": v.latest_view_count,
                "channel_title": channels[v.channel_id].title if v.channel_id in channels else "",
                "thumbnail_url": v.thumbnail_url,
                "published_at": v.published_at.isoformat(),
                "url": f"https://www.youtube.com/watch?v={v.id}",
            }
            for v in videos
            if not any(s.video_id == v.id for s in scores)
        ]

        return {
            "results": [s.to_dict() for s in scores],
            "unscored": unscored,
            "unscored_note": (
                "These channels don't have enough stored history to price against. "
                "Add one as a tracked channel to give it a baseline."
                if unscored else ""
            ),
            "units_spent": client.units_spent,
            "cache_hits": client.cache_hits,
        }
    finally:
        client.close()
