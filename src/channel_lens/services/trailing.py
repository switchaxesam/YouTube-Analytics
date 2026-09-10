"""Trailing baselines: judging a video against what was normal *at the time*.

The whole-catalogue baseline in :mod:`outliers` answers "how does this video
compare to this channel's typical upload". That is the right question for a
channel whose numbers have been stable, and the wrong one for a channel that
grew — which is most channels worth studying.

Two failures follow from it:

* **Old videos are held to a bar that didn't exist yet.** A 2019 upload is
  divided by a median containing five years of subsequent growth, so it looks
  like a failure even if it doubled what the channel was doing that month.
* **Recent videos are flattered or punished by an earlier breakout.** One video
  that brought a wave of subscribers lifts the median for everything after it,
  and every later upload is then measured against a floor that video created.

The fix is a *trailing* baseline: for each video, the median of the same
channel's uploads published strictly before it, inside a window. The comparison
population is then contemporaneous with the video being judged.

**A known bias, stated rather than hidden.** Views are current, not historical —
the API exposes no per-date view counts. The videos in a trailing window were
published earlier, so by today they have had slightly *longer* to accumulate,
which biases every trailing multiplier a little downward. For an old video the
gap is negligible (three years versus three years and one month); for a video
posted last week against a window of videos from last month it is not, and
those are flagged as still maturing exactly as elsewhere in the app.

Shorts and long-form keep separate windows here for the same reason they keep
separate baselines everywhere else: pooling two populations that differ by an
order of magnitude describes neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Sequence

import numpy as np

log = logging.getLogger(__name__)

WindowKind = Literal["count", "days"]


@dataclass(frozen=True)
class TrailingWindow:
    """Which prior videos form the comparison population.

    ``count`` suits channels that upload irregularly — fifteen uploads is
    fifteen uploads whether they span two months or two years. ``days`` suits
    channels with a steady cadence, and is the better choice when a channel's
    upload rate itself changed.
    """

    kind: WindowKind = "count"
    count: int = 15
    days: int = 180

    def describe(self) -> str:
        """
        >>> TrailingWindow().describe()
        'the 15 uploads before it'
        >>> TrailingWindow(kind="days", days=90).describe()
        'uploads from the 90 days before it'
        """
        if self.kind == "days":
            return f"uploads from the {self.days} days before it"
        return f"the {self.count} uploads before it"


@dataclass
class TrailingScore:
    """One video measured against its own moment."""

    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    views: int
    is_short: bool
    thumbnail_url: str | None = None

    #: The trailing population, when there was enough of it.
    trailing_median: float | None = None
    trailing_mean: float | None = None
    trailing_sample: int = 0
    #: views ÷ trailing median. The headline of this module.
    trailing_multiplier: float | None = None
    #: Rank within that same trailing population, 0–100.
    trailing_percentile: float | None = None

    #: The existing whole-catalogue figure, kept alongside for comparison
    #: rather than replaced.
    catalog_multiplier: float | None = None

    sufficient: bool = False
    #: Why there is no trailing number, when there isn't one.
    reason: str = ""
    caveats: list[str] = field(default_factory=list)

    #: "breakout", "spike", or None. Set by :func:`detect_breakouts`.
    breakout: str | None = None
    breakout_ratio: float | None = None
    median_before: float | None = None
    median_after: float | None = None

    @property
    def drift(self) -> float | None:
        """How far the trailing verdict departs from the catalogue one.

        The number this whole module exists to surface: greater than 1 means
        the video looks better against its own era than against the catalogue.
        """
        if not self.trailing_multiplier or not self.catalog_multiplier:
            return None
        return self.trailing_multiplier / self.catalog_multiplier

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "published_at": self.published_at.isoformat(),
            "views": self.views,
            "is_short": self.is_short,
            "thumbnail_url": self.thumbnail_url,
            "trailing_median": round(self.trailing_median) if self.trailing_median else None,
            "trailing_mean": round(self.trailing_mean) if self.trailing_mean else None,
            "trailing_sample": self.trailing_sample,
            "trailing_multiplier": (
                round(self.trailing_multiplier, 2) if self.trailing_multiplier else None
            ),
            "trailing_percentile": (
                round(self.trailing_percentile, 1) if self.trailing_percentile is not None else None
            ),
            "catalog_multiplier": (
                round(self.catalog_multiplier, 2) if self.catalog_multiplier else None
            ),
            "drift": round(self.drift, 2) if self.drift else None,
            "sufficient": self.sufficient,
            "reason": self.reason,
            "caveats": self.caveats,
            "breakout": self.breakout,
            "breakout_ratio": round(self.breakout_ratio, 2) if self.breakout_ratio else None,
            "median_before": round(self.median_before) if self.median_before else None,
            "median_after": round(self.median_after) if self.median_after else None,
            "url": f"https://www.youtube.com/watch?v={self.video_id}",
        }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def select_prior(
    ordered: Sequence[Any], index: int, window: TrailingWindow
) -> list[Any]:
    """The comparison population for ``ordered[index]``.

    ``ordered`` must be one channel and one format, sorted oldest first. Only
    videos published *strictly* before the target count — a same-day companion
    upload is not evidence of what was normal before it.

    >>> from datetime import datetime
    >>> class V:
    ...     def __init__(self, n, day):
    ...         self.id = n
    ...         self.published_at = datetime(2026, 1, day)
    >>> vids = [V(i, i + 1) for i in range(10)]
    >>> [v.id for v in select_prior(vids, 5, TrailingWindow(count=3))]
    [2, 3, 4]
    >>> [v.id for v in select_prior(vids, 1, TrailingWindow(count=3))]
    [0]
    >>> select_prior(vids, 0, TrailingWindow(count=3))
    []

    A day window keeps only what falls inside it. Video 9 sits on day 10, so a
    four-day window reaches back to day 6 and takes everything from there:

    >>> [v.id for v in select_prior(vids, 9, TrailingWindow(kind="days", days=4))]
    [5, 6, 7, 8]

    A window that reaches back before the channel started simply returns what
    exists, which the caller then rejects on the minimum-sample rule:

    >>> [v.id for v in select_prior(vids, 2, TrailingWindow(kind="days", days=365))]
    [0, 1]
    """
    if index <= 0:
        return []

    target_time = _aware(ordered[index].published_at)
    prior = [v for v in ordered[:index] if _aware(v.published_at) < target_time]

    if window.kind == "days":
        cutoff = target_time - timedelta(days=window.days)
        return [v for v in prior if _aware(v.published_at) >= cutoff]
    return prior[-window.count:] if window.count > 0 else prior


def score_channel_trailing(
    videos: Iterable[Any],
    *,
    channel_title: str = "",
    window: TrailingWindow | None = None,
    min_prior: int = 8,
    min_age_days: int = 14,
    now: datetime | None = None,
) -> list[TrailingScore]:
    """Score every video in a channel against its own trailing window.

    Formats are separated first, then each is walked oldest-first so that each
    video sees only what preceded it.
    """
    window = window or TrailingWindow()
    now = now or datetime.now(timezone.utc)

    usable = [
        v for v in videos
        if v.published_at is not None
        and v.latest_view_count is not None
        and not getattr(v, "is_live", False)
    ]
    scores: list[TrailingScore] = []

    for is_short in (False, True):
        ordered = sorted(
            (v for v in usable if bool(v.is_short) == is_short),
            key=lambda v: (_aware(v.published_at), v.id),
        )

        for index, video in enumerate(ordered):
            prior = select_prior(ordered, index, window)
            views = int(video.latest_view_count)

            score = TrailingScore(
                video_id=video.id,
                title=video.title or "",
                channel_id=video.channel_id,
                channel_title=channel_title,
                published_at=_aware(video.published_at),
                views=views,
                is_short=bool(video.is_short),
                thumbnail_url=getattr(video, "thumbnail_url", None),
                catalog_multiplier=getattr(video, "outlier_multiplier", None),
                trailing_sample=len(prior),
            )

            if len(prior) < min_prior:
                # Deliberately no number at all. A median over three videos
                # would be quoted with the same authority as one over thirty.
                score.sufficient = False
                score.reason = (
                    f"insufficient history — {len(prior)} prior "
                    f"{'Short' if is_short else 'long-form'} upload"
                    f"{'s' if len(prior) != 1 else ''}, {min_prior} needed"
                )
                scores.append(score)
                continue

            counts = np.array([float(v.latest_view_count or 0) for v in prior])
            median = float(np.median(counts))
            score.trailing_median = median
            score.trailing_mean = float(counts.mean())
            score.sufficient = True

            if median > 0:
                score.trailing_multiplier = views / median
            else:
                score.reason = "prior uploads have no recorded views"

            score.trailing_percentile = float((counts <= views).mean() * 100.0)

            age_days = (now - _aware(video.published_at)).total_seconds() / 86400.0
            if age_days < min_age_days:
                score.caveats.append(
                    f"Only {age_days:.1f} days old — still accumulating views, so this "
                    f"multiplier will rise."
                )
            # The structural bias, stated on every score rather than buried.
            score.caveats.append(
                "Compared against uploads that are now older than it, so they have had "
                "slightly longer to accumulate views. The effect is small for older "
                "videos and larger for recent ones."
            )
            scores.append(score)

    scores.sort(key=lambda s: (s.published_at, s.video_id))
    return scores


# --------------------------------------------------------------------------
# Breakout / step-change detection
# --------------------------------------------------------------------------


def detect_breakouts(
    scores: list[TrailingScore],
    *,
    window: int = 5,
    threshold: float = 3.0,
    revert_ratio: float = 1.5,
) -> list[TrailingScore]:
    """Mark videos where the channel's floor moved, and spikes where it didn't.

    Walks each format chronologically and compares the median of the ``window``
    videos *before* a video against the median of the ``window`` videos
    *after* it. The video itself is excluded from both sides — it is the
    candidate event, not evidence about the level either side of it.

    Two outcomes are marked:

    * **breakout** — the after-median is at least ``threshold`` times the
      before-median. The channel's normal moved up and stayed up.
    * **spike, not sustained** — the video itself cleared ``threshold`` against
      the before-median, but the after-median came back within
      ``revert_ratio`` of it. One good video, no lasting change.

    The distinction is the entire point. A breakout is a repeatable format; a
    spike is a lottery ticket, and treating the second as the first is how
    creators chase something that already finished happening.

    Videos without a full window on both sides are left unmarked rather than
    guessed at — which necessarily includes the most recent ``window`` uploads,
    whose "after" simply hasn't happened yet.

    **Adjacent candidates collapse to one event.** A step change makes every
    video near it look like a breakout, because each of their "after" windows
    straddles the same transition. Reporting six breakouts for one step is
    worse than useless — the whole question is *which* upload moved the floor.
    Runs of neighbouring candidates are therefore reduced to their sharpest
    member, and the rest are left unflagged.
    """
    by_format: dict[bool, list[TrailingScore]] = {False: [], True: []}
    for score in scores:
        by_format[score.is_short].append(score)

    for group in by_format.values():
        group.sort(key=lambda s: (s.published_at, s.video_id))
        views = [float(s.views) for s in group]
        candidates: list[tuple[int, float, float]] = []

        for index, score in enumerate(group):
            before = views[max(0, index - window):index]
            after = views[index + 1:index + 1 + window]
            if len(before) < window or len(after) < window:
                continue

            median_before = float(np.median(before))
            median_after = float(np.median(after))
            if median_before <= 0:
                continue

            ratio = median_after / median_before
            score.median_before = median_before
            score.median_after = median_after
            score.breakout_ratio = ratio

            if ratio >= threshold:
                # Prominence is the video's own standing against the old level.
                # It breaks the ties that ratio alone leaves: every video near a
                # step change shares the same before/after medians, so ratio
                # cannot say which one *is* the event.
                candidates.append((index, ratio, score.views / median_before))
            elif score.views >= median_before * threshold and ratio < revert_ratio:
                # A spike is a property of one upload, so it never clusters the
                # way a step change does and is marked immediately.
                score.breakout = "spike"

        for index in _sharpest_of_each_run(candidates, window):
            group[index].breakout = "breakout"

    return scores


def _sharpest_of_each_run(
    candidates: list[tuple[int, float, float]], window: int
) -> list[int]:
    """Reduce runs of neighbouring breakout candidates to one index each.

    Each entry is ``(index, ratio, prominence)``. Candidates within ``window``
    positions of one another are the same transition seen from different
    vantage points, so only one survives.

    Ratio decides first, then prominence — the video's own standing against the
    old level. That second term is doing real work rather than breaking rare
    ties: *every* video adjacent to a step change shares the same before and
    after medians, so their ratios are identical by construction and ratio
    alone would just pick whichever came first. Prominence picks the upload
    that actually marks the shift.

    >>> _sharpest_of_each_run([(9, 4.0, 1.0), (10, 12.0, 2.0), (11, 6.0, 1.0)], 5)
    [10]

    Identical ratios, resolved by which video is itself the event:

    >>> _sharpest_of_each_run([(9, 10.0, 1.0), (12, 10.0, 30.0), (13, 10.0, 2.0)], 5)
    [12]

    Genuinely separate events, further apart than the window, both survive:

    >>> _sharpest_of_each_run([(3, 5.0, 9.0), (40, 4.0, 7.0)], 5)
    [3, 40]
    >>> _sharpest_of_each_run([], 5)
    []
    """
    if not candidates:
        return []

    def rank(candidate: tuple[int, float, float]) -> tuple[float, float]:
        return candidate[1], candidate[2]

    chosen: list[int] = []
    run: list[tuple[int, float, float]] = [candidates[0]]

    for candidate in candidates[1:]:
        if candidate[0] - run[-1][0] <= window:
            run.append(candidate)
        else:
            chosen.append(max(run, key=rank)[0])
            run = [candidate]
    chosen.append(max(run, key=rank)[0])
    return chosen


# --------------------------------------------------------------------------
# Per-channel summary
# --------------------------------------------------------------------------


@dataclass
class ChannelSummary:
    """What the trailing view says about a channel as a whole."""

    channel_id: str
    channel_title: str
    total_videos: int
    scored: int
    insufficient: int
    hit_rate_5x: float
    hit_rate_10x: float
    median_trailing_multiplier: float | None
    breakouts: list[dict[str, Any]] = field(default_factory=list)
    spikes: list[dict[str, Any]] = field(default_factory=list)
    window: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "total_videos": self.total_videos,
            "scored": self.scored,
            "insufficient": self.insufficient,
            "hit_rate_5x": round(self.hit_rate_5x, 1),
            "hit_rate_10x": round(self.hit_rate_10x, 1),
            "median_trailing_multiplier": (
                round(self.median_trailing_multiplier, 2)
                if self.median_trailing_multiplier else None
            ),
            "breakouts": self.breakouts,
            "spikes": self.spikes,
            "window": self.window,
        }


def summarise_channel(
    scores: list[TrailingScore], *, channel_id: str, channel_title: str, window: str = "",
) -> ChannelSummary:
    """Roll a channel's trailing scores into a few honest headline numbers.

    Hit rates are expressed over the videos that *could* be scored, not over
    the whole catalogue — dividing by videos that were never eligible would
    quietly understate every channel's rate in proportion to how much early
    history it has.

    >>> from datetime import datetime, timezone
    >>> def s(vid, mult, ok=True):
    ...     x = TrailingScore(vid, vid, "UC", "C",
    ...                       datetime(2026, 1, 1, tzinfo=timezone.utc), 100, False)
    ...     x.sufficient, x.trailing_multiplier = ok, mult
    ...     return x
    >>> summary = summarise_channel(
    ...     [s("a", 6.0), s("b", 12.0), s("c", 1.0), s("d", None, ok=False)],
    ...     channel_id="UC", channel_title="C")
    >>> summary.scored, summary.insufficient
    (3, 1)

    Rates keep full precision here and are rounded only for display:

    >>> round(summary.hit_rate_5x, 1), round(summary.hit_rate_10x, 1)
    (66.7, 33.3)
    >>> summary.to_dict()["hit_rate_5x"]
    66.7

    The denominator is the videos that could be scored, not the whole
    catalogue — otherwise every channel's rate would be understated in
    proportion to how much early history it has:

    >>> summary.total_videos
    4
    """
    scored = [s for s in scores if s.sufficient and s.trailing_multiplier is not None]
    insufficient = sum(1 for s in scores if not s.sufficient)

    multipliers = [s.trailing_multiplier for s in scored]
    denominator = len(scored) or 1

    def rate(threshold: float) -> float:
        return sum(1 for m in multipliers if m >= threshold) / denominator * 100.0

    breakouts = [
        {
            "video_id": s.video_id, "title": s.title,
            "published_at": s.published_at.isoformat(),
            "views": s.views,
            "median_before": round(s.median_before) if s.median_before else None,
            "median_after": round(s.median_after) if s.median_after else None,
            "ratio": round(s.breakout_ratio, 2) if s.breakout_ratio else None,
            "is_short": s.is_short,
        }
        for s in scores if s.breakout == "breakout"
    ]
    spikes = [
        {
            "video_id": s.video_id, "title": s.title,
            "published_at": s.published_at.isoformat(),
            "views": s.views,
            "median_before": round(s.median_before) if s.median_before else None,
            "median_after": round(s.median_after) if s.median_after else None,
            "ratio": round(s.breakout_ratio, 2) if s.breakout_ratio else None,
            "is_short": s.is_short,
        }
        for s in scores if s.breakout == "spike"
    ]

    return ChannelSummary(
        channel_id=channel_id,
        channel_title=channel_title,
        total_videos=len(scores),
        scored=len(scored),
        insufficient=insufficient,
        hit_rate_5x=rate(5.0),
        hit_rate_10x=rate(10.0),
        median_trailing_multiplier=float(np.median(multipliers)) if multipliers else None,
        breakouts=breakouts,
        spikes=spikes,
        window=window,
    )
