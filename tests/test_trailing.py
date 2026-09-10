"""Tests for trailing baselines and breakout detection.

The premise these exist to defend: a whole-catalogue median judges an old video
against years of growth it never had, and a recent one against a floor an
earlier breakout created. Each test below constructs a channel where the two
methods must disagree, and asserts the trailing one is right.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from channel_lens.models import Channel, Video
from channel_lens.services import outliers, trailing
from channel_lens.services.trailing import TrailingWindow

START = datetime(2023, 1, 1, tzinfo=timezone.utc)


def make(vid, *, views, week, is_short=False, channel="UC1"):
    """A video published ``week`` weeks after START."""
    return Video(
        id=vid, channel_id=channel, title=vid,
        published_at=(START + timedelta(weeks=week)).replace(tzinfo=None),
        duration_seconds=30 if is_short else 600, is_short=is_short,
        latest_view_count=views, is_live=False,
    )


def growing_channel():
    """A channel that grew tenfold: 1k views early, 10k later.

    Every early video is ordinary *for its time* and looks like a failure
    against the whole-catalogue median.
    """
    early = [make(f"early{i}", views=1_000, week=i) for i in range(15)]
    late = [make(f"late{i}", views=10_000, week=20 + i) for i in range(15)]
    return early + late


# ------------------------------------------------------------- the premise


def test_the_catalog_median_understates_an_ordinary_early_video():
    """The bug this feature exists to fix, asserted directly."""
    videos = growing_channel()
    now = START + timedelta(weeks=40)

    baseline = outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=100, now=now,
    )
    # The catalogue median sits between the two eras, so an utterly typical
    # early video reads as a substantial underperformer.
    catalog_multiplier = 1_000 / baseline.median_views
    assert catalog_multiplier < 0.5

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8, now=now,
    )
    early = next(s for s in scores if s.video_id == "early12")

    # Against its own era it is exactly normal, which is the truth.
    assert early.trailing_multiplier == pytest.approx(1.0)


def test_a_late_video_is_not_flattered_by_early_history():
    videos = growing_channel()
    now = START + timedelta(weeks=40)

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8, now=now,
    )
    late = next(s for s in scores if s.video_id == "late12")

    # Ordinary for its era, despite being 10x an early video.
    assert late.trailing_multiplier == pytest.approx(1.0)
    assert late.trailing_median == pytest.approx(10_000)


# --------------------------------------------------------------- windowing


def test_only_strictly_earlier_videos_count():
    """A video must never be compared against itself or its own future."""
    videos = [make(f"v{i}", views=(i + 1) * 100, week=i) for i in range(12)]
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=5), min_prior=3, now=START + timedelta(weeks=30),
    )
    fifth = next(s for s in scores if s.video_id == "v5")

    # v0..v4 only: 100, 200, 300, 400, 500 -> median 300.
    assert fifth.trailing_sample == 5
    assert fifth.trailing_median == pytest.approx(300)


def test_a_day_window_ignores_uploads_outside_it():
    """Count and day windows must genuinely differ on an irregular schedule.

    A channel that went quiet for two years and came back bigger is the case
    that separates them: a count window reaches back across the silence and
    mixes the two eras, while a day window sees only the current one.
    """
    old = [make(f"old{i}", views=100, week=i) for i in range(20)]
    recent = [make(f"new{i}", views=5_000, week=100 + i) for i in range(10)]
    videos = old + recent
    now = START + timedelta(weeks=130)
    target = "new9"

    by_count = trailing.score_channel_trailing(
        videos, window=TrailingWindow(kind="count", count=25), min_prior=8, now=now,
    )
    by_days = trailing.score_channel_trailing(
        videos, window=TrailingWindow(kind="days", days=90), min_prior=8, now=now,
    )

    counted = next(s for s in by_count if s.video_id == target)
    dated = next(s for s in by_days if s.video_id == target)

    # The count window is still dominated by the pre-hiatus era…
    assert counted.trailing_median == pytest.approx(100)
    # …while the day window reflects only what the channel does now.
    assert dated.trailing_median == pytest.approx(5_000)
    # Which means the same video reads as a 50x hit or a perfectly normal
    # upload depending purely on the window — hence making it a per-run choice.
    assert counted.trailing_multiplier == pytest.approx(50.0)
    assert dated.trailing_multiplier == pytest.approx(1.0)


def test_shorts_never_enter_a_long_form_window():
    videos = ([make(f"long{i}", views=1_000, week=i) for i in range(12)] +
              [make(f"short{i}", views=500_000, week=i, is_short=True) for i in range(12)])
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8,
        now=START + timedelta(weeks=30),
    )

    long_form = next(s for s in scores if s.video_id == "long11")
    short = next(s for s in scores if s.video_id == "short11")

    assert long_form.trailing_median == pytest.approx(1_000)
    assert short.trailing_median == pytest.approx(500_000)


# --------------------------------------------------- insufficient history


def test_early_videos_get_no_number_rather_than_a_bad_one():
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(20)]
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=15), min_prior=8,
        now=START + timedelta(weeks=40),
    )

    first_eight = [s for s in scores if s.video_id in {f"v{i}" for i in range(8)}]
    assert all(not s.sufficient for s in first_eight)
    assert all(s.trailing_multiplier is None for s in first_eight)
    assert all("insufficient history" in s.reason for s in first_eight)

    # And the ninth, which finally has eight predecessors, is scored.
    ninth = next(s for s in scores if s.video_id == "v8")
    assert ninth.sufficient is True
    assert ninth.trailing_multiplier is not None


def test_the_minimum_is_configurable():
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(20)]
    lenient = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=15), min_prior=3,
        now=START + timedelta(weeks=40),
    )
    assert next(s for s in lenient if s.video_id == "v3").sufficient is True


# ---------------------------------------------------------- percentile


def test_percentile_is_against_the_trailing_population():
    """Not against the whole catalogue, which would mix eras."""
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(12)]
    videos.append(make("hit", views=5_000, week=12))
    videos += [make(f"after{i}", views=50_000, week=13 + i) for i in range(10)]

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8,
        now=START + timedelta(weeks=40),
    )
    hit = next(s for s in scores if s.video_id == "hit")

    # It beat everything that preceded it, regardless of what came later.
    assert hit.trailing_percentile == pytest.approx(100.0)
    assert hit.trailing_multiplier == pytest.approx(5.0)


# ------------------------------------------------------------- breakouts


def test_a_sustained_step_change_is_flagged_as_a_breakout():
    """The channel's floor moves and stays moved."""
    videos = ([make(f"before{i}", views=1_000, week=i) for i in range(10)] +
              [make("theone", views=40_000, week=10)] +
              [make(f"after{i}", views=12_000, week=11 + i) for i in range(10)])

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=3,
        now=START + timedelta(weeks=40),
    )
    trailing.detect_breakouts(scores, window=5, threshold=3.0, revert_ratio=1.5)

    event = next(s for s in scores if s.video_id == "theone")
    assert event.breakout == "breakout"
    assert event.median_before == pytest.approx(1_000)
    assert event.median_after == pytest.approx(12_000)
    assert event.breakout_ratio == pytest.approx(12.0)


def test_a_spike_that_reverts_is_not_a_breakout():
    """One good video, no lasting change — the distinction that matters."""
    videos = ([make(f"before{i}", views=1_000, week=i) for i in range(10)] +
              [make("fluke", views=40_000, week=10)] +
              [make(f"after{i}", views=1_100, week=11 + i) for i in range(10)])

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=3,
        now=START + timedelta(weeks=40),
    )
    trailing.detect_breakouts(scores, window=5, threshold=3.0, revert_ratio=1.5)

    event = next(s for s in scores if s.video_id == "fluke")
    assert event.breakout == "spike"
    assert event.breakout_ratio == pytest.approx(1.1)


def test_a_steady_channel_has_no_breakouts():
    videos = [make(f"v{i}", views=1_000 + i * 10, week=i) for i in range(30)]
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=3,
        now=START + timedelta(weeks=50),
    )
    trailing.detect_breakouts(scores, window=5, threshold=3.0)

    assert all(s.breakout is None for s in scores)


def test_recent_videos_are_left_unjudged_rather_than_guessed():
    """The last N uploads have no 'after' yet, and that isn't evidence of calm."""
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(20)]
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=3,
        now=START + timedelta(weeks=40),
    )
    trailing.detect_breakouts(scores, window=5, threshold=3.0)

    last_five = sorted(scores, key=lambda s: s.published_at)[-5:]
    assert all(s.breakout is None and s.breakout_ratio is None for s in last_five)


# --------------------------------------------------------------- summary


def test_summary_reports_hit_rates_and_breakouts():
    videos = ([make(f"before{i}", views=1_000, week=i) for i in range(12)] +
              [make("step", views=30_000, week=12)] +
              [make(f"after{i}", views=10_000, week=13 + i) for i in range(12)])

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8,
        now=START + timedelta(weeks=50),
    )
    trailing.detect_breakouts(scores, window=5, threshold=3.0)
    summary = trailing.summarise_channel(
        scores, channel_id="UC1", channel_title="Test", window="the 10 uploads before it",
    )

    assert summary.total_videos == 25
    assert summary.insufficient == 8          # the first eight have no history
    assert summary.scored == 17
    assert summary.hit_rate_5x > 0
    assert len(summary.breakouts) == 1

    event = summary.breakouts[0]
    assert event["video_id"] == "step"
    assert event["median_before"] == 1_000
    assert event["median_after"] == 10_000
    assert "2023" in event["published_at"]


def test_hit_rate_denominator_excludes_unscorable_videos():
    """Dividing by the whole catalogue would understate every channel's rate."""
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(12)]
    videos.append(make("big", views=10_000, week=12))

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8,
        now=START + timedelta(weeks=30),
    )
    summary = trailing.summarise_channel(scores, channel_id="UC1", channel_title="T")

    assert summary.scored == 5          # v8..v12
    assert summary.total_videos == 13
    # One of five scored videos cleared 5x, not one of thirteen.
    assert summary.hit_rate_5x == pytest.approx(20.0)


# -------------------------------------------------------------- honesty


def test_every_score_carries_the_structural_caveat():
    """The comparison population is older, so multipliers skew slightly low."""
    videos = [make(f"v{i}", views=1_000, week=i) for i in range(15)]
    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8,
        now=START + timedelta(weeks=30),
    )
    scored = [s for s in scores if s.sufficient]

    assert scored
    assert all(any("longer to accumulate" in c for c in s.caveats) for s in scored)


def test_drift_quantifies_the_disagreement_between_methods():
    videos = growing_channel()
    now = START + timedelta(weeks=40)

    baselines = {"UC1:long": outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=100, now=now)}
    catalog = {
        s.video_id: s.multiplier for s in outliers.score_videos(
            videos, baselines, {"UC1": "T"}, outlier_threshold=3.0,
            min_age_days=14, now=now)
    }
    for video in videos:
        video.outlier_multiplier = catalog.get(video.id)

    scores = trailing.score_channel_trailing(
        videos, window=TrailingWindow(count=10), min_prior=8, now=now)
    early = next(s for s in scores if s.video_id == "early12")

    # Trailing says "normal"; the catalogue said "underperformer". Drift > 1
    # is exactly that disagreement, and it is the number worth surfacing.
    assert early.drift > 2.0
