"""Filtering out content that isn't yours to compete with.

A niche tool is only useful if its idea of "your niche" matches yours. Search
and even a carefully chosen channel list drag in neighbouring content — a games
trailer, a music video, a four-hour livestream VOD — and every one of those
dilutes the patterns the app is trying to show you.

Two principles shape this.

**Structure beats keywords.** A keyword list is the obvious tool and the
weakest one: it only catches what you thought to type, and it misfires on words
that appear innocently. YouTube already labels a video's category, language and
duration, and a channel's size. Those hold for content you have never seen and
could not have anticipated, so they do the heavy lifting; keywords handle the
specifics only you know.

**An exclusion is never silent.** Every check returns *why* something was
dropped, and the caller is expected to surface the count. A filter that quietly
removes results is indistinguishable from a bug — the user searches for
something they know exists, doesn't see it, and has no way to find out why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: YouTube's video categories, by the id stored on each video. Only the ones a
#: creator would plausibly want to exclude wholesale are listed; the rest are
#: still excludable by id, just without a friendly name.
CATEGORY_NAMES: dict[str, str] = {
    "1": "Film & Animation",
    "2": "Autos & Vehicles",
    "10": "Music",
    "15": "Pets & Animals",
    "17": "Sports",
    "19": "Travel & Events",
    "20": "Gaming",
    "22": "People & Blogs",
    "23": "Comedy",
    "24": "Entertainment",
    "25": "News & Politics",
    "26": "Howto & Style",
    "27": "Education",
    "28": "Science & Technology",
    "29": "Nonprofits & Activism",
    "30": "Movies",
    "31": "Anime / Animation",
    "44": "Trailers",
}


def category_name(category_id: str | None) -> str:
    """Human name for a category id, falling back to the id itself.

    >>> category_name("20")
    'Gaming'
    >>> category_name("999")
    'Category 999'
    >>> category_name(None)
    'Uncategorised'
    """
    if not category_id:
        return "Uncategorised"
    return CATEGORY_NAMES.get(category_id, f"Category {category_id}")


def compile_keywords(keywords: Iterable[str]) -> list[tuple[str, "re.Pattern[str]"]]:
    """Build word-boundary matchers for each keyword.

    Whole-word matching by default, because substring matching is a trap:
    excluding "war" would also drop "warranty", "warm" and "workshop warm-up".
    A trailing ``*`` opts into prefix matching for the cases where you do want
    it, so "minecraft*" catches "minecrafter".

    Multi-word phrases work as written, with flexible whitespace.

    >>> [k for k, _ in compile_keywords(["Gaming", " ", "let's play"])]
    ['gaming', "let's play"]
    >>> _, pattern = compile_keywords(["war"])[0]
    >>> bool(pattern.search("the war years")), bool(pattern.search("warranty claim"))
    (True, False)
    >>> _, prefix = compile_keywords(["minecraft*"])[0]
    >>> bool(prefix.search("minecrafter builds a base"))
    True
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for raw in keywords:
        term = (raw or "").strip().lower()
        if not term:
            continue
        prefix = term.endswith("*")
        core = term[:-1].strip() if prefix else term
        if not core:
            continue
        # Flexible whitespace so "let's  play" still matches "let's play".
        body = r"\s+".join(re.escape(part) for part in core.split())
        pattern = rf"\b{body}" if prefix else rf"\b{body}\b"
        compiled.append((term, re.compile(pattern, re.IGNORECASE)))
    return compiled


@dataclass
class Exclusions:
    """The user's standing definition of "not my niche".

    Every field is optional and an empty value means "no opinion", so a default
    instance excludes nothing at all.
    """

    keywords: list[str] = field(default_factory=list)
    category_ids: list[str] = field(default_factory=list)
    #: Two-letter language codes to drop, matched on the video's declared
    #: audio/default language. Empty means keep every language.
    languages: list[str] = field(default_factory=list)
    #: The opposite, and the stronger tool when you only make one language:
    #: keep *only* these. Applied after `languages`.
    only_languages: list[str] = field(default_factory=list)
    channel_ids: list[str] = field(default_factory=list)
    min_duration_seconds: int = 0
    max_duration_seconds: int = 0
    min_channel_subscribers: int = 0
    max_channel_subscribers: int = 0
    exclude_live: bool = True

    def __post_init__(self) -> None:
        self._patterns = compile_keywords(self.keywords)
        self._categories = {c for c in self.category_ids if c}
        self._languages = {l.lower() for l in self.languages if l}
        self._only_languages = {l.lower() for l in self.only_languages if l}
        self._channels = {c for c in self.channel_ids if c}

    @property
    def active(self) -> bool:
        """Whether anything is actually being excluded.

        ``exclude_live`` counts, even though it is on by default. Leaving it
        out made the rule apply only when *some other* exclusion happened to be
        set, so live VODs were dropped or kept depending on an unrelated
        setting — the kind of inconsistency that is impossible to reason about
        from the outside.

        >>> Exclusions(exclude_live=False).active
        False
        >>> Exclusions().active            # live exclusion is on by default
        True
        >>> Exclusions(exclude_live=False, keywords=["gaming"]).active
        True
        """
        return bool(
            self._patterns or self._categories or self._languages
            or self._only_languages or self._channels
            or self.min_duration_seconds or self.max_duration_seconds
            or self.min_channel_subscribers or self.max_channel_subscribers
            or self.exclude_live
        )

    def reason_to_exclude(self, video: Any, channel: Any = None) -> str | None:
        """Why this video is out of scope, or ``None`` to keep it.

        Ordered so the most informative reason wins: a games video excluded by
        category is more usefully reported as "Gaming" than as "matched the
        word 'gameplay'".
        """
        if self._channels and getattr(video, "channel_id", None) in self._channels:
            return "channel is on your exclusion list"

        category = getattr(video, "category_id", None)
        if category and category in self._categories:
            return f"category is {category_name(category)}"

        language = (getattr(video, "default_language", None) or "").lower()
        # YouTube reports regional variants like "en-GB"; compare on the base.
        base_language = language.split("-")[0]
        if base_language:
            if base_language in self._languages:
                return f"language is {base_language}"
            if self._only_languages and base_language not in self._only_languages:
                return f"language is {base_language}, not one you cover"

        if self.exclude_live and getattr(video, "is_live", False):
            return "live broadcast"

        duration = getattr(video, "duration_seconds", None)
        if duration:
            if self.min_duration_seconds and duration < self.min_duration_seconds:
                return f"shorter than {self.min_duration_seconds // 60} min"
            if self.max_duration_seconds and duration > self.max_duration_seconds:
                return f"longer than {self.max_duration_seconds // 60} min"

        if channel is not None:
            subscribers = getattr(channel, "subscriber_count", None)
            if subscribers:
                if self.min_channel_subscribers and subscribers < self.min_channel_subscribers:
                    return "channel is smaller than your range"
                if self.max_channel_subscribers and subscribers > self.max_channel_subscribers:
                    return "channel is bigger than your range"

        haystack = " ".join(filter(None, [
            getattr(video, "title", "") or "",
            " ".join(getattr(video, "tags", None) or []),
        ]))
        for term, pattern in self._patterns:
            if pattern.search(haystack):
                return f"matched “{term}”"

        return None

    def excludes(self, video: Any, channel: Any = None) -> bool:
        return self.reason_to_exclude(video, channel) is not None


def from_settings(settings: Any) -> Exclusions:
    """Build the exclusion set from stored settings."""
    return Exclusions(
        keywords=list(settings.excluded_keywords or []),
        category_ids=[str(c) for c in (settings.excluded_category_ids or [])],
        languages=list(settings.excluded_languages or []),
        only_languages=list(settings.only_languages or []),
        channel_ids=list(settings.excluded_channel_ids or []),
        min_duration_seconds=settings.min_duration_seconds or 0,
        max_duration_seconds=settings.max_duration_seconds or 0,
        min_channel_subscribers=settings.min_channel_subscribers or 0,
        max_channel_subscribers=settings.max_channel_subscribers or 0,
        exclude_live=bool(settings.exclude_live),
    )


def partition(
    videos: Iterable[Any], exclusions: Exclusions, channels: dict[str, Any] | None = None
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Split videos into kept and excluded.

    The excluded half is returned in full, with reasons, rather than discarded:
    the caller has to be able to show what was removed and why.

    >>> class V:
    ...     def __init__(self, vid, title, category=None):
    ...         self.id, self.title, self.category_id = vid, title, category
    ...         self.tags, self.channel_id, self.is_live = [], "UC1", False
    ...         self.duration_seconds, self.default_language = 600, "en"
    >>> videos = [V("a", "Building a workbench"), V("b", "Minecraft base tour")]
    >>> kept, dropped = partition(videos, Exclusions(keywords=["minecraft"]))
    >>> [v.id for v in kept]
    ['a']
    >>> dropped[0]["video_id"], dropped[0]["reason"]
    ('b', 'matched “minecraft”')
    """
    channels = channels or {}
    kept: list[Any] = []
    dropped: list[dict[str, Any]] = []

    for video in videos:
        channel = channels.get(getattr(video, "channel_id", None))
        reason = exclusions.reason_to_exclude(video, channel)
        if reason is None:
            kept.append(video)
        else:
            dropped.append({
                "video_id": getattr(video, "id", None),
                "title": getattr(video, "title", ""),
                "reason": reason,
            })
    return kept, dropped
