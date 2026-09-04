"""End-to-end tests over the HTTP surface.

These exist mainly to prove the app is *navigable while unconfigured*. A fresh
install has no API key, no OAuth, and no data, and every screen still has to
render something that explains itself — that is the state a new user is
actually in, and the one easiest to leave broken.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from channel_lens.models import Channel, Video


def test_app_serves_the_frontend(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_status_works_with_no_configuration(client):
    """The shell must load on a fresh install, not 500 on a missing key."""
    response = client.get("/api/status")
    assert response.status_code == 200

    body = response.json()
    assert body["ready"] is False
    assert body["quota"]["used"] == 0
    assert body["quota"]["budget"] == 9000

    keys = {gap["key"] for gap in body["missing"]}
    assert "youtube_api_key" in keys
    # Every gap must say what it blocks and how to fix it — that text is the
    # entire empty-state UI.
    for gap in body["missing"]:
        assert gap["blocks"] and gap["fix"]


def test_secrets_never_leave_the_process(client):
    client.put("/api/settings", json={"youtube_api_key": "AIzaSyTOPSECRETVALUE"})
    body = client.get("/api/settings").json()

    assert "youtube_api_key" not in body
    assert body["youtube_api_key_set"] is True
    assert body["youtube_api_key_hint"] == "…ALUE"
    assert "TOPSECRET" not in response_text(body)


def response_text(payload) -> str:
    import json

    return json.dumps(payload)


def test_blank_secret_does_not_wipe_an_existing_one(client):
    """The UI never holds the real value, so a blank save must mean 'unchanged'."""
    client.put("/api/settings", json={"youtube_api_key": "AIzaSyREALKEY1234"})
    client.put("/api/settings", json={"youtube_api_key": "", "theme": "dark"})

    body = client.get("/api/settings").json()
    assert body["youtube_api_key_set"] is True
    assert body["theme"] == "dark"


def test_secrets_can_be_cleared_explicitly(client):
    client.put("/api/settings", json={"youtube_api_key": "AIzaSyREALKEY1234"})
    client.put("/api/settings", json={"clear_secrets": ["youtube_api_key"]})

    assert client.get("/api/settings").json()["youtube_api_key_set"] is False


def test_unknown_settings_survive_a_round_trip(client):
    """A newer build's settings must not be destroyed by an older one saving."""
    from channel_lens.config import Settings, settings_path
    import json

    settings_path().write_text(
        json.dumps({"port": 8730, "some_future_option": "keep me"}), encoding="utf-8"
    )
    loaded = Settings.load()
    loaded.save()

    assert json.loads(settings_path().read_text())["some_future_option"] == "keep me"


def test_data_endpoints_report_missing_key_actionably(client):
    """A missing API key is a 409 with instructions, not a 500."""
    response = client.post("/api/channels", json={"reference": "@someone"})
    assert response.status_code == 409

    body = response.json()
    assert body["error"] == "NotConfigured"
    assert "Settings" in body["hint"]


def test_outliers_returns_empty_rather_than_erroring(client):
    response = client.get("/api/outliers")
    assert response.status_code == 200
    assert response.json() == {"results": [], "total": 0, "baselines": {}}


def test_outlier_filters_are_applied(client, session):
    """Filters must narrow results without needing any API access."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(Channel(id="UC_test", title="Test Channel"))
    for i in range(12):
        session.add(
            Video(
                id=f"vid{i:03d}",
                channel_id="UC_test",
                title=f"Long-form video {i}",
                published_at=now - timedelta(days=40 + i),
                duration_seconds=600,
                is_short=False,
                latest_view_count=1000,
                outlier_multiplier=1.0,
            )
        )
    # One genuine breakout, and one Short that must not be pooled with it.
    session.add(
        Video(
            id="breakout", channel_id="UC_test", title="The breakout",
            published_at=now - timedelta(days=30), duration_seconds=600,
            is_short=False, latest_view_count=20000, outlier_multiplier=20.0,
        )
    )
    session.add(
        Video(
            id="shortie", channel_id="UC_test", title="A Short",
            published_at=now - timedelta(days=30), duration_seconds=45,
            is_short=True, latest_view_count=500000, outlier_multiplier=1.0,
        )
    )
    session.commit()

    all_results = client.get("/api/outliers").json()
    assert all_results["total"] > 0

    high = client.get("/api/outliers", params={"min_multiplier": 5}).json()
    ids = [r["video_id"] for r in high["results"]]
    assert "breakout" in ids
    assert "vid000" not in ids

    long_only = client.get("/api/outliers", params={"format": "long"}).json()
    assert all(not r["is_short"] for r in long_only["results"])

    searched = client.get("/api/outliers", params={"search": "breakout"}).json()
    assert [r["video_id"] for r in searched["results"]] == ["breakout"]


def test_shorts_get_their_own_baseline(client, session):
    """Pooling Shorts with long-form would make both baselines meaningless."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(Channel(id="UC_mixed", title="Mixed"))
    for i in range(8):
        session.add(
            Video(
                id=f"long{i}", channel_id="UC_mixed", title=f"Long {i}",
                published_at=now - timedelta(days=40 + i), duration_seconds=600,
                is_short=False, latest_view_count=1_000, outlier_multiplier=1.0,
            )
        )
        session.add(
            Video(
                id=f"shrt{i}", channel_id="UC_mixed", title=f"Short {i}",
                published_at=now - timedelta(days=40 + i), duration_seconds=30,
                is_short=True, latest_view_count=100_000, outlier_multiplier=1.0,
            )
        )
    session.commit()

    body = client.get("/api/outliers").json()
    baselines = body["baselines"]
    assert baselines["UC_mixed:long"]["median_views"] == 1_000
    assert baselines["UC_mixed:short"]["median_views"] == 100_000

    # A typical Short must not read as a 100x outlier just for being a Short.
    shorts = [r for r in body["results"] if r["is_short"]]
    assert all(r["multiplier"] == pytest.approx(1.0, abs=0.01) for r in shorts)


def test_rescore_costs_nothing(client, session):
    session.add(Channel(id="UC_x", title="X", is_tracked=True))
    session.commit()

    body = client.post("/api/outliers/rescore").json()
    assert body["units_spent"] == 0
    assert client.get("/api/quota").json()["used"] == 0


def test_owned_status_without_oauth(client):
    """The My Channel screen must explain itself before anything is connected."""
    body = client.get("/api/owned/status").json()
    assert body["oauth"]["connected"] is False
    assert body["ctr_reporting"]["message"]


def test_diagnosis_explains_why_it_is_empty(client):
    body = client.get("/api/owned/diagnosis").json()
    assert body["results"] == []
    assert body["meta"]["reason"]


def test_watchlist_rejects_a_non_video_reference(client):
    response = client.post("/api/watchlist/videos", json={"reference": "hello world"})
    assert response.status_code == 400
    assert "YouTube video URL" in response.json()["detail"]


def test_jobs_and_changes_are_empty_but_valid(client):
    assert client.get("/api/jobs").json() == []
    assert client.get("/api/changes").json() == []
    assert client.get("/api/watchlist").json() == []


def test_title_comparison_without_an_own_channel(client):
    body = client.get("/api/titles/compare").json()
    assert body["has_own_channel"] is False
    assert body["comparisons"] == []
