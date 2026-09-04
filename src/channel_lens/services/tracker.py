"""Watchlist polling, velocity, and change detection.

This is the module that turns the app from a snapshot tool into a record. A
single query to YouTube tells you a video has 40,000 views; a month of stored
snapshots tells you it got 30,000 of them in the last four days, which is the
only version of that fact worth acting on.

It is also the only way to observe a competitor's packaging experiments. When a
creator swaps a thumbnail, YouTube publishes no notice — but the URL changes,
and if you were already watching, you have view counts either side of the swap.
That before/after is the closest thing to a competitor's A/B test results that
exists outside their own analytics.

Polling is cheap by design: ``videos.list`` costs 1 unit per 50 videos, so a
200-video watchlist costs 4 units per cycle and roughly 16 units a day at the
default six-hour interval. That is 0.2% of the daily budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Channel, JobRun, Video, VideoRevision, VideoStat, WatchlistEntry
from ..youtube.client import YouTubeClient
from ..youtube.errors import YouTubeError
from .ingest import ingest_videos

log = logging.getLogger(__name__)

#: Window either side of a change used for the before/after velocity read.
CHANGE_WINDOW = timedelta(days=3)


@dataclass
class VelocityReading:
    """Views per day over a window, with the evidence it rests on."""

    views_per_day: float
    window_hours: float
    start_views: int
    end_views: int
    sample_count: int

    @property
    def reliable(self) -> bool:
        # Under six hours, YouTube's own counter rounding dominates the signal.
        return self.window_hours >= 6.0 and self.sample_count >= 2

    def to_dict(self) -> dict:
        return {
            "views_per_day": round(self.views_per_day, 1),
            "window_hours": round(self.window_hours, 1),
            "start_views": self.start_views,
            "end_views": self.end_views,
            "sample_count": self.sample_count,
            "reliable": self.reliable,
        }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def compute_velocity(
    stats: list[VideoStat], *, since: datetime | None = None, until: datetime | None = None
) -> VelocityReading | None:
    """Views per day between the first and last snapshot in a window.

    Returns ``None`` when fewer than two snapshots fall inside it — a single
    reading is a level, not a rate, and reporting it as one would be a lie.
    """
    ordered = sorted(stats, key=lambda s: _aware(s.captured_at))
    if since:
        ordered = [s for s in ordered if _aware(s.captured_at) >= since]
    if until:
        ordered = [s for s in ordered if _aware(s.captured_at) <= until]
    if len(ordered) < 2:
        return None

    first, last = ordered[0], ordered[-1]
    seconds = (_aware(last.captured_at) - _aware(first.captured_at)).total_seconds()
    if seconds <= 0:
        return None

    delta_views = last.view_count - first.view_count
    return VelocityReading(
        views_per_day=delta_views / (seconds / 86400.0),
        window_hours=seconds / 3600.0,
        start_views=first.view_count,
        end_views=last.view_count,
        sample_count=len(ordered),
    )


def annotate_revision(session: Session, revision: VideoRevision) -> VideoRevision:
    """Fill in a revision's before/after velocity once enough data exists.

    Deliberately conservative. Both sides need at least two snapshots inside
    their window, otherwise the fields stay null and the UI says "not enough
    data yet" rather than printing a number built from one reading.
    """
    detected = _aware(revision.detected_at)
    stats = list(
        session.scalars(
            select(VideoStat).where(VideoStat.video_id == revision.video_id)
        ).all()
    )

    before = compute_velocity(
        stats, since=detected - CHANGE_WINDOW, until=detected
    )
    after = compute_velocity(stats, since=detected, until=detected + CHANGE_WINDOW)

    if before and before.reliable:
        revision.velocity_before = before.views_per_day
    if after and after.reliable:
        revision.velocity_after = after.views_per_day
    session.flush()
    return revision


def describe_revision_effect(revision: VideoRevision) -> str:
    """Plain-language reading of a change's before/after velocity.

    Always hedged, because this is observational: a thumbnail swap and a change
    in views are correlated here, never proven causal. YouTube's own promotion
    decisions move far more traffic than any thumbnail, and a creator who swaps
    a thumbnail is often reacting to a change in performance rather than
    causing one.

    >>> r = VideoRevision(field="thumbnail", velocity_before=100.0, velocity_after=400.0)
    >>> describe_revision_effect(r)
    'Views ran 4.0x faster after this change (100/day before, 400/day after). Correlation only — YouTube may have changed how it was promoting the video for unrelated reasons.'
    >>> r2 = VideoRevision(field="title", velocity_before=None, velocity_after=None)
    >>> describe_revision_effect(r2)
    'Not enough snapshots either side of this change to compare.'
    >>> r3 = VideoRevision(field="title", velocity_before=100.0, velocity_after=105.0)
    >>> describe_revision_effect(r3)
    'No meaningful change in pace (100/day before, 105/day after).'
    """
    before, after = revision.velocity_before, revision.velocity_after
    if before is None or after is None:
        return "Not enough snapshots either side of this change to compare."
    if before <= 0:
        return f"Views were flat before this change, then ran at {after:.0f}/day."

    ratio = after / before
    if 0.85 <= ratio <= 1.15:
        return f"No meaningful change in pace ({before:.0f}/day before, {after:.0f}/day after)."

    direction = "faster" if ratio > 1 else "slower"
    factor = ratio if ratio > 1 else 1 / ratio
    return (
        f"Views ran {factor:.1f}x {direction} after this change "
        f"({before:.0f}/day before, {after:.0f}/day after). Correlation only — "
        f"YouTube may have changed how it was promoting the video for unrelated reasons."
    )


def watchlist_video_ids(session: Session, *, max_age_days: int) -> list[str]:
    """Every video the tracker should poll this cycle.

    Comes from three places: explicitly watched videos, all videos belonging to
    watched channels, and the user's own videos. Videos past ``max_age_days``
    are dropped so the per-cycle cost stays flat as history accumulates.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    explicit = set(
        session.scalars(
            select(WatchlistEntry.value).where(
                WatchlistEntry.kind == "video", WatchlistEntry.active.is_(True)
            )
        ).all()
    )

    from_channels = set(
        session.scalars(
            select(Video.id)
            .join(Channel, Channel.id == Video.channel_id)
            .where(
                (Channel.is_tracked.is_(True)) | (Channel.is_owned.is_(True)),
                Video.published_at >= cutoff_naive,
            )
        ).all()
    )

    return sorted(explicit | from_channels)


def run_poll_cycle(
    client: YouTubeClient,
    session: Session,
    *,
    shorts_max_seconds: int,
    max_video_age_days: int,
) -> JobRun:
    """One tracker pass: snapshot every watched video, record any changes.

    Records a :class:`JobRun` either way, so a failure is visible in the UI
    afterwards rather than only in a log nobody opens.
    """
    job = JobRun(job="tracker.poll", status="running")
    session.add(job)
    session.flush()

    try:
        video_ids = watchlist_video_ids(session, max_age_days=max_video_age_days)
        if not video_ids:
            job.status = "ok"
            job.detail = "Nothing on the watchlist to poll."
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.flush()
            return job

        _videos, revisions = ingest_videos(
            client, session, video_ids, shorts_max_seconds=shorts_max_seconds
        )

        # Backfill before/after readings on any revision old enough to have them.
        pending = session.scalars(
            select(VideoRevision).where(VideoRevision.velocity_after.is_(None))
        ).all()
        for revision in pending:
            annotate_revision(session, revision)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for entry in session.scalars(
            select(WatchlistEntry).where(WatchlistEntry.active.is_(True))
        ).all():
            entry.last_polled_at = now
            entry.last_error = None

        job.status = "ok"
        job.items_processed = len(video_ids)
        job.units_spent = client.units_spent
        changed = len(revisions)
        job.detail = (
            f"Polled {len(video_ids)} videos, {changed} change"
            f"{'' if changed == 1 else 's'} detected."
        )
        job.finished_at = now
        session.flush()
        return job

    except YouTubeError as exc:
        job.status = "error"
        job.error = f"{exc.message} {exc.hint}".strip()
        job.units_spent = client.units_spent
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        return job
    except Exception as exc:  # noqa: BLE001 - a tracker cycle must never kill the app
        log.exception("Tracker cycle failed")
        job.status = "error"
        job.error = str(exc)
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.flush()
        return job


def recent_changes(session: Session, *, days: int = 30, limit: int = 100) -> list[dict]:
    """Detected title and thumbnail changes, newest first, with their effect."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    rows = session.scalars(
        select(VideoRevision)
        .where(VideoRevision.detected_at >= cutoff)
        .order_by(VideoRevision.detected_at.desc())
        .limit(limit)
    ).all()

    results: list[dict] = []
    for revision in rows:
        video = session.get(Video, revision.video_id)
        channel = session.get(Channel, video.channel_id) if video else None
        results.append(
            {
                "id": revision.id,
                "video_id": revision.video_id,
                "video_title": video.title if video else "",
                "channel_title": channel.title if channel else "",
                "detected_at": _aware(revision.detected_at).isoformat(),
                "field": revision.field,
                "old_value": revision.old_value,
                "new_value": revision.new_value,
                "views_at_change": revision.views_at_change,
                "velocity_before": revision.velocity_before,
                "velocity_after": revision.velocity_after,
                "effect": describe_revision_effect(revision),
                "url": f"https://www.youtube.com/watch?v={revision.video_id}",
            }
        )
    return results


def video_history(session: Session, video_id: str) -> dict:
    """Full stored history for one video: snapshots, changes, and velocity."""
    video = session.get(Video, video_id)
    if video is None:
        return {}

    stats = list(
        session.scalars(
            select(VideoStat)
            .where(VideoStat.video_id == video_id)
            .order_by(VideoStat.captured_at)
        ).all()
    )
    revisions = list(
        session.scalars(
            select(VideoRevision)
            .where(VideoRevision.video_id == video_id)
            .order_by(VideoRevision.detected_at)
        ).all()
    )

    now = datetime.now(timezone.utc)
    overall = compute_velocity(stats)
    last_week = compute_velocity(stats, since=now - timedelta(days=7))

    return {
        "video_id": video_id,
        "title": video.title,
        "snapshots": [
            {
                "captured_at": _aware(s.captured_at).isoformat(),
                "views": s.view_count,
                "likes": s.like_count,
                "comments": s.comment_count,
            }
            for s in stats
        ],
        "revisions": [
            {
                "detected_at": _aware(r.detected_at).isoformat(),
                "field": r.field,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "effect": describe_revision_effect(r),
            }
            for r in revisions
        ],
        "velocity_overall": overall.to_dict() if overall else None,
        "velocity_7d": last_week.to_dict() if last_week else None,
    }
