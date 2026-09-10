"""YouTube Data API v3 client.

Synchronous on purpose. This is a single-user local app; FastAPI runs sync
endpoints in a threadpool and the background tracker gets its own thread, so
async would add colour to every function in the call graph and buy nothing.

Three behaviours distinguish this from a thin ``httpx`` wrapper:

* **Quota is checked before the request, not after.** Every public method
  declares what it will cost and asks the ledger first.
* **Responses are cached in SQLite with per-endpoint TTLs.** A cache hit costs
  zero units, and this is the main reason a day's work fits inside 10,000.
* **Errors come back typed and phrased for a human.** Google's error bodies
  distinguish ``quotaExceeded`` from ``keyInvalid`` from ``videoNotFound``; all
  three arrive as HTTP 403, so the body has to be read to say anything useful.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import ApiCache
from .errors import (
    InvalidCredentials,
    NotConfigured,
    NotFound,
    TransientUpstreamError,
    UpstreamQuotaExceeded,
    YouTubeError,
)
from .quota import MAX_BATCH, QuotaLedger, calls_for_items, cost_of

log = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"

#: How long a cached response stays usable, per endpoint. Tuned by how fast the
#: underlying thing actually changes: view counts drift continuously, a
#: channel's upload list changes when it uploads, and search results reshuffle
#: slowly. Search gets the longest TTL despite changing fastest, because it is
#: 100x the price of everything else.
CACHE_TTL = {
    "videos.list": timedelta(hours=3),
    "channels.list": timedelta(hours=12),
    "playlistItems.list": timedelta(hours=3),
    "search.list": timedelta(hours=12),
    "videoCategories.list": timedelta(days=30),
}
DEFAULT_TTL = timedelta(hours=6)

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def parse_duration(value: str | None) -> int | None:
    """Convert an ISO 8601 duration to whole seconds.

    YouTube reports durations as ``PT4M13S``. Live streams with no set length
    come back as ``P0D``, which is a real value meaning "not applicable", not a
    parse failure.

    >>> parse_duration("PT4M13S")
    253
    >>> parse_duration("PT1H2M3S")
    3723
    >>> parse_duration("PT45S")
    45
    >>> parse_duration("P0D")
    0
    >>> parse_duration("garbage") is None
    True
    >>> parse_duration(None) is None
    True
    """
    if not value:
        return None
    match = _ISO_DURATION.match(value)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an RFC 3339 timestamp into an aware UTC datetime.

    >>> parse_timestamp("2026-03-02T14:05:00Z")
    datetime.datetime(2026, 3, 2, 14, 5, tzinfo=datetime.timezone.utc)
    >>> parse_timestamp(None) is None
    True
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def chunked(items: Sequence[str], size: int = MAX_BATCH) -> Iterator[list[str]]:
    """Split ``items`` into lists of at most ``size``.

    The API caps id lists at 50 per request, and since cost is per request this
    is exactly where the savings are.

    >>> list(chunked(["a", "b", "c"], 2))
    [['a', 'b'], ['c']]
    >>> list(chunked([], 2))
    []
    """
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def best_thumbnail(thumbnails: dict[str, Any] | None) -> str | None:
    """Pick the highest-resolution thumbnail available.

    YouTube omits larger sizes for older or low-resolution uploads, so the
    preferred order has to degrade rather than assume ``maxres`` exists.

    >>> best_thumbnail({"default": {"url": "d"}, "maxres": {"url": "m"}})
    'm'
    >>> best_thumbnail({"default": {"url": "d"}, "high": {"url": "h"}})
    'h'
    >>> best_thumbnail(None) is None
    True
    """
    if not thumbnails:
        return None
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if entry and entry.get("url"):
            return entry["url"]
    return None


def normalise_channel_input(raw: str) -> tuple[str, str]:
    """Classify a user-typed channel reference.

    Returns ``(kind, value)`` where kind is ``id``, ``handle``, or ``username``.
    Accepts bare ids, @handles, and any of the URL shapes YouTube has used over
    the years, because users paste whatever is in the address bar.

    >>> normalise_channel_input("UCabcdefghijklmnopqrstuv")
    ('id', 'UCabcdefghijklmnopqrstuv')
    >>> normalise_channel_input("@mkbhd")
    ('handle', '@mkbhd')
    >>> normalise_channel_input("https://www.youtube.com/@mkbhd/videos")
    ('handle', '@mkbhd')
    >>> normalise_channel_input("youtube.com/channel/UCabcdefghijklmnopqrstuv")
    ('id', 'UCabcdefghijklmnopqrstuv')
    >>> normalise_channel_input("https://youtube.com/c/SomeName")
    ('username', 'SomeName')
    >>> normalise_channel_input("  mkbhd  ")
    ('handle', '@mkbhd')
    """
    text = raw.strip()
    if not text:
        raise ValueError("Empty channel reference.")

    match = re.search(r"/channel/(UC[\w-]{20,})", text)
    if match:
        return "id", match.group(1)
    match = re.search(r"/@([\w.\-]+)", text)
    if match:
        return "handle", f"@{match.group(1)}"
    match = re.search(r"/(?:c|user)/([\w.\-]+)", text)
    if match:
        return "username", match.group(1)

    if text.startswith("@"):
        return "handle", text
    if re.fullmatch(r"UC[\w-]{20,}", text):
        return "id", text
    return "handle", f"@{text}"


def parse_channel_references(text: str) -> list[str]:
    r"""Split pasted text into channel references, preserving order.

    People paste lists from wherever they collected them, so this accepts one
    per line, comma or semicolon separated, bulleted, numbered, or a mix, and
    tolerates the tab-separated leftovers of a spreadsheet copy. Order is kept
    because the parsed result is shown back for confirmation, and a list that
    silently reorders itself looks like it lost something.

    >>> parse_channel_references("@mkbhd\n@mkbhd\n@ltt")
    ['@mkbhd', '@ltt']
    >>> parse_channel_references("@a, @b; @c")
    ['@a', '@b', '@c']
    >>> parse_channel_references("- @a\n2. @b\n  * @c  ")
    ['@a', '@b', '@c']
    >>> parse_channel_references("")
    []

    Duplicates are dropped case-insensitively, since handles are not
    case-sensitive and paying twice to import one channel is pure waste:

    >>> parse_channel_references("@MKBHD\n@mkbhd")
    ['@MKBHD']

    A pasted URL keeps its whole path -- the separator split must never cut one
    in half:

    >>> parse_channel_references("https://youtube.com/@a/videos\nhttps://youtube.com/@b")
    ['https://youtube.com/@a/videos', 'https://youtube.com/@b']

    Spreadsheet columns arrive tab separated, so extra cells surface as their
    own entries rather than being glued onto the handle. They fail to resolve
    individually and are reported per row, which is easier to understand than
    one silently corrupted reference:

    >>> parse_channel_references("@a\tsome note")
    ['@a', 'some note']
    """
    references: list[str] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip list decoration: bullets, dashes, "1." / "1)" numbering.
        line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", line)

        # Split only on characters that cannot appear inside a YouTube
        # reference. A URL contains neither a comma nor a semicolon.
        for part in re.split(r"[,;\t]+", line):
            candidate = part.strip().strip("\"'")
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            references.append(candidate)

    return references


def extract_video_id(raw: str) -> str | None:
    """Pull a video id out of any YouTube URL shape, or accept a bare id.

    >>> extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    'dQw4w9WgXcQ'
    >>> extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42")
    'dQw4w9WgXcQ'
    >>> extract_video_id("https://www.youtube.com/shorts/abc12345678")
    'abc12345678'
    >>> extract_video_id("dQw4w9WgXcQ")
    'dQw4w9WgXcQ'
    >>> extract_video_id("not a video") is None
    True
    """
    text = raw.strip()
    for pattern in (
        r"[?&]v=([\w-]{11})",
        r"youtu\.be/([\w-]{11})",
        r"/shorts/([\w-]{11})",
        r"/embed/([\w-]{11})",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if re.fullmatch(r"[\w-]{11}", text):
        return text
    return None


class YouTubeClient:
    """Quota-aware, caching wrapper over the Data API.

    A client is constructed per operation and handed the SQLAlchemy session that
    the operation runs in, so cache writes and quota bookings land in the same
    transaction as the data they describe.
    """

    def __init__(
        self,
        api_key: str,
        ledger: QuotaLedger,
        session: Session,
        *,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise NotConfigured()
        self.api_key = api_key
        self.ledger = ledger
        self.session = session
        self.max_retries = max_retries
        self._http = http or httpx.Client(timeout=20.0)
        self._owns_http = http is None
        #: Units actually spent by this client, for per-job reporting.
        self.units_spent = 0
        self.cache_hits = 0

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "YouTubeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Cache -------------------------------------------------------------

    def _cache_key(self, endpoint: str, params: dict[str, Any]) -> str:
        # The API key is deliberately excluded: the same request under a
        # different key returns the same public data, and including it would
        # throw the whole cache away on a key rotation.
        payload = json.dumps(
            {"endpoint": endpoint, "params": {k: v for k, v in sorted(params.items())}},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        row = self.session.get(ApiCache, key)
        if row is None:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        return row.payload

    def _cache_put(self, key: str, endpoint: str, payload: dict) -> None:
        ttl = CACHE_TTL.get(endpoint, DEFAULT_TTL)
        now = datetime.now(timezone.utc)
        row = self.session.get(ApiCache, key)
        if row is None:
            row = ApiCache(key=key, endpoint=endpoint)
            self.session.add(row)
        row.payload = payload
        row.fetched_at = now
        row.expires_at = now + ttl
        self.session.flush()

    def purge_expired_cache(self) -> int:
        result = self.session.execute(
            delete(ApiCache).where(ApiCache.expires_at < datetime.now(timezone.utc))
        )
        return result.rowcount or 0

    # -- Transport ---------------------------------------------------------

    def _request(
        self, endpoint: str, params: dict[str, Any], *, use_cache: bool = True
    ) -> dict:
        """One API request, with cache, quota booking, retries, and error mapping."""
        path = endpoint.split(".")[0]
        key = self._cache_key(endpoint, params)

        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached

        self.ledger.check(self.session, cost_of(endpoint), what=f"A {endpoint} request")

        query = dict(params)
        query["key"] = self.api_key

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._http.get(f"{API_ROOT}/{path}", params=query)
            except httpx.HTTPError as exc:
                last_error = exc
                # Network-level failure costs no quota, so retrying is free.
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise TransientUpstreamError(
                    "Could not reach the YouTube API.",
                    "Check your internet connection, then try again.",
                ) from exc

            if response.status_code == 200:
                # Book the unit only on a response that actually consumed one.
                self.units_spent += self.ledger.record(self.session, endpoint)
                payload = response.json()
                if use_cache:
                    self._cache_put(key, endpoint, payload)
                return payload

            if response.status_code >= 500:
                last_error = TransientUpstreamError(
                    f"YouTube returned {response.status_code}.", "This is usually temporary."
                )
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise last_error

            raise self._translate_error(response, endpoint)

        raise TransientUpstreamError(
            "The YouTube API did not respond successfully.",
            "This is usually temporary — try again shortly.",
        ) from last_error

    def _translate_error(self, response: httpx.Response, endpoint: str) -> YouTubeError:
        """Turn Google's error body into a specific, actionable exception.

        403 alone is ambiguous — it covers an exhausted quota, a disabled API,
        and a key restricted to the wrong referrer, which need three different
        fixes. The ``reason`` field is the only thing that separates them.
        """
        try:
            body = response.json()
            error = body.get("error", {})
            reason = (error.get("errors") or [{}])[0].get("reason", "")
            message = error.get("message", response.text[:400])
        except (ValueError, AttributeError, IndexError):
            reason, message = "", response.text[:400]

        if reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            # Google disagrees with our ledger. Google is right.
            self.ledger.mark_exhausted(self.session)
            return UpstreamQuotaExceeded(
                "YouTube reports the daily API quota is already used up.",
                "The quota resets at midnight US/Pacific. If you use this API key "
                "elsewhere, its spend counts against the same 10,000 units.",
            )
        if reason in {"keyInvalid", "badRequest"} and "API key" in message:
            return InvalidCredentials(
                "The YouTube API key was rejected.",
                "Check for stray whitespace, and confirm the key belongs to a project "
                "with the YouTube Data API v3 enabled.",
            )
        if reason in {"accessNotConfigured", "forbidden", "servicedisabled"}:
            return InvalidCredentials(
                "The YouTube Data API v3 is not enabled for this key's project.",
                "In Google Cloud, open APIs & Services → Library, find 'YouTube Data "
                "API v3', and press Enable. It can take a minute to take effect.",
            )
        if response.status_code == 404 or reason in {
            "videoNotFound",
            "channelNotFound",
            "playlistNotFound",
        }:
            return NotFound(
                "YouTube has no such channel, video, or playlist.",
                "It may have been deleted or made private. Check the id or URL.",
            )
        if response.status_code == 400:
            return YouTubeError(
                f"YouTube rejected the request: {message}",
                "This is a bug in the app if you didn't hand-edit anything.",
            )
        return YouTubeError(
            f"YouTube returned {response.status_code}: {message}",
            "Try again; if it persists, check the Google Cloud console for this project.",
        )

    # -- Cost estimation ---------------------------------------------------

    def estimate_videos_cost(self, video_count: int) -> int:
        """Units to fetch metadata for ``video_count`` videos.

        >>> from unittest.mock import Mock
        >>> c = YouTubeClient.__new__(YouTubeClient)
        >>> c.estimate_videos_cost(120)
        3
        """
        return cost_of("videos.list", calls_for_items(video_count))

    def estimate_uploads_cost(self, video_count: int) -> int:
        """Units to page through ``video_count`` uploads via the uploads playlist.

        >>> c = YouTubeClient.__new__(YouTubeClient)
        >>> c.estimate_uploads_cost(200)
        4
        """
        return cost_of("playlistItems.list", calls_for_items(video_count))

    # -- Endpoints ---------------------------------------------------------

    def get_channels(self, channel_ids: Sequence[str]) -> list[dict]:
        """Full ``snippet``/``statistics``/``contentDetails`` for each channel."""
        results: list[dict] = []
        for batch in chunked(list(dict.fromkeys(channel_ids))):
            payload = self._request(
                "channels.list",
                {
                    "part": "snippet,statistics,contentDetails,brandingSettings",
                    "id": ",".join(batch),
                    "maxResults": MAX_BATCH,
                },
            )
            results.extend(payload.get("items", []))
        return results

    def resolve_channel(self, reference: str) -> dict:
        """Find a channel from an id, @handle, legacy username, or any URL.

        Costs 1 unit in every case that succeeds without search. Falls back to
        ``search.list`` (100 units) only when the cheap lookups all miss, and
        says so in the raised error rather than silently spending it.
        """
        kind, value = normalise_channel_input(reference)

        if kind == "id":
            items = self.get_channels([value])
            if items:
                return items[0]
            raise NotFound(
                f"No channel with id {value}.",
                "Double-check the id — it should start with 'UC' and be 24 characters.",
            )

        part = "snippet,statistics,contentDetails,brandingSettings"
        if kind == "handle":
            payload = self._request(
                "channels.list", {"part": part, "forHandle": value, "maxResults": 1}
            )
            items = payload.get("items", [])
            if items:
                return items[0]

        legacy = value.lstrip("@")
        payload = self._request(
            "channels.list", {"part": part, "forUsername": legacy, "maxResults": 1}
        )
        items = payload.get("items", [])
        if items:
            return items[0]

        raise NotFound(
            f"Could not resolve “{reference}” to a channel.",
            "Open the channel on YouTube, copy the URL from the address bar, and paste "
            "that. A /channel/UC… URL always works; vanity URLs sometimes don't resolve "
            "through the API.",
        )

    def get_videos(self, video_ids: Sequence[str]) -> list[dict]:
        """Metadata and public statistics for up to any number of videos.

        Batched 50 per request, so 500 videos cost 10 units — the single
        cheapest bulk operation the API offers, and the backbone of this app.
        """
        results: list[dict] = []
        unique = list(dict.fromkeys(video_ids))
        for batch in chunked(unique):
            payload = self._request(
                "videos.list",
                {
                    "part": "snippet,statistics,contentDetails,status",
                    "id": ",".join(batch),
                    "maxResults": MAX_BATCH,
                },
            )
            results.extend(payload.get("items", []))
        return results

    def list_upload_video_ids(
        self,
        uploads_playlist_id: str,
        *,
        limit: int | None = 50,
        published_after: datetime | None = None,
    ) -> list[str]:
        """Recent uploads for a channel, newest first, at 1 unit per 50.

        This exists so the app never reaches for ``search.list`` to answer
        "what has this channel posted lately". The uploads playlist is ordered
        newest-first, so ``published_after`` can stop paging early instead of
        filtering after the fact.

        ``limit=None`` pages the entire upload history. That is what trailing
        baselines and breakout detection need — both walk a channel's timeline
        and cannot do so from a truncated recent slice. It stays cheap: the
        playlist costs 1 unit per 50 ids regardless of how far back it goes, so
        a thousand-video channel is 20 units to enumerate.
        """
        ids: list[str] = []
        page_token: str | None = None
        unbounded = limit is None

        while unbounded or len(ids) < limit:
            params: dict[str, Any] = {
                "part": "contentDetails",
                "playlistId": uploads_playlist_id,
                "maxResults": MAX_BATCH if unbounded else min(MAX_BATCH, limit - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._request("playlistItems.list", params)
            items = payload.get("items", [])
            if not items:
                break

            stop = False
            for item in items:
                details = item.get("contentDetails", {})
                video_id = details.get("videoId")
                if not video_id:
                    continue
                if published_after:
                    published = parse_timestamp(details.get("videoPublishedAt"))
                    if published and published < published_after:
                        # Newest-first ordering means everything after this is
                        # older too, so paging further would only waste units.
                        stop = True
                        break
                ids.append(video_id)

            if stop:
                break
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return ids

    def search(
        self,
        query: str,
        *,
        limit: int = 25,
        published_after: datetime | None = None,
        order: str = "relevance",
        video_duration: str | None = None,
        region_code: str | None = None,
        relevance_language: str | None = None,
    ) -> list[str]:
        """Search for videos, returning ids only.

        **100 units per 50 results.** Every call here is worth roughly a hundred
        ``videos.list`` calls, which is why search is only ever used for
        discovery — finding out *which* videos exist — and the actual data is
        then pulled with the cheap endpoint.
        """
        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < limit:
            params: dict[str, Any] = {
                "part": "id",
                "q": query,
                "type": "video",
                "order": order,
                "maxResults": min(MAX_BATCH, limit - len(ids)),
            }
            if published_after:
                params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
            if video_duration:
                params["videoDuration"] = video_duration
            if region_code:
                params["regionCode"] = region_code
            if relevance_language:
                params["relevanceLanguage"] = relevance_language
            if page_token:
                params["pageToken"] = page_token

            payload = self._request("search.list", params)
            items = payload.get("items", [])
            if not items:
                break
            for item in items:
                video_id = (item.get("id") or {}).get("videoId")
                if video_id:
                    ids.append(video_id)
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return ids

    def search_channels(self, query: str, *, limit: int = 10) -> list[str]:
        """Find channel ids by name. 100 units — used only for the add-channel
        search box, never in a loop."""
        payload = self._request(
            "search.list",
            {"part": "id", "q": query, "type": "channel", "maxResults": min(MAX_BATCH, limit)},
        )
        return [
            (item.get("id") or {}).get("channelId")
            for item in payload.get("items", [])
            if (item.get("id") or {}).get("channelId")
        ]
