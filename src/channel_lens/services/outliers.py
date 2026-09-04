"""Outlier scoring: which videos beat what their channel normally does.

This is the metric the paid tools are built around, and it is worth being
precise about what it does and doesn't say.

**Why relative, not absolute.** 200,000 views is a catastrophe for a channel
that averages two million and a career-changing week for one that averages
three thousand. Dividing by the channel's own baseline is what makes a small
creator's breakout visible next to a large one's routine upload.

**Why the median, not the mean.** One viral video drags a mean upward for
months, which raises the bar for everything after it and hides the next
breakout — precisely when you most want to see it. The median barely moves.

**Why young videos are excluded from the baseline but still scored.** A video
posted yesterday has most of its views ahead of it. Averaging it into the
baseline drags the baseline down and inflates every other video's multiplier.
So recent uploads are scored against the baseline but never form part of it,
and a maturity adjustment is offered separately — clearly labelled as the
projection it is, never blended silently into the headline number.

**Shorts and long-form are scored apart.** Their view distributions differ by
an order of magnitude. Pooling them produces a baseline that describes neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Channel, Video

log = logging.getLogger(__name__)

#: Share of a video's eventual 90-day views typically accrued by day N.
#:
#: These are rounded, widely-reproduced figures for the shape of YouTube view
#: accrual, not a measurement of any specific channel. They exist to keep a
#: three-day-old video from being called a flop, and they are only ever applied
#: to a clearly-labelled "projected" figure — never to the headline multiplier.
#: Once a channel has enough snapshot history, :func:`empirical_maturity_curve`
#: replaces them with that channel's own observed shape.
DEFAULT_MATURITY_CURVE: list[tuple[int, float]] = [
    (1, 0.18),
    (2, 0.28),
    (3, 0.36),
    (7, 0.55),
    (14, 0.71),
    (21, 0.81),
    (30, 0.88),
    (60, 0.96),
    (90, 1.00),
]


def maturity_fraction(age_days: float, curve: list[tuple[int, float]] | None = None) -> float:
    """Fraction of eventual views a video of this age has typically accrued.

    Linearly interpolated between the curve's control points, clamped at both
    ends. Never returns 0, so it is always safe to divide by.

    >>> round(maturity_fraction(1), 2)
    0.18
    >>> round(maturity_fraction(90), 2)
    1.0
    >>> round(maturity_fraction(200), 2)
    1.0
    >>> round(maturity_fraction(10.5), 3)
    0.63
    >>> maturity_fraction(0) > 0
    True
    """
    points = curve or DEFAULT_MATURITY_CURVE
    age = max(0.5, float(age_days))
    if age <= points[0][0]:
        return points[0][1]
    if age >= points[-1][0]:
        return points[-1][1]
    for (day_a, frac_a), (day_b, frac_b) in zip(points, points[1:]):
        if day_a <= age <= day_b:
            span = day_b - day_a
            weight = (age - day_a) / span if span else 0.0
            return frac_a + weight * (frac_b - frac_a)
    return points[-1][1]


@dataclass
class Baseline:
    """What "normal" means for one channel in one format.

    Carries its own confidence, because a baseline built from four videos is a
    guess and the UI must be able to say so rather than presenting a decimal
    that looks equally authoritative either way.
    """

    channel_id: str
    #: "long" or "short".
    format: str
    median_views: float
    mean_views: float
    #: 25th and 75th percentile — the band a typical upload lands in.
    p25_views: float
    p75_views: float
    sample_size: int
    #: True when the sample is big enough to trust the multiplier.
    reliable: bool
    #: Plain-language reasons the baseline may mislead. Displayed verbatim.
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "format": self.format,
            "median_views": round(self.median_views),
            "mean_views": round(self.mean_views),
            "p25_views": round(self.p25_views),
            "p75_views": round(self.p75_views),
            "sample_size": self.sample_size,
            "reliable": self.reliable,
            "caveats": self.caveats,
        }


@dataclass
class VideoScore:
    """One video's performance against its channel's baseline."""

    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    age_days: float
    views: int
    is_short: bool
    thumbnail_url: str | None

    baseline_views: float
    #: views ÷ baseline. The headline number.
    multiplier: float
    #: Where this video sits among its channel's recent uploads, 0–100.
    percentile: float
    #: Multiplier the video is on course for, given its age. A projection.
    projected_multiplier: float | None
    is_outlier: bool
    reliable: bool
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "published_at": self.published_at.isoformat(),
            "age_days": round(self.age_days, 1),
            "views": self.views,
            "is_short": self.is_short,
            "thumbnail_url": self.thumbnail_url,
            "baseline_views": round(self.baseline_views),
            "multiplier": round(self.multiplier, 2),
            "percentile": round(self.percentile, 1),
            "projected_multiplier": (
                round(self.projected_multiplier, 2)
                if self.projected_multiplier is not None
                else None
            ),
            "is_outlier": self.is_outlier,
            "reliable": self.reliable,
            "caveats": self.caveats,
            "url": f"https://www.youtube.com/watch?v={self.video_id}",
        }


def _age_days(published_at: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    published = published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return max(0.0, (now - published).total_seconds() / 86400.0)


def compute_baseline(
    videos: list[Video],
    *,
    channel_id: str,
    is_short: bool,
    min_age_days: int,
    min_videos: int,
    window: int,
    now: datetime | None = None,
) -> Baseline | None:
    """Build a baseline from a channel's mature uploads in one format.

    Returns ``None`` when there is nothing usable to build from — the caller
    then reports "not enough history" rather than inventing a denominator.
    """
    now = now or datetime.now(timezone.utc)
    fmt = "short" if is_short else "long"

    eligible = [
        v
        for v in videos
        if v.is_short == is_short
        and not v.is_live
        and v.latest_view_count is not None
        and v.published_at is not None
        and _age_days(v.published_at, now) >= min_age_days
    ]
    eligible.sort(key=lambda v: v.published_at, reverse=True)
    eligible = eligible[:window]

    if not eligible:
        return None

    counts = np.array([float(v.latest_view_count or 0) for v in eligible])
    caveats: list[str] = []
    reliable = len(eligible) >= min_videos

    if not reliable:
        caveats.append(
            f"Baseline built from only {len(eligible)} mature "
            f"{'Short' if is_short else 'long-form'} upload"
            f"{'s' if len(eligible) != 1 else ''} — treat multipliers as rough."
        )

    spread = float(np.percentile(counts, 75)) - float(np.percentile(counts, 25))
    median = float(np.median(counts))
    if median > 0 and spread > 3 * median:
        caveats.append(
            "This channel's views vary enormously upload to upload, so a high "
            "multiplier here is weaker evidence than it would be elsewhere."
        )

    return Baseline(
        channel_id=channel_id,
        format=fmt,
        median_views=median,
        mean_views=float(np.mean(counts)),
        p25_views=float(np.percentile(counts, 25)),
        p75_views=float(np.percentile(counts, 75)),
        sample_size=len(eligible),
        reliable=reliable,
        caveats=caveats,
    )


def empirical_maturity_curve(session: Session, channel_id: str) -> list[tuple[int, float]] | None:
    """Derive a channel's own view-accrual shape from stored snapshots.

    Only returns a curve when there is enough snapshot history to beat the
    default; otherwise the generic curve is more honest than a shape fitted to
    three data points.
    """
    from ..models import VideoStat

    rows = session.execute(
        select(Video.id, Video.published_at, VideoStat.captured_at, VideoStat.view_count)
        .join(VideoStat, VideoStat.video_id == Video.id)
        .where(Video.channel_id == channel_id, Video.is_short.is_(False))
    ).all()
    if len(rows) < 40:
        return None

    # Group by video, take each video's newest snapshot as its "final" figure,
    # then express every earlier snapshot as a fraction of it.
    finals: dict[str, float] = {}
    for video_id, _published, _captured, views in rows:
        finals[video_id] = max(finals.get(video_id, 0.0), float(views))

    buckets: dict[int, list[float]] = {}
    for video_id, published, captured, views in rows:
        final = finals.get(video_id, 0.0)
        if final <= 0 or published is None:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        age = (captured - published).total_seconds() / 86400.0
        if age < 0.5:
            continue
        for day, _ in DEFAULT_MATURITY_CURVE:
            if abs(age - day) <= max(1.0, day * 0.15):
                buckets.setdefault(day, []).append(min(1.0, float(views) / final))

    curve = [
        (day, float(np.median(values)))
        for day, values in sorted(buckets.items())
        if len(values) >= 5
    ]
    if len(curve) < 4:
        return None

    # Force monotonicity — views never go down, so a non-increasing curve is
    # sampling noise, not signal.
    cleaned: list[tuple[int, float]] = []
    highest = 0.0
    for day, frac in curve:
        highest = max(highest, frac)
        cleaned.append((day, highest))
    if cleaned[-1][1] < 1.0:
        cleaned.append((90, 1.0))
    return cleaned


def score_videos(
    videos: list[Video],
    baselines: dict[str, Baseline],
    channel_titles: dict[str, str],
    *,
    outlier_threshold: float,
    min_age_days: int,
    maturity_curve: list[tuple[int, float]] | None = None,
    now: datetime | None = None,
) -> list[VideoScore]:
    """Score each video against the baseline for its channel and format.

    ``baselines`` is keyed ``"{channel_id}:{long|short}"``. Videos with no
    matching baseline are skipped rather than scored against a substitute —
    a multiplier against the wrong denominator is worse than no multiplier.
    """
    now = now or datetime.now(timezone.utc)
    scores: list[VideoScore] = []

    # Percentile is computed within each channel+format group.
    groups: dict[str, list[float]] = {}
    for video in videos:
        key = f"{video.channel_id}:{'short' if video.is_short else 'long'}"
        if video.latest_view_count is not None:
            groups.setdefault(key, []).append(float(video.latest_view_count))

    for video in videos:
        if video.latest_view_count is None or video.published_at is None:
            continue
        key = f"{video.channel_id}:{'short' if video.is_short else 'long'}"
        baseline = baselines.get(key)
        if baseline is None or baseline.median_views <= 0:
            continue

        views = int(video.latest_view_count)
        age = _age_days(video.published_at, now)
        multiplier = views / baseline.median_views

        peers = groups.get(key) or [float(views)]
        percentile = float((np.array(peers) <= views).mean() * 100.0)

        caveats = list(baseline.caveats)
        projected: float | None = None
        if age < min_age_days:
            fraction = maturity_fraction(age, maturity_curve)
            projected = (views / fraction) / baseline.median_views
            caveats.append(
                f"Only {age:.1f} days old — typically about "
                f"{fraction * 100:.0f}% of eventual views. The projection assumes "
                f"this video follows a normal curve, which breakouts often don't."
            )

        scores.append(
            VideoScore(
                video_id=video.id,
                title=video.title,
                channel_id=video.channel_id,
                channel_title=channel_titles.get(video.channel_id, ""),
                published_at=video.published_at,
                age_days=age,
                views=views,
                is_short=video.is_short,
                thumbnail_url=video.thumbnail_url,
                baseline_views=baseline.median_views,
                multiplier=multiplier,
                percentile=percentile,
                projected_multiplier=projected,
                is_outlier=multiplier >= outlier_threshold,
                reliable=baseline.reliable and age >= min_age_days,
                caveats=caveats,
            )
        )

    scores.sort(key=lambda s: s.multiplier, reverse=True)
    return scores


def score_channel(
    session: Session,
    channel_id: str,
    *,
    window: int,
    min_age_days: int,
    min_videos: int,
    outlier_threshold: float,
    persist: bool = True,
    now: datetime | None = None,
) -> tuple[list[VideoScore], dict[str, Baseline]]:
    """Score every stored video for one channel, optionally writing the result.

    Reads only from the database — costs no quota. Rescoring after a fresh
    ingest is therefore free and can be done liberally.
    """
    now = now or datetime.now(timezone.utc)
    channel = session.get(Channel, channel_id)
    if channel is None:
        return [], {}

    videos = list(
        session.scalars(select(Video).where(Video.channel_id == channel_id)).all()
    )
    if not videos:
        return [], {}

    curve = empirical_maturity_curve(session, channel_id)
    baselines: dict[str, Baseline] = {}
    for is_short in (False, True):
        baseline = compute_baseline(
            videos, channel_id=channel_id, is_short=is_short,
            min_age_days=min_age_days, min_videos=min_videos, window=window, now=now,
        )
        if baseline:
            baselines[f"{channel_id}:{'short' if is_short else 'long'}"] = baseline

    scores = score_videos(
        videos, baselines, {channel_id: channel.title},
        outlier_threshold=outlier_threshold, min_age_days=min_age_days,
        maturity_curve=curve, now=now,
    )

    if persist:
        by_id = {s.video_id: s for s in scores}
        stamp = now.replace(tzinfo=None)
        for video in videos:
            score = by_id.get(video.id)
            if score is not None:
                video.outlier_multiplier = score.multiplier
                video.outlier_scored_at = stamp
        session.flush()

    return scores, baselines


def score_all_tracked(
    session: Session,
    *,
    window: int,
    min_age_days: int,
    min_videos: int,
    outlier_threshold: float,
    now: datetime | None = None,
) -> dict[str, list[VideoScore]]:
    """Rescore every tracked channel plus the user's own. Free — no API calls."""
    channel_ids = list(
        session.scalars(
            select(Channel.id).where(
                (Channel.is_tracked.is_(True)) | (Channel.is_owned.is_(True))
            )
        ).all()
    )
    results: dict[str, list[VideoScore]] = {}
    for channel_id in channel_ids:
        scores, _ = score_channel(
            session, channel_id, window=window, min_age_days=min_age_days,
            min_videos=min_videos, outlier_threshold=outlier_threshold, now=now,
        )
        results[channel_id] = scores
    return results
