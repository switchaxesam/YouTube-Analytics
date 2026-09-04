"""Database schema.

Two ideas shape this file.

**Everything fetched is kept.** API quota is the scarce resource (10,000 units
a day), so a row that has been paid for once is never thrown away. Statistics
are append-only snapshots rather than an updated column, which is what makes
velocity and "when did this take off" answerable at all.

**Public data and owned data are separate tables.** :class:`VideoStat` holds
what anyone can see (views, likes, comments). :class:`OwnedVideoDaily` holds
what only an authenticated channel owner can see (impressions, CTR, retention).
Keeping them apart stops the two from ever being conflated in a query, which
matters because the whole point of the app is knowing which numbers are real
measurements and which are inferences.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


class Channel(Base):
    """A YouTube channel. Primary key is YouTube's own channel id (``UC...``).

    A vendor id as primary key is normally a mistake, but YouTube channel ids
    are permanent and globally unique — unlike handles, which are renameable
    and therefore only ever cached here, never matched on.
    """

    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    handle: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    country: Mapped[str | None] = mapped_column(String(8))
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    uploads_playlist_id: Mapped[str | None] = mapped_column(String(64))

    subscriber_count: Mapped[int | None] = mapped_column(Integer)
    #: True when YouTube hides the exact subscriber count. The number above is
    #: then a rounded bucket, so anything derived from it is an estimate.
    subscriber_count_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    video_count: Mapped[int | None] = mapped_column(Integer)
    view_count: Mapped[int | None] = mapped_column(Integer)

    #: The user's own channel. Unlocks Analytics API data for its videos.
    is_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    #: On the watchlist — its uploads get polled by the tracker.
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Free-text grouping ("cooking", "my niche"), used by filters.
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    videos: Mapped[list["Video"]] = relationship(back_populates="channel")


class Video(Base):
    """A video's slow-changing metadata. Counts live in :class:`VideoStat`."""

    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )

    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    #: Derived from duration at ingest, using the configured Shorts cutoff.
    #: Stored rather than computed on read so that changing the setting later
    #: doesn't silently rewrite history in existing analyses.
    is_short: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)

    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[str | None] = mapped_column(String(16))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    default_language: Mapped[str | None] = mapped_column(String(16))

    #: Denormalised copies of the newest snapshot. Redundant with VideoStat,
    #: but list views sort and filter on these constantly and the join to find
    #: "latest per video" is the single most expensive query otherwise.
    latest_view_count: Mapped[int | None] = mapped_column(Integer, index=True)
    latest_like_count: Mapped[int | None] = mapped_column(Integer)
    latest_comment_count: Mapped[int | None] = mapped_column(Integer)

    #: Views ÷ the channel's baseline for videos of the same format. Written by
    #: services.outliers; null until scored.
    outlier_multiplier: Mapped[float | None] = mapped_column(Float, index=True)
    outlier_scored_at: Mapped[datetime | None] = mapped_column(DateTime)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    stats: Mapped[list["VideoStat"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_videos_channel_published", "channel_id", "published_at"),
    )


class VideoStat(Base):
    """One observation of a video's public counters at a point in time.

    Append-only. Two snapshots plus their timestamps give velocity; a long run
    of them gives the growth curve that says whether YouTube is still pushing
    a video or has dropped it.
    """

    __tablename__ = "video_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    view_count: Mapped[int] = mapped_column(Integer)
    like_count: Mapped[int | None] = mapped_column(Integer)
    comment_count: Mapped[int | None] = mapped_column(Integer)

    video: Mapped[Video] = relationship(back_populates="stats")

    __table_args__ = (
        Index("ix_video_stats_video_time", "video_id", "captured_at"),
    )


class VideoRevision(Base):
    """A detected change to a tracked video's title or thumbnail.

    This is the closest thing available to watching a competitor run an A/B
    test. Pairing the change timestamp against :class:`VideoStat` either side
    of it is how "the swap moved the numbers" gets evidenced rather than
    asserted.
    """

    __tablename__ = "video_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    #: "title" or "thumbnail".
    field: Mapped[str] = mapped_column(String(16), index=True)
    old_value: Mapped[str] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)

    #: Views at the moment the change was noticed, so the before/after
    #: comparison survives even if snapshots are later pruned.
    views_at_change: Mapped[int | None] = mapped_column(Integer)
    #: Views per day over the window before and after. Filled in by the tracker
    #: once enough post-change snapshots exist; null until then.
    velocity_before: Mapped[float | None] = mapped_column(Float)
    velocity_after: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


class ThumbnailAnalysis(Base):
    """Structured description of one thumbnail.

    Cheap deterministic features (colour, brightness, edge density) are computed
    locally with Pillow and always present. The vision fields are filled in by a
    model call and cost money, so they are nullable and cached indefinitely —
    a thumbnail image never changes for a given URL.
    """

    __tablename__ = "thumbnail_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    #: The exact image analysed. A thumbnail swap produces a new row rather
    #: than overwriting, so the analysis history matches the revision history.
    thumbnail_url: Mapped[str] = mapped_column(Text)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # -- Local, free -------------------------------------------------------
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    #: Up to five dominant colours as hex strings, most prominent first.
    dominant_colors: Mapped[list] = mapped_column(JSON, default=list)
    mean_brightness: Mapped[float | None] = mapped_column(Float)
    mean_saturation: Mapped[float | None] = mapped_column(Float)
    #: Standard deviation of luminance — a rough proxy for visual contrast,
    #: which is what actually makes a thumbnail survive being shown small.
    contrast: Mapped[float | None] = mapped_column(Float)
    #: Fraction of pixels sitting on a strong edge; high values mean a busy
    #: image that will turn to mush at sidebar size.
    edge_density: Mapped[float | None] = mapped_column(Float)

    # -- Vision model, paid ------------------------------------------------
    model: Mapped[str | None] = mapped_column(String(64))
    face_count: Mapped[int | None] = mapped_column(Integer)
    dominant_emotion: Mapped[str | None] = mapped_column(String(32))
    has_text: Mapped[bool | None] = mapped_column(Boolean)
    text_content: Mapped[str | None] = mapped_column(Text)
    text_word_count: Mapped[int | None] = mapped_column(Integer)
    #: Rough share of the frame covered by overlaid text, 0–1.
    text_area_fraction: Mapped[float | None] = mapped_column(Float)
    has_arrow_or_circle: Mapped[bool | None] = mapped_column(Boolean)
    #: Short noun phrase for the main subject ("person holding a camera").
    subject: Mapped[str | None] = mapped_column(Text)
    #: One of: closeup, medium, wide, screenshot, graphic, collage.
    composition: Mapped[str | None] = mapped_column(String(32))
    #: Model's own read of legibility at sidebar size, 0–100.
    clarity_score: Mapped[int | None] = mapped_column(Integer)
    #: Free-text observations, shown verbatim. Never summarised into a score
    #: on its own — the number without the reason is the thing users can't act on.
    notes: Mapped[str | None] = mapped_column(Text)
    raw_response: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("video_id", "thumbnail_url", name="uq_thumb_video_url"),
    )


class TitleAnalysis(Base):
    """Feature extraction over a video title.

    Deterministic features are computed with no API call and no model, so they
    exist for every video the app has ever seen. ``curiosity_score`` and
    ``angle`` come from a model and are nullable.
    """

    __tablename__ = "title_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    #: The exact title analysed, so a retitle produces a new row.
    title: Mapped[str] = mapped_column(Text)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    char_count: Mapped[int] = mapped_column(Integer)
    word_count: Mapped[int] = mapped_column(Integer)
    #: Titles are truncated in most surfaces around here; this flags the risk.
    truncation_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    has_number: Mapped[bool] = mapped_column(Boolean, default=False)
    has_brackets: Mapped[bool] = mapped_column(Boolean, default=False)
    has_question: Mapped[bool] = mapped_column(Boolean, default=False)
    has_colon: Mapped[bool] = mapped_column(Boolean, default=False)
    allcaps_word_count: Mapped[int] = mapped_column(Integer, default=0)
    emoji_count: Mapped[int] = mapped_column(Integer, default=0)
    power_words: Mapped[list] = mapped_column(JSON, default=list)
    #: Leading words, used to cluster "every top video opens with How I…".
    opening_ngram: Mapped[str | None] = mapped_column(String(64))

    model: Mapped[str | None] = mapped_column(String(64))
    #: 0–100, model's read of how strong the curiosity gap is.
    curiosity_score: Mapped[int | None] = mapped_column(Integer)
    #: Short label for the rhetorical angle: challenge, tutorial, listicle,
    #: story, reaction, comparison, warning, result.
    angle: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("video_id", "title", name="uq_title_video_text"),)


# --------------------------------------------------------------------------
# Owned-channel analytics (OAuth only)
# --------------------------------------------------------------------------


class OwnedVideoDaily(Base):
    """Per-day Analytics API metrics for a video on the user's own channel.

    These are measurements, not estimates — impressions and CTR in particular
    are unavailable for any channel but your own, at any price, from any tool.
    """

    __tablename__ = "owned_video_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)

    views: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int | None] = mapped_column(Integer)
    #: Click-through rate as a percentage, exactly as YouTube reports it.
    impressions_ctr: Mapped[float | None] = mapped_column(Float)
    average_view_duration_seconds: Mapped[float | None] = mapped_column(Float)
    average_view_percentage: Mapped[float | None] = mapped_column(Float)
    estimated_minutes_watched: Mapped[float | None] = mapped_column(Float)
    subscribers_gained: Mapped[int | None] = mapped_column(Integer)
    subscribers_lost: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (UniqueConstraint("video_id", "day", name="uq_owned_video_day"),)


class OwnedTrafficDaily(Base):
    """Views and CTR split by traffic source for an owned video.

    The split is what separates a title/thumbnail problem from a topic problem:
    weak CTR on ``SUGGESTED`` with healthy ``YT_SEARCH`` means the packaging
    loses against neighbouring videos, not that nobody wants the subject.
    """

    __tablename__ = "owned_traffic_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    #: YouTube's insightTrafficSourceType, e.g. YT_SEARCH, SUGGESTED, BROWSE.
    source_type: Mapped[str] = mapped_column(String(32), index=True)

    views: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int | None] = mapped_column(Integer)
    impressions_ctr: Mapped[float | None] = mapped_column(Float)
    estimated_minutes_watched: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("video_id", "day", "source_type", name="uq_owned_traffic"),
    )


# --------------------------------------------------------------------------
# Operational
# --------------------------------------------------------------------------


class WatchlistEntry(Base):
    """Something the tracker keeps an eye on: a channel, a video, or a query."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: "channel", "video", or "query".
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: Channel id, video id, or the literal search text.
    value: Mapped[str] = mapped_column(String(256))
    label: Mapped[str] = mapped_column(String(256), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_watchlist_kind_value"),)


class QuotaUsage(Base):
    """Data API units spent, one row per endpoint per Pacific day.

    Quota is the binding constraint on this whole app, so it is accounted for
    explicitly rather than discovered when a request starts returning 403. The
    day is stored as YouTube resets it — midnight US/Pacific, not local
    midnight — because those are different moments and only one of them refills
    the budget.
    """

    __tablename__ = "quota_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    calls: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("day", "endpoint", name="uq_quota_day_endpoint"),)


class ApiCache(Base):
    """Raw API responses, keyed by a hash of the request.

    A cache hit is a quota unit not spent, which is the entire reason this
    table exists. Entries carry their own TTL because a channel's subscriber
    count goes stale in hours while a video's publish date never does.
    """

    __tablename__ = "api_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class JobRun(Base):
    """One execution of a background or long-running job.

    Surfaced in the UI so that a slow import reads as progress rather than a
    frozen page, and so a failure leaves a durable, readable trace instead of
    only a toast that has already disappeared.
    """

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    #: "running", "ok", "error", or "cancelled".
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    detail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    units_spent: Mapped[int] = mapped_column(Integer, default=0)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
