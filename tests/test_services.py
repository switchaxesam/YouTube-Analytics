"""Tests for the analysis logic.

Focused on the claims the app makes to the user. If `compute_baseline` is wrong
then every multiplier on every screen is wrong, and it would still *look*
plausible — which is exactly why these are the tests worth having.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from channel_lens.models import Channel, Video, VideoStat
from channel_lens.services import outliers, titles, tracker
from channel_lens.services.owned import _diagnose_one
from channel_lens.youtube.quota import QuotaLedger, cost_of, quota_day

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def make_video(vid, *, views, age_days, is_short=False, channel="UC1"):
    return Video(
        id=vid, channel_id=channel, title=vid,
        published_at=(NOW - timedelta(days=age_days)).replace(tzinfo=None),
        duration_seconds=30 if is_short else 600, is_short=is_short,
        latest_view_count=views, is_live=False,
    )


# --------------------------------------------------------------- baselines


def test_baseline_uses_median_not_mean():
    """One past viral video must not raise the bar for everything after it."""
    videos = [make_video(f"v{i}", views=1000, age_days=30 + i) for i in range(9)]
    videos.append(make_video("viral", views=500_000, age_days=40))

    baseline = outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW,
    )

    assert baseline.median_views == 1000
    # The mean is dragged to ~50k by the one outlier; the median ignores it.
    assert baseline.mean_views > 50_000


def test_young_videos_are_excluded_from_the_baseline():
    """Including immature videos would drag the baseline down and inflate everyone."""
    mature = [make_video(f"m{i}", views=1000, age_days=30 + i) for i in range(6)]
    fresh = [make_video(f"f{i}", views=50, age_days=1) for i in range(6)]

    baseline = outliers.compute_baseline(
        mature + fresh, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW,
    )

    assert baseline.sample_size == 6
    assert baseline.median_views == 1000


def test_thin_baseline_is_flagged_not_hidden():
    videos = [make_video(f"v{i}", views=1000, age_days=30 + i) for i in range(3)]
    baseline = outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW,
    )

    assert baseline.reliable is False
    assert baseline.caveats
    assert "3" in baseline.caveats[0]


def test_no_mature_videos_yields_no_baseline():
    """Better to say 'not enough history' than to invent a denominator."""
    videos = [make_video(f"v{i}", views=1000, age_days=2) for i in range(9)]
    assert outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW,
    ) is None


def test_shorts_and_long_form_get_separate_baselines():
    videos = ([make_video(f"l{i}", views=1_000, age_days=30 + i) for i in range(6)] +
              [make_video(f"s{i}", views=200_000, age_days=30 + i, is_short=True) for i in range(6)])

    long_baseline = outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14, min_videos=5,
        window=30, now=NOW)
    short_baseline = outliers.compute_baseline(
        videos, channel_id="UC1", is_short=True, min_age_days=14, min_videos=5,
        window=30, now=NOW)

    assert long_baseline.median_views == 1_000
    assert short_baseline.median_views == 200_000
    assert long_baseline.sample_size == short_baseline.sample_size == 6


# ------------------------------------------------------------------ scoring


def test_multiplier_is_views_over_baseline():
    videos = [make_video(f"v{i}", views=1000, age_days=30 + i) for i in range(9)]
    videos.append(make_video("hit", views=9000, age_days=25))

    baselines = {"UC1:long": outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW)}
    scores = outliers.score_videos(
        videos, baselines, {"UC1": "Channel"}, outlier_threshold=3.0,
        min_age_days=14, now=NOW)

    hit = next(s for s in scores if s.video_id == "hit")
    assert hit.multiplier == pytest.approx(9.0)
    assert hit.is_outlier is True
    assert scores[0].video_id == "hit"  # sorted by multiplier


def test_a_young_video_gets_a_projection_and_a_caveat():
    videos = [make_video(f"v{i}", views=1000, age_days=30 + i) for i in range(9)]
    videos.append(make_video("new", views=1000, age_days=2))

    baselines = {"UC1:long": outliers.compute_baseline(
        videos, channel_id="UC1", is_short=False, min_age_days=14,
        min_videos=5, window=30, now=NOW)}
    scores = outliers.score_videos(
        videos, baselines, {"UC1": "Channel"}, outlier_threshold=3.0,
        min_age_days=14, now=NOW)

    fresh = next(s for s in scores if s.video_id == "new")
    assert fresh.multiplier == pytest.approx(1.0)
    # Two days in, a video has only ~28% of its eventual views.
    assert fresh.projected_multiplier > 3.0
    assert fresh.reliable is False
    assert any("days old" in c for c in fresh.caveats)


def test_a_video_with_no_baseline_is_skipped_not_guessed():
    videos = [make_video("orphan", views=5000, age_days=30, channel="UC_other")]
    scores = outliers.score_videos(
        videos, {}, {}, outlier_threshold=3.0, min_age_days=14, now=NOW)
    assert scores == []


# ------------------------------------------------------------------ velocity


def test_velocity_needs_two_snapshots():
    one = [VideoStat(video_id="v", captured_at=NOW.replace(tzinfo=None), view_count=100)]
    assert tracker.compute_velocity(one) is None


def test_velocity_is_views_per_day():
    stats = [
        VideoStat(video_id="v", captured_at=(NOW - timedelta(days=2)).replace(tzinfo=None),
                  view_count=1000),
        VideoStat(video_id="v", captured_at=NOW.replace(tzinfo=None), view_count=3000),
    ]
    reading = tracker.compute_velocity(stats)
    assert reading.views_per_day == pytest.approx(1000.0)
    assert reading.reliable is True


def test_short_window_velocity_is_marked_unreliable():
    """Under six hours, YouTube's own counter rounding dominates."""
    stats = [
        VideoStat(video_id="v", captured_at=(NOW - timedelta(hours=1)).replace(tzinfo=None),
                  view_count=1000),
        VideoStat(video_id="v", captured_at=NOW.replace(tzinfo=None), view_count=1010),
    ]
    assert tracker.compute_velocity(stats).reliable is False


# -------------------------------------------------------------------- titles


def test_title_features_are_deterministic():
    features = titles.extract_features("How I Built a $500 Desk in 3 DAYS (Full Build)")
    assert features.has_number is True
    assert features.has_brackets is True
    assert features.allcaps_word_count == 1
    assert features.opening_ngram == "how i built"


def test_title_comparison_refuses_a_tiny_sample():
    """Reporting a difference between three and four titles would be noise."""
    assert titles.compare_title_sets(["a b c"] * 3, ["d e f"] * 3) == []


def test_title_comparison_reports_a_real_gap():
    short_titles = ["Why X Fails", "How I Fixed X", "X Is Dead", "Stop Doing X", "The X Myth"]
    long_titles = ["A Very Long And Complete Guide To Absolutely Everything About X"] * 5

    comparisons = titles.compare_title_sets(short_titles, long_titles)
    length = next(c for c in comparisons if c.metric == "char_count")

    assert length.delta > 20
    assert "longer" not in length.reading  # phrased with the unit, not a bare word
    assert "characters" in length.reading


# ----------------------------------------------------------------- diagnosis


class _FakeSession:
    def get(self, _model, _key):
        return None


@pytest.mark.parametrize(
    "ctr,retention,expected",
    [
        (2.0, 60.0, "packaging"),    # low CTR, high retention
        (8.0, 20.0, "overpromise"),  # high CTR, low retention
        (8.0, 60.0, "working"),
        (2.0, 20.0, "topic"),
    ],
)
def test_quadrant_assignment(ctr, retention, expected):
    from channel_lens.models import OwnedVideoDaily

    row = OwnedVideoDaily(
        video_id="v", day=NOW.date(), views=1000, impressions=10_000,
        impressions_ctr=ctr, average_view_percentage=retention,
    )
    result = _diagnose_one(_FakeSession(), row, median_ctr=5.0, median_retention=40.0)
    assert result.quadrant == expected
    # Every reading must explain itself with the actual numbers.
    assert str(int(ctr)) in result.detail


def test_packaging_quadrant_quantifies_the_upside():
    from channel_lens.models import OwnedVideoDaily

    row = OwnedVideoDaily(
        video_id="v", day=NOW.date(), views=200, impressions=10_000,
        impressions_ctr=2.0, average_view_percentage=60.0,
    )
    result = _diagnose_one(_FakeSession(), row, median_ctr=5.0, median_retention=40.0)
    # 3 percentage points of 10,000 impressions is 300 views left on the table.
    assert result.upside_views == 300
    assert "300" in result.detail


def test_missing_ctr_explains_the_48_hour_wait():
    from channel_lens.models import OwnedVideoDaily

    row = OwnedVideoDaily(video_id="v", day=NOW.date(), views=100,
                          average_view_percentage=50.0)
    result = _diagnose_one(_FakeSession(), row, None, None)
    assert result.quadrant == "unknown"
    assert "48 hours" in result.detail


# ---------------------------------------------------------------- quota


def test_search_is_a_hundred_times_the_price_of_a_fetch():
    """The ratio the whole app's design follows from."""
    assert cost_of("search.list") == 100 * cost_of("videos.list")


def test_ledger_refuses_before_spending(session):
    ledger = QuotaLedger(budget=100)
    ledger.record(session, "search.list")  # 100 units — budget now gone

    from channel_lens.youtube.errors import QuotaExceeded

    with pytest.raises(QuotaExceeded) as caught:
        ledger.check(session, 1, what="Another request")

    assert "Another request" in caught.value.message
    assert caught.value.hint  # must tell the user what to do about it


def test_ledger_tracks_per_endpoint(session):
    ledger = QuotaLedger(budget=9000)
    ledger.record(session, "videos.list", calls=3)
    ledger.record(session, "search.list")

    status = ledger.status(session)
    assert status.by_endpoint == {"videos.list": 3, "search.list": 100}
    assert status.used == 103
    assert status.remaining == 8897


def test_quota_day_follows_pacific_not_local():
    """Google's reset is midnight US/Pacific; using UTC would be a day out."""
    assert quota_day(datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)).day == 1
    assert quota_day(datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)).day == 2


# --------------------------------------------------- local thumbnail metrics


def _image(draw_fn, size=(1280, 720), background=(20, 20, 30)):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, background)
    draw_fn(ImageDraw.Draw(img))
    return img


def test_detail_retention_separates_clean_from_mush():
    """The metric that decides whether an image survives being shown small.

    The first version compared mean edge energy before and after downscaling
    and returned ~1.0 for everything, so it never fired. This asserts it
    actually discriminates.
    """
    from PIL import Image

    from channel_lens.services import thumbnails

    clean = _image(lambda d: d.ellipse([200, 120, 900, 600], fill=(240, 90, 40)))
    busy = Image.effect_noise((1280, 720), 80).convert("RGB")

    clean_score = thumbnails.analyse_image(clean).detail_retention
    busy_score = thumbnails.analyse_image(busy).detail_retention

    assert clean_score > 0.85
    assert busy_score < 0.5
    assert thumbnails.analyse_image(busy).loses_detail is True
    assert thumbnails.analyse_image(clean).loses_detail is False


def test_visual_weight_is_not_dragged_to_centre_by_the_background():
    """A large flat background must not outvote the actual subject.

    Using absolute brightness made every centroid land dead centre, including
    for a subject jammed against an edge.
    """
    from channel_lens.services import thumbnails

    left = _image(lambda d: d.ellipse([40, 200, 380, 540], fill=(250, 200, 40)))
    right = _image(lambda d: d.ellipse([900, 200, 1240, 540], fill=(250, 200, 40)))

    assert thumbnails.analyse_image(left).weight_x < 0.4
    assert thumbnails.analyse_image(right).weight_x > 0.6
    assert "far right" in thumbnails.analyse_image(right).composition_note


def test_text_like_detection_finds_an_overlay_and_ignores_a_plain_image():
    from channel_lens.services import thumbnails

    plain = _image(lambda d: d.ellipse([400, 200, 880, 520], fill=(120, 120, 130)))

    def dense_text(d):
        for row in range(5):
            for x in range(60, 1220, 7):
                d.rectangle([x, 120 + row * 120, x + 3, 200 + row * 120],
                            fill=(255, 255, 255))

    texty = _image(dense_text)

    assert thumbnails.analyse_image(plain).text_area_estimate < 0.05
    assert thumbnails.analyse_image(texty).text_area_estimate > 0.15
    assert thumbnails.analyse_image(texty).is_text_heavy is True


def test_a_flat_image_is_not_reported_as_losing_detail():
    """Nothing to lose is not a legibility failure."""
    from PIL import Image

    from channel_lens.services import thumbnails

    flat = Image.new("RGB", (640, 360), (128, 128, 128))
    assert thumbnails.analyse_image(flat).detail_retention == 1.0


def test_observations_stay_silent_on_a_good_thumbnail():
    """Silence is what gives the warnings their weight."""
    from channel_lens.services import thumbnails

    good = _image(
        lambda d: (d.ellipse([120, 140, 620, 600], fill=(245, 95, 45)),
                   d.rectangle([700, 250, 1180, 430], fill=(250, 250, 250))),
        background=(40, 60, 110),
    )
    notes = thumbnails.analyse_image(good).observations()
    assert not any("detail survives" in n for n in notes)
    assert not any("Visually busy" in n for n in notes)


def test_missing_columns_are_added_to_an_existing_database():
    """create_all never ALTERs, so a new column on an old database is absent.

    That is the failure this project hit twice; it surfaces later as an
    opaque 'no such column' at query time.
    """
    from sqlalchemy import Column, Float

    from channel_lens import db
    from channel_lens.models import ThumbnailAnalysis

    table = ThumbnailAnalysis.__table__
    table.append_column(Column("a_later_addition", Float, nullable=True))
    try:
        added = db.add_missing_columns()
        assert "thumbnail_analyses.a_later_addition" in added

        with db.get_engine().begin() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql(
                    "PRAGMA table_info('thumbnail_analyses')"
                )
            }
        assert "a_later_addition" in columns

        # Running again is a no-op rather than an error.
        assert db.add_missing_columns() == []
    finally:
        table._columns.remove(table.c.a_later_addition)


# -------------------------------------------------------------------- PKCE


def test_pkce_verifier_survives_between_the_two_flow_objects():
    """The auth URL and the token exchange are built by separate Flow objects.

    The library generates a PKCE verifier when the URL is built and needs the
    same value at the exchange. Losing it fails with Google's opaque
    "(invalid_grant) Missing code verifier", which is what shipped first.
    """
    from channel_lens.youtube import analytics

    url, state = analytics.build_auth_url(
        "cid.apps.googleusercontent.com", "GOCSPX-secret",
        "http://127.0.0.1:8730/api/auth/callback",
    )

    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url

    verifier = analytics._take_verifier(state)
    assert verifier and len(verifier) >= 43  # RFC 7636 minimum


def test_pkce_verifier_is_single_use():
    from channel_lens.youtube import analytics

    _url, state = analytics.build_auth_url(
        "cid", "secret", "http://127.0.0.1:8730/api/auth/callback")

    assert analytics._take_verifier(state) is not None
    assert analytics._take_verifier(state) is None


def test_pkce_verifier_requires_a_matching_state():
    """A callback from a different attempt must not collect this verifier."""
    from channel_lens.youtube import analytics

    analytics.build_auth_url(
        "cid", "secret", "http://127.0.0.1:8730/api/auth/callback")
    assert analytics._take_verifier("some-other-state") is None


def test_lost_verifier_is_explained_not_blamed_on_the_code():
    """The raw error says invalid_grant, which sends you down the wrong path."""
    from channel_lens.youtube.analytics import _explain_exchange_failure

    err = _explain_exchange_failure(Exception("(invalid_grant) Missing code verifier."))
    assert "security check" in err.message.lower()
    assert "Connect again" in err.hint
    assert "Missing code verifier" in err.hint  # raw text always preserved
