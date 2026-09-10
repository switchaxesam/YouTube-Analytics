"""Competitor research: channels, outliers, tracking, thumbnails, titles."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import numpy as np

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
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
from ..services import (
    exclusions,
    ingest,
    jobs,
    outliers,
    thumbnails,
    titles,
    tracker,
    trailing,
)
from ..services.ingest import estimate_channel_ingest_cost
from ..youtube.client import (
    chunked,
    extract_video_id,
    normalise_channel_input,
    parse_channel_references,
)
from ..youtube.errors import (
    NotConfigured,
    NotFound,
    OperationTooExpensive,
    QuotaExceeded,
    UpstreamQuotaExceeded,
    YouTubeError,
)
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


class BulkAddChannels(BaseModel):
    """A pasted list of channel references."""

    text: str
    video_limit: int = Field(default=50, ge=10, le=500)
    #: Skip references resolving to a channel already tracked.
    skip_existing: bool = True
    tags: list[str] = []


#: Upper bound on one paste. Not a quota limit — that is enforced separately —
#: but a guard against pasting a whole spreadsheet and waiting minutes for a
#: request that was never going to be affordable.
MAX_BULK_REFERENCES = 100


def _short_error(exc: Exception) -> str:
    """One readable line from an arbitrary exception.

    SQLAlchemy in particular embeds the whole failing statement and its bound
    parameters in ``str(exc)``, which is invaluable in a log and useless in a
    table cell. The full traceback still reaches the terminal via
    ``log.exception``.

    >>> _short_error(ValueError("something broke"))
    'something broke'
    >>> _short_error(RuntimeError("(sqlite3.IntegrityError) FOREIGN KEY constraint failed\\n[SQL: INSERT INTO ...]"))
    '(sqlite3.IntegrityError) FOREIGN KEY constraint failed'
    >>> _short_error(ValueError("x" * 300)).endswith("…")
    True

    An exception carrying no message still names itself, rather than rendering
    as an empty cell:

    >>> _short_error(ValueError(""))
    'ValueError'
    """
    text = str(exc).split("\n", 1)[0].strip()
    if not text:
        return type(exc).__name__
    return text if len(text) <= 200 else text[:199] + "…"


def _existing_channel_hint(session: Session, reference: str) -> Channel | None:
    """Best-effort match of a reference to an already-stored channel.

    Cheap and deliberately approximate: it exists so the preview can say "you
    already track this one" *before* spending a unit to resolve it. Resolution
    during the import is what actually decides, so a miss here costs one unit,
    and a false positive is impossible since id and handle matches are exact.
    """
    try:
        kind, value = normalise_channel_input(reference)
    except ValueError:
        return None
    if kind == "id":
        return session.get(Channel, value)
    handle = value.lstrip("@").lower()
    if not handle:
        return None
    return session.scalar(
        select(Channel).where(
            func.lower(func.replace(func.coalesce(Channel.handle, ""), "@", "")) == handle
        )
    )


@router.post("/channels/bulk/preview")
def bulk_preview(
    payload: BulkAddChannels, session: Session = Depends(get_db)
) -> dict[str, Any]:
    """Parse the pasted text and price the import, spending nothing.

    Always shown before the import runs. A bulk action that starts spending
    quota the moment you paste is precisely the behaviour this app exists to
    avoid.
    """
    config = get_settings()
    references = parse_channel_references(payload.text)
    truncated = len(references) > MAX_BULK_REFERENCES
    references = references[:MAX_BULK_REFERENCES]

    items: list[dict[str, Any]] = []
    new_count = 0
    for reference in references:
        existing = _existing_channel_hint(session, reference)
        if existing is not None:
            items.append({
                "reference": reference, "status": "existing",
                "channel_title": existing.title, "tracked": existing.is_tracked,
            })
        else:
            new_count += 1
            items.append({"reference": reference, "status": "new"})

    to_import = new_count if payload.skip_existing else len(references)
    per_channel = estimate_channel_ingest_cost(payload.video_limit) + 1
    estimated = to_import * per_channel
    status = ledger(config).status(session)

    return {
        "items": items,
        "parsed": len(references),
        "new": new_count,
        "existing": len(references) - new_count,
        "to_import": to_import,
        "per_channel_units": per_channel,
        "estimated_units": estimated,
        "remaining": status.remaining,
        "affordable": estimated <= status.remaining,
        "cap": config.per_operation_quota_cap,
        "within_cap": estimated <= config.per_operation_quota_cap,
        "truncated": truncated,
        "max_references": MAX_BULK_REFERENCES,
    }


@router.post("/channels/bulk", status_code=202)
def bulk_add_channels(
    payload: BulkAddChannels, session: Session = Depends(get_db)
) -> dict[str, Any]:
    """Start a bulk import in the background and return a job to poll.

    Returns immediately rather than holding the request open for the minutes a
    large import takes. A browser given nothing to render for two minutes is
    indistinguishable from a hung one, and the user has no way to tell whether
    their click registered.

    Validation and the quota cap are checked here, synchronously, so an import
    that could never succeed is refused with a real error instead of failing
    silently inside a background thread.
    """
    config = get_settings()
    references = parse_channel_references(payload.text)[:MAX_BULK_REFERENCES]
    if not references:
        raise HTTPException(
            status_code=400, detail="No channel references found in that text."
        )
    if not config.has_data_api:
        raise NotConfigured()

    per_channel = estimate_channel_ingest_cost(payload.video_limit) + 1
    total_estimate = len(references) * per_channel
    if total_estimate > config.per_operation_quota_cap:
        raise OperationTooExpensive(
            f"Importing {len(references)} channels at {payload.video_limit} uploads "
            f"each would cost about {total_estimate:,} units, over the "
            f"{config.per_operation_quota_cap:,}-unit per-operation cap.",
            "Import fewer at a time, lower the uploads per channel, or raise the cap "
            "in Settings.",
        )

    running = jobs.active("channel.bulk_add")
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail="An import is already running. Wait for it to finish, or cancel it.",
        )

    job = jobs.start(
        "channel.bulk_add", len(references),
        lambda job, job_session: _run_bulk_import(
            job, job_session, references, payload, per_channel,
        ),
    )
    return {
        "job_id": job.id, "total": len(references),
        "estimated_units": total_estimate,
    }


def _run_bulk_import(
    job: jobs.Job, session: Session, references: list[str],
    payload: BulkAddChannels, per_channel: int,
) -> dict[str, Any]:
    """Import each channel, reporting outcomes individually.

    Three rules make this survivable.

    **One bad reference never stops the run.** A typo, a deleted channel, or a
    vanity URL the API refuses to resolve is recorded against that row and the
    loop continues. A batch that dies on item three, having already spent quota
    on one and two, is the worst available outcome.

    **Quota is re-checked before every channel, not once up front.** The
    estimate is a forecast; real spend varies with how many uploads a channel
    actually has and how much was already cached. When the budget runs out the
    run stops cleanly and names the channels never attempted, rather than
    failing them one by one against an exhausted API.

    **Cancellation is honoured between channels.** A long import that has begun
    spending quota on the wrong list has to be stoppable, and stopping at a
    channel boundary leaves no half-imported channel behind.
    """
    config = get_settings()
    db_job = JobRun(job="channel.bulk_add", status="running",
                    detail=f"{len(references)} references")
    session.add(db_job)
    session.flush()

    client = youtube_client(session, config)
    results: list[dict[str, Any]] = []
    imported = failed = skipped = 0
    quota_ran_out = False

    cancelled = False
    try:
        for reference in references:
            job.current = reference

            if cancelled or job.cancelled:
                cancelled = True
                results.append({
                    "reference": reference, "status": "not_attempted",
                    "message": "Cancelled before this one was reached.",
                })
                skipped += 1
                job.advance()
                continue

            if quota_ran_out or not ledger(config).can_afford(session, per_channel):
                quota_ran_out = True
                results.append({
                    "reference": reference, "status": "not_attempted",
                    "message": "Stopped before this one — the daily quota ran out.",
                })
                skipped += 1
                job.advance()
                continue

            try:
                raw = client.resolve_channel(reference)
            except (QuotaExceeded, UpstreamQuotaExceeded) as exc:
                # Not this row's fault; end the run rather than burning the
                # rest of the list against an exhausted API.
                quota_ran_out = True
                results.append({"reference": reference, "status": "not_attempted",
                                "message": exc.message})
                skipped += 1
                job.advance()
                continue
            except YouTubeError as exc:
                results.append({"reference": reference, "status": "failed",
                                "message": exc.message, "hint": exc.hint})
                failed += 1
                job.advance()
                continue

            existing = session.get(Channel, raw["id"])
            if existing is not None and existing.is_tracked and payload.skip_existing:
                results.append({
                    "reference": reference, "status": "skipped",
                    "channel_id": existing.id, "channel_title": existing.title,
                    "message": "Already tracked.",
                })
                skipped += 1
                job.advance()
                continue

            # Each channel writes inside its own SAVEPOINT. Catching the
            # exception is not enough on its own: any error during a flush
            # leaves the session in a rolled-back state where every later
            # statement raises PendingRollbackError, so a single bad channel
            # would silently fail the whole rest of the batch. The savepoint is
            # what actually delivers the per-row isolation this loop promises.
            units_before = client.units_spent
            savepoint = session.begin_nested()
            try:
                channel = ingest.upsert_channel(session, raw, is_tracked=True)
                if payload.tags:
                    channel.tags = payload.tags
                videos, _revisions = ingest.ingest_channel_uploads(
                    client, session, channel, limit=payload.video_limit,
                    shorts_max_seconds=config.shorts_max_seconds,
                )
                titles.analyse_and_store(session, videos)
                outliers.score_channel(
                    session, channel.id, window=config.baseline_window,
                    min_age_days=config.baseline_min_age_days,
                    min_videos=config.baseline_min_videos,
                    outlier_threshold=config.outlier_threshold,
                )
                savepoint.commit()
                results.append({
                    "reference": reference, "status": "imported",
                    "channel_id": channel.id, "channel_title": channel.title,
                    "videos": len(videos),
                })
                imported += 1
            except BaseException as exc:  # noqa: BLE001 - re-raised below if unknown
                savepoint.rollback()

                # Rolling back also discards this channel's quota bookings, but
                # those units were genuinely spent — the requests went out. Re-book
                # them, because the ledger is allowed to over-count and must never
                # under-count.
                wasted = client.units_spent - units_before
                if wasted:
                    ledger(config).record_units(session, "failed_import", wasted)

                if isinstance(exc, (QuotaExceeded, UpstreamQuotaExceeded)):
                    quota_ran_out = True
                    results.append({"reference": reference, "status": "failed",
                                    "message": exc.message, "hint": exc.hint})
                elif isinstance(exc, YouTubeError):
                    results.append({"reference": reference, "status": "failed",
                                    "message": exc.message, "hint": exc.hint})
                elif isinstance(exc, Exception):
                    log.exception("Bulk import failed for %s", reference)
                    results.append({"reference": reference, "status": "failed",
                                    "message": _short_error(exc),
                                    "hint": "The full traceback is in the terminal "
                                            "running Channel Lens."})
                else:
                    # KeyboardInterrupt / SystemExit: the savepoint is unwound,
                    # now let it out rather than swallowing a shutdown.
                    raise
                failed += 1
            job.advance()

            # Commit after each channel so progress is durable and a later
            # failure can't roll back work already reported as done.
            session.commit()

        db_job.status = "error" if (failed and not imported) else "ok"
        db_job.items_processed = imported
        db_job.units_spent = client.units_spent
        db_job.detail = (
            f"Imported {imported}, skipped {skipped}, failed {failed} "
            f"of {len(references)}."
        )
        if failed:
            db_job.error = "; ".join(
                f"{r['reference']}: {r.get('message', '')}"
                for r in results if r["status"] == "failed"
            )[:2000]
        db_job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()

        return {
            "results": results, "imported": imported, "skipped": skipped,
            "failed": failed, "total": len(references),
            "units_spent": client.units_spent, "cache_hits": client.cache_hits,
            "quota_ran_out": quota_ran_out, "cancelled": cancelled,
        }
    finally:
        client.close()


@router.post("/channels/{channel_id}/full-history", status_code=202)
def pull_full_history(channel_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    """Fetch a channel's entire upload history, oldest to newest.

    Trailing baselines and breakout detection both walk a channel's timeline,
    and neither can do that from a truncated slice of recent uploads. This is
    the mode that gets the whole thing.

    Cheap: the uploads playlist is 1 unit per 50 ids and ``videos.list`` is
    another 1 per 50, so a thousand-video channel costs about 40 units out of
    the daily 10,000.
    """
    config = get_settings()
    channel = session.get(Channel, channel_id)
    if channel is None:
        raise NotFound(f"No stored channel with id {channel_id}.")
    if not config.has_data_api:
        raise NotConfigured()

    running = jobs.active("channel.full_history")
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail="A full-history pull is already running. Wait for it to finish.",
        )

    # The channel's own video count is YouTube's estimate, but it is the only
    # forecast available and it is close enough to warn on.
    expected = channel.video_count or 0
    estimated_units = estimate_channel_ingest_cost(expected) if expected else 20

    job = jobs.start(
        "channel.full_history", max(1, expected),
        lambda job, job_session: _run_full_history(job, job_session, channel_id),
    )
    return {
        "job_id": job.id,
        "channel_title": channel.title,
        "expected_videos": expected,
        "estimated_units": estimated_units,
    }


def _run_full_history(job: jobs.Job, session: Session, channel_id: str) -> dict[str, Any]:
    """Page the whole uploads playlist, then fetch every video 50 at a time."""
    config = get_settings()
    channel = session.get(Channel, channel_id)
    client = youtube_client(session, config)

    db_job = JobRun(job="channel.full_history", status="running", detail=channel.title)
    session.add(db_job)
    session.flush()

    try:
        if not channel.uploads_playlist_id:
            raw = client.get_channels([channel_id])
            if raw:
                ingest.upsert_channel(session, raw[0])
        if not channel.uploads_playlist_id:
            raise NotFound("This channel has no uploads playlist.")

        job.current = "Listing uploads…"
        video_ids = client.list_upload_video_ids(channel.uploads_playlist_id, limit=None)
        job.total = max(1, len(video_ids))

        stored = 0
        # 50 at a time, committing as we go, so a long pull shows progress and
        # a failure late on doesn't discard everything before it.
        for batch in chunked(video_ids, 50):
            if job.cancelled:
                break
            videos, _ = ingest.ingest_videos(
                client, session, batch, shorts_max_seconds=config.shorts_max_seconds
            )
            stored += len(videos)
            job.done = min(job.total, job.done + len(batch))
            job.current = f"{stored} of {len(video_ids)} stored"
            session.commit()

        titles.analyse_and_store(
            session,
            list(session.scalars(select(Video).where(Video.channel_id == channel_id)).all()),
        )
        outliers.score_channel(
            session, channel_id, window=config.baseline_window,
            min_age_days=config.baseline_min_age_days,
            min_videos=config.baseline_min_videos,
            outlier_threshold=config.outlier_threshold,
        )

        db_job.status = "ok"
        db_job.items_processed = stored
        db_job.units_spent = client.units_spent
        db_job.detail = f"Pulled {stored} of {len(video_ids)} uploads for {channel.title}."
        db_job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()

        return {
            "channel_id": channel_id, "channel_title": channel.title,
            "listed": len(video_ids), "stored": stored,
            "units_spent": client.units_spent, "cache_hits": client.cache_hits,
            "cancelled": job.cancelled,
        }
    finally:
        client.close()


@router.get("/jobs/live/{job_id}")
def job_progress(job_id: str) -> dict[str, Any]:
    """Poll a background job. Cheap enough to call once a second."""
    job = jobs.get(job_id)
    if job is None:
        raise NotFound(
            "That job is no longer available.",
            "Finished jobs are kept for half an hour, and none survive a restart "
            "of Channel Lens.",
        )
    return job.to_dict()


@router.get("/jobs/live")
def active_job(name: str | None = Query(None)) -> dict[str, Any]:
    """The job currently running, if any.

    Lets a page reopened mid-import re-attach to work already in flight rather
    than showing no sign of it.
    """
    job = jobs.active(name)
    return job.to_dict() if job else {"running": False}


@router.post("/jobs/live/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Ask a job to stop at its next clean boundary.

    Cooperative rather than forced: the worker finishes the channel it is on,
    so cancelling never leaves a half-imported channel behind.
    """
    job = jobs.get(job_id)
    if job is None:
        raise NotFound("That job is no longer available.")
    job.cancel()
    return {"ok": True, "message": "Stopping after the current channel."}


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
    #: Bypass the standing exclusions, so the user can always see what was hidden.
    include_excluded: bool = Query(False),
    search: str | None = Query(None),
    sort: Literal[
        "multiplier", "views", "recent", "percentile", "baseline",
        "title", "channel", "duration",
    ] = Query("multiplier"),
    direction: Literal["asc", "desc"] = Query("desc"),
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
        return {"results": [], "total": 0, "baselines": {}, "excluded": 0, "exclusions": []}

    channel_ids = {v.channel_id for v in videos}
    channels = {
        c.id: c for c in session.scalars(select(Channel).where(Channel.id.in_(channel_ids))).all()
    }

    # Exclusions are applied to the *scored* set, never to the baselines below:
    # a channel's normal is what it normally does, including the videos you
    # personally don't care about. Filtering those out of the denominator would
    # silently inflate every multiplier that survived.
    rules = exclusions.from_settings(config)
    dropped: list[dict[str, Any]] = []
    if rules.active and not include_excluded:
        videos, dropped = exclusions.partition(videos, rules, channels)
        if not videos:
            return {
                "results": [], "total": 0, "baselines": {},
                "excluded": len(dropped), "exclusions": dropped[:50],
                "threshold": config.outlier_threshold,
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

    # Sorting happens over the whole scored set before the limit is applied, so
    # "sort by views ascending" means the lowest of everything that matched —
    # not the lowest of whichever hundred happened to rank highest first.
    sort_keys = {
        "multiplier": lambda s: s.multiplier,
        "views": lambda s: s.views,
        "recent": lambda s: s.published_at,
        "percentile": lambda s: s.percentile,
        "baseline": lambda s: s.baseline_views,
        "duration": lambda s: s.age_days,
        "title": lambda s: (s.title or "").lower(),
        "channel": lambda s: ((s.channel_title or "").lower(), -s.multiplier),
    }
    scores.sort(key=sort_keys[sort], reverse=(direction == "desc"))

    total = len(scores)
    # Summary figures are computed over the whole matching set, never over the
    # returned page. They are independent of both the sort column and the limit:
    # "top multiplier" means the highest that matched, not whatever happens to
    # be in row one after sorting by title.
    return {
        "results": [s.to_dict() for s in scores[:limit]],
        "total": total,
        "shown": min(total, limit),
        "outlier_count": sum(1 for s in scores if s.is_outlier),
        "max_multiplier": round(max((s.multiplier for s in scores), default=0.0), 2),
        "unreliable_count": sum(1 for s in scores if not s.reliable),
        "channels_matched": len({s.channel_id for s in scores}),
        "baselines": {k: b.to_dict() for k, b in baselines.items()},
        "threshold": config.outlier_threshold,
        # Always reported, so a hidden result is never a mystery.
        "excluded": len(dropped),
        "exclusions": dropped[:50],
        "exclusions_active": rules.active,
    }


# --------------------------------------------------------------------------
# Trailing baselines
# --------------------------------------------------------------------------


def _window_from(kind: str | None, count: int | None, days: int | None) -> trailing.TrailingWindow:
    config = get_settings()
    return trailing.TrailingWindow(
        kind=(kind or config.trailing_window_kind),  # type: ignore[arg-type]
        count=count or config.trailing_window_count,
        days=days or config.trailing_window_days,
    )


@router.get("/trailing")
def trailing_scores(
    channel_id: list[str] | None = Query(None),
    window_kind: Literal["count", "days"] | None = Query(None),
    window_count: int | None = Query(None, ge=2, le=200),
    window_days: int | None = Query(None, ge=7, le=3650),
    min_prior: int | None = Query(None, ge=2, le=100),
    fmt: Literal["all", "long", "short"] = Query("all", alias="format"),
    sort: Literal["trailing", "catalog", "drift", "recent", "views"] = Query("trailing"),
    direction: Literal["asc", "desc"] = Query("desc"),
    only_sufficient: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Every video scored against what was normal for its channel at the time.

    Reads stored data only, so re-running with a different window costs nothing
    and can be done as often as you like.
    """
    config = get_settings()
    window = _window_from(window_kind, window_count, window_days)
    minimum = min_prior or config.trailing_min_prior

    channels = list(session.scalars(
        select(Channel).where(Channel.id.in_(channel_id)) if channel_id
        else select(Channel).where(
            (Channel.is_tracked.is_(True)) | (Channel.is_owned.is_(True))
        )
    ).all())

    all_scores: list[trailing.TrailingScore] = []
    summaries: list[dict[str, Any]] = []

    for channel in channels:
        videos = list(session.scalars(
            select(Video).where(Video.channel_id == channel.id)
        ).all())
        if not videos:
            continue

        scores = trailing.score_channel_trailing(
            videos, channel_title=channel.title, window=window,
            min_prior=minimum, min_age_days=config.baseline_min_age_days,
        )
        trailing.detect_breakouts(
            scores, window=config.breakout_window,
            threshold=config.breakout_threshold,
            revert_ratio=config.breakout_revert_ratio,
        )
        summaries.append(trailing.summarise_channel(
            scores, channel_id=channel.id, channel_title=channel.title,
            window=window.describe(),
        ).to_dict())
        all_scores.extend(scores)

    if fmt == "long":
        all_scores = [s for s in all_scores if not s.is_short]
    elif fmt == "short":
        all_scores = [s for s in all_scores if s.is_short]
    if only_sufficient:
        all_scores = [s for s in all_scores if s.sufficient]

    keys = {
        "trailing": lambda s: s.trailing_multiplier or -1,
        "catalog": lambda s: s.catalog_multiplier or -1,
        "drift": lambda s: s.drift or -1,
        "recent": lambda s: s.published_at,
        "views": lambda s: s.views,
    }
    all_scores.sort(key=keys[sort], reverse=(direction == "desc"))

    return {
        "results": [s.to_dict() for s in all_scores[:limit]],
        "total": len(all_scores),
        "shown": min(len(all_scores), limit),
        "summaries": summaries,
        "window": {
            "kind": window.kind, "count": window.count, "days": window.days,
            "description": window.describe(), "min_prior": minimum,
        },
        "breakout": {
            "window": config.breakout_window,
            "threshold": config.breakout_threshold,
            "revert_ratio": config.breakout_revert_ratio,
        },
    }


@router.get("/trailing/compare")
def trailing_comparison(
    channel_id: list[str] | None = Query(None),
    window_kind: Literal["count", "days"] | None = Query(None),
    window_count: int | None = Query(None, ge=2, le=200),
    window_days: int | None = Query(None, ge=7, le=3650),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """How much the trailing method disagrees with the catalogue one.

    The point of the exercise: if the two agree, the channel was stable and
    either method works. Where they diverge, the catalogue figure was reading
    growth as performance.
    """
    body = trailing_scores(
        channel_id=channel_id, window_kind=window_kind, window_count=window_count,
        window_days=window_days, min_prior=None, fmt="all", sort="drift",
        direction="desc", only_sufficient=True, limit=1000, session=session,
    )

    rows = [r for r in body["results"] if r["drift"] is not None]
    biggest_gain = [r for r in rows if r["drift"] >= 1.5][:15]
    biggest_drop = sorted(rows, key=lambda r: r["drift"])[:15]

    return {
        "window": body["window"],
        "summaries": body["summaries"],
        "compared": len(rows),
        "median_drift": (
            round(float(np.median([r["drift"] for r in rows])), 2) if rows else None
        ),
        "understated_by_catalog": biggest_gain,
        "overstated_by_catalog": [r for r in biggest_drop if r["drift"] < 0.67],
        "reading": (
            "Drift above 1 means the video looks better against its own era than "
            "against the whole catalogue — the catalogue figure was holding it to a "
            "bar the channel only reached later. Below 1 means the opposite: the "
            "catalogue median is inflated by a later run of bigger videos."
        ),
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
            raise NotFound(
                "YouTube returned nothing usable for that video.",
                "It may be private or deleted, or its channel may no longer exist.",
            )
        video = videos[0]

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
        # Measured locally, free, and present on every analysed thumbnail.
        "small_contrast": row.small_contrast,
        "detail_retention": row.detail_retention,
        "weight_x": row.weight_x,
        "weight_y": row.weight_y,
        "composition_note": row.composition_note,
        "text_area_estimate": row.text_area_estimate,
        "text_bands": row.text_bands or [],
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


@router.get("/thumbnails/image")
def thumbnail_image(url: str = Query(..., description="The thumbnail URL that was analysed")):
    """Serve the cached copy of a thumbnail that was actually analysed.

    The obvious alternative — pointing an ``<img>`` straight at YouTube — has a
    real flaw: it shows whatever the URL serves *now*, beside numbers computed
    from what it served *then*. Serving the cached file guarantees the picture
    and the measurements describe the same image.

    It also means the legibility view keeps working offline, and doesn't depend
    on YouTube's CDN allowing hotlinks.

    Path traversal is impossible: the filename is derived by hashing the URL
    here, so nothing the caller sends reaches the filesystem verbatim.
    """
    path = thumbnails._cache_path(url)
    if not path.exists():
        raise NotFound(
            "That thumbnail hasn't been downloaded yet.",
            "Run the analysis on this set first — the image is cached as part of it.",
        )
    return FileResponse(
        path,
        media_type="image/jpeg",
        # Cache hard: the file is immutable, keyed on a URL YouTube itself
        # replaces rather than reuses.
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@router.get("/thumbnails/analyses")
def stored_thumbnail_analyses(
    min_multiplier: float = Query(3.0, ge=0),
    channel_id: list[str] | None = Query(None),
    limit: int = Query(60, ge=1, le=300),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Thumbnail analyses already computed, newest first. Costs nothing.

    Reads stored rows rather than re-analysing, so the legibility view can be
    opened as often as you like without re-downloading a single image.
    """
    query = (
        select(ThumbnailAnalysis)
        .join(Video, Video.id == ThumbnailAnalysis.video_id)
        .where(Video.outlier_multiplier >= min_multiplier)
    )
    if channel_id:
        query = query.where(Video.channel_id.in_(channel_id))
    rows = list(session.scalars(query.limit(limit)).all())
    return {"results": [_thumbnail_dict(r) for r in rows], "count": len(rows)}


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

        # ingest_videos resolves the channels behind these results first — it
        # has to, since a video row cannot exist without its channel row — so
        # the denominators for the multipliers below are already stored.
        videos, _ = ingest.ingest_videos(
            client, session, video_ids, shorts_max_seconds=config.shorts_max_seconds
        )
        channel_ids = list({v.channel_id for v in videos if v.channel_id})

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

        # Applied after ingestion, deliberately. The videos were already paid
        # for by the search, and storing them means a later change of mind
        # about the exclusions costs nothing to act on.
        rules = exclusions.from_settings(config)
        dropped: list[dict[str, Any]] = []
        if rules.active:
            videos, dropped = exclusions.partition(videos, rules, channels)

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
            "excluded": len(dropped),
            "exclusions": dropped[:50],
            "exclusions_active": rules.active,
        }
    finally:
        client.close()
