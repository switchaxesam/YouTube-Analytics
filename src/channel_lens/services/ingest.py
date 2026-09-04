"""Turning API payloads into rows.

Every path that fetches from YouTube funnels through here, so there is exactly
one place that decides what a "video" row looks like, when a stats snapshot is
worth writing, and what counts as a change worth recording.

Ingestion is idempotent: re-running it on the same payload updates metadata in
place and adds at most one new snapshot. That matters because the tracker, a
manual refresh, and an outlier scan can all touch the same video within a
minute of each other, and none of them should corrupt the other's history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Channel, Video, VideoRevision, VideoStat
from ..youtube.client import YouTubeClient, best_thumbnail, parse_duration, parse_timestamp

log = logging.getLogger(__name__)

#: Don't write a new snapshot if the last one is younger than this. Two
#: snapshots minutes apart produce a velocity figure dominated by rounding in
#: YouTube's own (heavily cached, coarsely rounded) view counter rather than by
#: anything real.
MIN_SNAPSHOT_GAP = timedelta(minutes=30)


def _as_int(value: Any) -> int | None:
    """Statistics arrive as strings, and are absent entirely when hidden.

    >>> _as_int("1234")
    1234
    >>> _as_int(None) is None
    True
    >>> _as_int("not a number") is None
    True
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _naive_utc(value: datetime | None) -> datetime | None:
    """SQLite has no timezone type; store UTC and strip the tzinfo consistently."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None)


def upsert_channel(
    session: Session, payload: dict, *, is_owned: bool | None = None,
    is_tracked: bool | None = None,
) -> Channel:
    """Create or refresh a channel row from a ``channels.list`` item.

    ``is_owned``/``is_tracked`` are only written when explicitly passed, so a
    routine metadata refresh can never quietly un-track a channel.
    """
    channel_id = payload["id"]
    snippet = payload.get("snippet", {})
    stats = payload.get("statistics", {})
    content = payload.get("contentDetails", {})

    channel = session.get(Channel, channel_id)
    if channel is None:
        channel = Channel(id=channel_id)
        session.add(channel)

    channel.title = snippet.get("title", "") or channel.title
    channel.description = snippet.get("description", "") or ""
    channel.country = snippet.get("country")
    channel.handle = snippet.get("customUrl") or channel.handle
    channel.thumbnail_url = best_thumbnail(snippet.get("thumbnails")) or channel.thumbnail_url
    channel.uploads_playlist_id = (
        content.get("relatedPlaylists", {}).get("uploads") or channel.uploads_playlist_id
    )

    channel.subscriber_count = _as_int(stats.get("subscriberCount"))
    channel.subscriber_count_hidden = bool(stats.get("hiddenSubscriberCount", False))
    channel.video_count = _as_int(stats.get("videoCount"))
    channel.view_count = _as_int(stats.get("viewCount"))
    channel.fetched_at = _naive_utc(datetime.now(timezone.utc))

    if is_owned is not None:
        channel.is_owned = is_owned
    if is_tracked is not None:
        channel.is_tracked = is_tracked

    session.flush()
    return channel


def upsert_video(
    session: Session, payload: dict, *, shorts_max_seconds: int,
    record_revisions: bool = True,
) -> tuple[Video, list[VideoRevision]]:
    """Create or refresh a video row and append a stats snapshot.

    Returns the video and any revisions detected on this pass. A revision is
    only recorded for a video already in the database — the first time a video
    is seen, its current title is not a "change".
    """
    video_id = payload["id"]
    snippet = payload.get("snippet", {})
    stats = payload.get("statistics", {})
    content = payload.get("contentDetails", {})

    video = session.get(Video, video_id)
    is_new = video is None
    if is_new:
        video = Video(id=video_id, channel_id=snippet.get("channelId", ""))
        session.add(video)

    new_title = snippet.get("title", "") or ""
    new_thumbnail = best_thumbnail(snippet.get("thumbnails"))
    view_count = _as_int(stats.get("viewCount"))

    revisions: list[VideoRevision] = []
    if not is_new and record_revisions:
        revisions = _detect_revisions(
            session, video, new_title=new_title, new_thumbnail=new_thumbnail,
            view_count=view_count,
        )

    video.channel_id = snippet.get("channelId", "") or video.channel_id
    video.title = new_title or video.title
    video.description = snippet.get("description", "") or ""
    video.published_at = _naive_utc(parse_timestamp(snippet.get("publishedAt"))) or video.published_at
    video.thumbnail_url = new_thumbnail or video.thumbnail_url
    video.category_id = snippet.get("categoryId") or video.category_id
    video.tags = snippet.get("tags", []) or []
    video.default_language = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage")

    duration = parse_duration(content.get("duration"))
    if duration is not None:
        video.duration_seconds = duration
        # A live broadcast reports P0D until it ends; calling that a Short
        # would drop real long-form content into the wrong baseline.
        video.is_live = duration == 0 and snippet.get("liveBroadcastContent") not in (None, "none")
        video.is_short = 0 < duration <= shorts_max_seconds

    video.latest_view_count = view_count if view_count is not None else video.latest_view_count
    video.latest_like_count = _as_int(stats.get("likeCount")) or video.latest_like_count
    video.latest_comment_count = _as_int(stats.get("commentCount")) or video.latest_comment_count
    video.fetched_at = _naive_utc(datetime.now(timezone.utc))

    session.flush()

    if view_count is not None:
        _maybe_snapshot(session, video, stats)

    return video, revisions


def _detect_revisions(
    session: Session, video: Video, *, new_title: str, new_thumbnail: str | None,
    view_count: int | None,
) -> list[VideoRevision]:
    """Record title and thumbnail changes against the stored values.

    Thumbnail comparison is on URL, which works because YouTube mints a new
    URL when the image is replaced. It does mean a URL that changes for an
    unrelated reason (a CDN migration, a resolution tier appearing later)
    registers as a false positive — acceptable, since a false "they changed
    the thumbnail" is far cheaper than missing a real test.
    """
    revisions: list[VideoRevision] = []
    now = _naive_utc(datetime.now(timezone.utc))

    if new_title and video.title and new_title != video.title:
        revision = VideoRevision(
            video_id=video.id, detected_at=now, field="title",
            old_value=video.title, new_value=new_title, views_at_change=view_count,
        )
        session.add(revision)
        revisions.append(revision)

    if new_thumbnail and video.thumbnail_url and new_thumbnail != video.thumbnail_url:
        revision = VideoRevision(
            video_id=video.id, detected_at=now, field="thumbnail",
            old_value=video.thumbnail_url, new_value=new_thumbnail,
            views_at_change=view_count,
        )
        session.add(revision)
        revisions.append(revision)

    return revisions


def _maybe_snapshot(session: Session, video: Video, stats: dict) -> VideoStat | None:
    """Append a stats snapshot unless one was taken very recently."""
    now = datetime.now(timezone.utc)
    latest = session.scalar(
        select(VideoStat)
        .where(VideoStat.video_id == video.id)
        .order_by(VideoStat.captured_at.desc())
        .limit(1)
    )
    if latest is not None:
        captured = latest.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        if now - captured < MIN_SNAPSHOT_GAP:
            return None

    snapshot = VideoStat(
        video_id=video.id,
        captured_at=_naive_utc(now),
        view_count=_as_int(stats.get("viewCount")) or 0,
        like_count=_as_int(stats.get("likeCount")),
        comment_count=_as_int(stats.get("commentCount")),
    )
    session.add(snapshot)
    return snapshot


def ingest_videos(
    client: YouTubeClient, session: Session, video_ids: list[str], *,
    shorts_max_seconds: int,
) -> tuple[list[Video], list[VideoRevision]]:
    """Fetch and store a batch of videos. 1 unit per 50."""
    if not video_ids:
        return [], []
    payloads = client.get_videos(video_ids)
    videos: list[Video] = []
    revisions: list[VideoRevision] = []
    for payload in payloads:
        video, revs = upsert_video(session, payload, shorts_max_seconds=shorts_max_seconds)
        videos.append(video)
        revisions.extend(revs)
    return videos, revisions


def ingest_channel_uploads(
    client: YouTubeClient,
    session: Session,
    channel: Channel,
    *,
    limit: int,
    shorts_max_seconds: int,
    published_after: datetime | None = None,
) -> tuple[list[Video], list[VideoRevision]]:
    """Fetch a channel's recent uploads and store them.

    Costs 1 unit per 50 uploads listed plus 1 unit per 50 fetched — so a
    100-video pull is 4 units, versus the 200 that the same thing would cost
    through ``search.list``.
    """
    if not channel.uploads_playlist_id:
        payloads = client.get_channels([channel.id])
        if payloads:
            upsert_channel(session, payloads[0])
    if not channel.uploads_playlist_id:
        return [], []

    video_ids = client.list_upload_video_ids(
        channel.uploads_playlist_id, limit=limit, published_after=published_after
    )
    return ingest_videos(
        client, session, video_ids, shorts_max_seconds=shorts_max_seconds
    )


def estimate_channel_ingest_cost(video_count: int) -> int:
    """Units to pull ``video_count`` uploads for one channel.

    Listing and fetching are both 1 unit per 50, hence two calls' worth.

    >>> estimate_channel_ingest_cost(50)
    2
    >>> estimate_channel_ingest_cost(200)
    8
    >>> estimate_channel_ingest_cost(0)
    0
    """
    if video_count <= 0:
        return 0
    pages = (video_count + 49) // 50
    return pages * 2
