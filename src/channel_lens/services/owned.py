"""Your own channel: syncing owner analytics, and reading them.

The diagnosis this module exists for is the **CTR × retention cross-read**, and
it is the one piece of analysis in the app that rests entirely on measurements
rather than inference.

Four quadrants, against your own channel's medians:

* **Low CTR, high retention** — the most valuable finding available. People who
  click stay, so the video is good; not enough people click. That is a
  packaging problem with a known fix, and the upside is bounded only by how
  many impressions you're already getting for free.
* **High CTR, low retention** — the packaging outruns the video. Often
  self-limiting: YouTube reads the early exits and stops showing it.
* **High CTR, high retention** — the format to repeat.
* **Low CTR, low retention** — the topic or execution, not the packaging.

No composite score is produced. A single "video health: 72" would collapse
exactly the distinction that makes this useful, because the two failing
quadrants need opposite responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Channel, OwnedTrafficDaily, OwnedVideoDaily, Video
from ..youtube import analytics

log = logging.getLogger(__name__)

#: Traffic sources where the thumbnail and title compete directly against other
#: videos. Weak CTR here is a packaging signal; weak CTR on search is not, since
#: search intent does most of the work.
PACKAGING_SOURCES = {"SUGGESTED", "BROWSE", "RELATED_VIDEO", "YT_CHANNEL"}


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def sync_analytics(
    session: Session, *, days: int = 90, http=None
) -> dict:
    """Pull Analytics API v2 data for the owned channel into the database.

    Costs no YouTube Data API quota. Upserts on ``(video_id, day)``, so
    re-running over an overlapping range is safe and corrects any figures
    Google has since revised — which it does, for roughly 72 hours after the
    fact.
    """
    end = date.today()
    start = end - timedelta(days=days)

    daily_rows = analytics.video_totals(start, end, http=http)
    written = 0
    for row in daily_rows:
        video_id = row.get("video")
        if not video_id:
            continue
        existing = session.scalar(
            select(OwnedVideoDaily).where(
                OwnedVideoDaily.video_id == video_id, OwnedVideoDaily.day == end
            )
        )
        record = existing or OwnedVideoDaily(video_id=video_id, day=end)
        record.views = _as_int(row.get("views"))
        record.estimated_minutes_watched = _as_float(row.get("estimatedMinutesWatched"))
        record.average_view_duration_seconds = _as_float(row.get("averageViewDuration"))
        record.average_view_percentage = _as_float(row.get("averageViewPercentage"))
        record.subscribers_gained = _as_int(row.get("subscribersGained"))
        record.subscribers_lost = _as_int(row.get("subscribersLost"))
        record.likes = _as_int(row.get("likes"))
        record.comments = _as_int(row.get("comments"))
        record.shares = _as_int(row.get("shares"))
        if existing is None:
            session.add(record)
        written += 1

    traffic_rows = analytics.traffic_sources(start, end, http=http)
    for row in traffic_rows:
        source = row.get("insightTrafficSourceType")
        if not source:
            continue
        existing = session.scalar(
            select(OwnedTrafficDaily).where(
                OwnedTrafficDaily.video_id == "",
                OwnedTrafficDaily.day == end,
                OwnedTrafficDaily.source_type == source,
            )
        )
        record = existing or OwnedTrafficDaily(video_id="", day=end, source_type=source)
        record.views = _as_int(row.get("views"))
        record.estimated_minutes_watched = _as_float(row.get("estimatedMinutesWatched"))
        if existing is None:
            session.add(record)

    session.flush()
    return {
        "videos_updated": written,
        "traffic_rows": len(traffic_rows),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
    }


def sync_ctr(session: Session, *, http=None) -> dict:
    """Import thumbnail impressions and CTR from the Reporting API.

    Returns a status dict rather than raising when the report isn't ready —
    "Google hasn't generated it yet" is an expected state for the first 48
    hours, not an error.
    """
    status = analytics.reach_job_status(http=http)
    if not status.get("job_exists"):
        return {"imported": 0, "status": status}
    if not status.get("reports_available"):
        return {"imported": 0, "status": status}

    reports = analytics.list_reports(status["job_id"], http=http)
    imported = 0
    # Newest first; a handful of recent days is enough to keep current without
    # re-downloading the entire 60-day retention window on every sync.
    for report in sorted(
        reports, key=lambda r: r.get("createTime", ""), reverse=True
    )[:10]:
        url = report.get("downloadUrl")
        if not url:
            continue
        try:
            rows = analytics.parse_reach_rows(analytics.download_report(url, http=http))
        except analytics.AnalyticsError as exc:
            log.warning("Could not download reach report: %s", exc)
            continue

        for row in rows:
            existing = session.scalar(
                select(OwnedVideoDaily).where(
                    OwnedVideoDaily.video_id == row["video_id"],
                    OwnedVideoDaily.day == row["day"],
                )
            )
            record = existing or OwnedVideoDaily(
                video_id=row["video_id"], day=row["day"]
            )
            record.impressions = row["impressions"]
            record.impressions_ctr = row["impressions_ctr"]
            if existing is None:
                session.add(record)
            imported += 1

    session.flush()
    return {"imported": imported, "status": status}


@dataclass
class VideoDiagnosis:
    """One owned video's CTR and retention, read against the channel's own norms."""

    video_id: str
    title: str
    thumbnail_url: str | None
    published_at: datetime | None
    views: int
    impressions: int | None
    ctr: float | None
    average_view_percentage: float | None
    average_view_duration_seconds: float | None

    #: "packaging", "overpromise", "working", "topic", or "unknown".
    quadrant: str
    headline: str
    detail: str
    #: Extra impressions-to-views upside if CTR reached the channel median.
    upside_views: int | None = None
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "thumbnail_url": self.thumbnail_url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "views": self.views,
            "impressions": self.impressions,
            "ctr": round(self.ctr, 2) if self.ctr is not None else None,
            "average_view_percentage": (
                round(self.average_view_percentage, 1)
                if self.average_view_percentage is not None
                else None
            ),
            "average_view_duration_seconds": self.average_view_duration_seconds,
            "quadrant": self.quadrant,
            "headline": self.headline,
            "detail": self.detail,
            "upside_views": self.upside_views,
            "caveats": self.caveats,
            "url": f"https://www.youtube.com/watch?v={self.video_id}",
        }


QUADRANT_LABELS = {
    "packaging": "Packaging is holding this back",
    "overpromise": "Packaging outruns the video",
    "working": "This formula works",
    "topic": "Topic or execution, not packaging",
    "unknown": "Not enough data",
}


def diagnose(
    session: Session, *, days: int = 90, min_impressions: int = 500
) -> tuple[list[VideoDiagnosis], dict]:
    """Cross-read CTR against retention for every owned video with enough data.

    Thresholds are the channel's own medians, not industry benchmarks. A "good"
    CTR is entirely dependent on niche, channel size, and how much browse
    traffic you get, so an absolute number imported from a blog post would be
    worse than useless.
    """
    cutoff = date.today() - timedelta(days=days)
    rows = list(
        session.scalars(
            select(OwnedVideoDaily).where(OwnedVideoDaily.day >= cutoff)
        ).all()
    )
    if not rows:
        return [], {"reason": "No owner analytics have been synced yet."}

    # Collapse to one record per video — the newest day carrying each field.
    by_video: dict[str, OwnedVideoDaily] = {}
    for row in sorted(rows, key=lambda r: r.day):
        current = by_video.get(row.video_id)
        if current is None:
            by_video[row.video_id] = row
            continue
        # Merge: keep the newest non-null value for each metric.
        for attribute in (
            "views", "impressions", "impressions_ctr",
            "average_view_percentage", "average_view_duration_seconds",
        ):
            value = getattr(row, attribute)
            if value is not None:
                setattr(current, attribute, value)

    scored = [
        r for r in by_video.values()
        if r.impressions_ctr is not None
        and r.average_view_percentage is not None
        and (r.impressions or 0) >= min_impressions
    ]

    if len(scored) < 3:
        partial = [
            _diagnose_one(session, r, None, None) for r in by_video.values()
        ]
        return partial, {
            "reason": (
                f"Only {len(scored)} video{'s' if len(scored) != 1 else ''} have both CTR "
                f"and retention with at least {min_impressions:,} impressions. "
                f"The quadrant read needs at least 3 to establish your channel's own "
                f"medians — it will appear as more data arrives."
            ),
            "median_ctr": None,
            "median_retention": None,
        }

    median_ctr = float(np.median([r.impressions_ctr for r in scored]))
    median_retention = float(np.median([r.average_view_percentage for r in scored]))

    diagnoses = [
        _diagnose_one(session, row, median_ctr, median_retention)
        for row in by_video.values()
    ]
    # Biggest recoverable upside first; that is the point of the screen.
    diagnoses.sort(key=lambda d: (d.upside_views or 0), reverse=True)

    return diagnoses, {
        "median_ctr": round(median_ctr, 2),
        "median_retention": round(median_retention, 1),
        "sample_size": len(scored),
        "reason": None,
    }


def _diagnose_one(
    session: Session,
    row: OwnedVideoDaily,
    median_ctr: float | None,
    median_retention: float | None,
) -> VideoDiagnosis:
    video = session.get(Video, row.video_id)
    title = video.title if video else row.video_id
    thumbnail = video.thumbnail_url if video else None
    published = video.published_at if video else None

    ctr = row.impressions_ctr
    retention = row.average_view_percentage
    caveats: list[str] = []

    if ctr is None or retention is None or median_ctr is None or median_retention is None:
        missing = []
        if ctr is None:
            missing.append("CTR")
        if retention is None:
            missing.append("retention")
        detail = (
            f"Missing {' and '.join(missing)}. CTR arrives through Google's bulk "
            f"reporting, which takes up to 48 hours to start after you enable it."
            if "CTR" in missing
            else "Not enough analytics synced for this video yet."
        )
        return VideoDiagnosis(
            video_id=row.video_id, title=title, thumbnail_url=thumbnail,
            published_at=published, views=row.views, impressions=row.impressions,
            ctr=ctr, average_view_percentage=retention,
            average_view_duration_seconds=row.average_view_duration_seconds,
            quadrant="unknown", headline=QUADRANT_LABELS["unknown"], detail=detail,
        )

    high_ctr = ctr >= median_ctr
    high_retention = retention >= median_retention

    if (row.impressions or 0) < 1000:
        caveats.append(
            f"Only {row.impressions:,} impressions — CTR is noisy at this volume."
        )

    upside: int | None = None
    if not high_ctr and row.impressions:
        # Views this video would have had at the channel's median CTR.
        gain = row.impressions * (median_ctr - ctr) / 100.0
        upside = int(max(0, gain))

    if high_ctr and high_retention:
        quadrant = "working"
        detail = (
            f"CTR {ctr:.1f}% beats your median of {median_ctr:.1f}%, and viewers stay "
            f"for {retention:.0f}% against your median {median_retention:.0f}%. "
            f"Whatever this one is doing — subject, framing, title angle — is the "
            f"pattern worth repeating."
        )
    elif not high_ctr and high_retention:
        quadrant = "packaging"
        detail = (
            f"Viewers who click stay ({retention:.0f}% vs your median "
            f"{median_retention:.0f}%), but only {ctr:.1f}% click against your median "
            f"{median_ctr:.1f}%. The video is working; the thumbnail and title aren't "
            f"getting it opened."
        )
        if upside:
            detail += (
                f" At your median CTR, the impressions it already has would have "
                f"produced roughly {upside:,} more views."
            )
    elif high_ctr and not high_retention:
        quadrant = "overpromise"
        detail = (
            f"Strong CTR at {ctr:.1f}% (median {median_ctr:.1f}%), but retention is "
            f"{retention:.0f}% against your median {median_retention:.0f}%. The "
            f"packaging is promising something the video doesn't deliver, and YouTube "
            f"reads those early exits."
        )
    else:
        quadrant = "topic"
        detail = (
            f"CTR {ctr:.1f}% and retention {retention:.0f}% are both below your medians "
            f"({median_ctr:.1f}% / {median_retention:.0f}%). New packaging is unlikely "
            f"to rescue this one — the subject or the execution is the issue."
        )

    return VideoDiagnosis(
        video_id=row.video_id, title=title, thumbnail_url=thumbnail,
        published_at=published, views=row.views, impressions=row.impressions,
        ctr=ctr, average_view_percentage=retention,
        average_view_duration_seconds=row.average_view_duration_seconds,
        quadrant=quadrant, headline=QUADRANT_LABELS[quadrant], detail=detail,
        upside_views=upside, caveats=caveats,
    )


def owned_channel(session: Session) -> Channel | None:
    return session.scalar(select(Channel).where(Channel.is_owned.is_(True)))
