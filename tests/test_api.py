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


def test_only_the_youtube_key_is_actually_required(client):
    """The paid Anthropic key must never be presented as a blocker.

    It was, which made the Packaging screen look like it needed a paid API key
    to work at all. It does not: every title metric and the local thumbnail
    measurements run without one.
    """
    body = client.get("/api/status").json()
    gaps = {gap["key"]: gap for gap in body["missing"]}

    assert gaps["youtube_api_key"]["required"] is True
    assert gaps["anthropic_api_key"]["required"] is False
    assert gaps["google_oauth"]["required"] is False
    assert gaps["owned_channel_id"]["required"] is False

    # The sidebar badge counts blockers only.
    assert body["required_missing"] == 1

    # And the one paid item says so, in the text the user actually reads.
    assert gaps["anthropic_api_key"]["cost"] == "paid"
    assert all(g["cost"] == "free" for k, g in gaps.items() if k != "anthropic_api_key")
    assert "without it" in gaps["anthropic_api_key"]["fix"]


def test_configuring_the_youtube_key_clears_the_setup_badge(client):
    client.put("/api/settings", json={"youtube_api_key": "AIzaTESTKEY123"})
    body = client.get("/api/status").json()

    assert body["required_missing"] == 0
    assert body["ready"] is True
    # Optional suggestions remain listed, just not as blockers.
    assert body["missing"]


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


def test_outliers_sort_by_every_column_in_both_directions(client, session):
    """Each column header has to actually reorder the whole result set."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(Channel(id="UC_a", title="Alpha Channel"))
    session.add(Channel(id="UC_b", title="Zulu Channel"))
    for i in range(8):
        for cid in ("UC_a", "UC_b"):
            session.add(Video(
                id=f"{cid}_v{i}", channel_id=cid, title=f"{cid} video {i}",
                published_at=now - timedelta(days=30 + i * 3),
                duration_seconds=600, is_short=False,
                latest_view_count=1000 * (i + 1), outlier_multiplier=1.0,
            ))
    session.commit()

    def ordered(sort, direction):
        body = client.get("/api/outliers", params={
            "sort": sort, "direction": direction, "min_multiplier": 0,
        }).json()
        return body["results"]

    for column, key in [
        ("views", lambda r: r["views"]),
        ("multiplier", lambda r: r["multiplier"]),
        ("baseline", lambda r: r["baseline_views"]),
        ("percentile", lambda r: r["percentile"]),
        ("title", lambda r: r["title"].lower()),
        ("recent", lambda r: r["published_at"]),
    ]:
        desc = [key(r) for r in ordered(column, "desc")]
        asc = [key(r) for r in ordered(column, "asc")]

        assert desc == sorted(desc, reverse=True), f"{column} desc"
        assert asc == sorted(asc), f"{column} asc"
        assert desc[0] == asc[-1], f"{column} directions disagree"


def test_summary_figures_ignore_the_sort_and_the_limit(client, session):
    """'Top multiplier' must mean the highest that matched, not row one.

    Shipped broken the moment sorting became configurable: the tile read
    results[0], so sorting by title reported that video's multiplier as the
    channel's best.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(Channel(id="UC_s", title="Summary"))
    for i in range(9):
        session.add(Video(
            id=f"sum{i}", channel_id="UC_s", title=f"zzz video {i}",
            published_at=now - timedelta(days=30 + i),
            duration_seconds=600, is_short=False,
            latest_view_count=1000, outlier_multiplier=1.0,
        ))
    session.add(Video(
        id="aaa_big", channel_id="UC_s", title="aaa the big one",
        published_at=now - timedelta(days=25), duration_seconds=600,
        is_short=False, latest_view_count=20_000, outlier_multiplier=20.0,
    ))
    session.commit()

    by_multiplier = client.get("/api/outliers", params={"min_multiplier": 0}).json()
    top = by_multiplier["max_multiplier"]
    assert top == pytest.approx(20.0)

    # Sorting by title puts the 1x video first; the summary must not follow it.
    by_title = client.get("/api/outliers", params={
        "sort": "title", "direction": "asc", "min_multiplier": 0,
    }).json()

    assert by_title["results"][0]["video_id"] == "aaa_big"
    assert by_title["max_multiplier"] == pytest.approx(top)
    assert by_title["outlier_count"] == by_multiplier["outlier_count"]
    assert by_title["channels_matched"] == 1

    # And a truncated page still reports the whole set's figures.
    limited = client.get("/api/outliers", params={
        "sort": "title", "direction": "desc", "limit": 2, "min_multiplier": 0,
    }).json()
    assert len(limited["results"]) == 2
    assert limited["max_multiplier"] == pytest.approx(top)


def test_sorting_applies_before_the_limit(client, session):
    """Sorting only the visible page would quietly show the wrong rows.

    'Lowest views' must mean the lowest of everything matching, not the lowest
    of whichever page happened to rank highest under the previous sort.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(Channel(id="UC_many", title="Many"))
    for i in range(30):
        session.add(Video(
            id=f"many{i:03d}", channel_id="UC_many", title=f"Video {i}",
            published_at=now - timedelta(days=40 + i),
            duration_seconds=600, is_short=False,
            latest_view_count=100 * (i + 1), outlier_multiplier=1.0,
        ))
    session.commit()

    page = client.get("/api/outliers", params={
        "sort": "views", "direction": "asc", "limit": 5, "min_multiplier": 0,
    }).json()

    assert page["total"] == 30
    assert len(page["results"]) == 5
    # The genuinely smallest video in the set, not the smallest of a top slice.
    assert page["results"][0]["views"] == 100


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


# --------------------------------------------------------------- bulk import


def _stub_youtube(monkeypatch, *, explode_on: str = ""):
    """Stand in for the Data API so bulk import can be exercised offline.

    ``explode_on`` makes one channel raise during the database flush, which is
    the failure that used to poison the session and kill the rest of the batch.
    """
    from channel_lens.youtube import client as yt
    from channel_lens.youtube.errors import NotFound

    def resolve(self, reference):
        if "missing" in reference:
            raise NotFound("No channel matches that reference.", "Check the handle.")
        slug = reference.strip("@/").split("/")[-1][:18] or "x"
        return {
            "id": ("UC" + slug).ljust(24, "0")[:24],
            "snippet": {"title": f"Channel {slug}", "customUrl": f"@{slug}",
                        "thumbnails": {"high": {"url": "https://x/t.jpg"}}},
            "statistics": {"subscriberCount": "1000", "videoCount": "10",
                           "viewCount": "50000"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + slug}},
        }

    def uploads(self, playlist_id, *, limit=50, published_after=None):
        self.ledger.record(self.session, "playlistItems.list")
        return [f"{playlist_id}-v{i}" for i in range(2)]

    def videos(self, video_ids):
        self.ledger.record(self.session, "videos.list")
        out = []
        for vid in video_ids:
            slug = vid.split("-v")[0][2:]
            # A channel_id with no matching row violates the foreign key at flush.
            channel_id = "UC_nonexistent" if (explode_on and slug == explode_on) \
                else ("UC" + slug).ljust(24, "0")[:24]
            out.append({
                "id": vid[:32],
                "snippet": {"title": f"Video {vid}", "channelId": channel_id,
                            "publishedAt": "2026-01-01T00:00:00Z",
                            "thumbnails": {"high": {"url": "https://x/v.jpg"}},
                            "tags": []},
                "statistics": {"viewCount": "1000", "likeCount": "10",
                               "commentCount": "1"},
                "contentDetails": {"duration": "PT10M"},
            })
        return out

    monkeypatch.setattr(yt.YouTubeClient, "resolve_channel", resolve)
    monkeypatch.setattr(yt.YouTubeClient, "list_upload_video_ids", uploads)
    monkeypatch.setattr(yt.YouTubeClient, "get_videos", videos)


def test_bulk_preview_spends_no_quota(client):
    """Pricing an import must never itself cost anything."""
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    body = client.post("/api/channels/bulk/preview", json={
        "text": "@one\n- @two\n3. @three\n@one", "video_limit": 50,
    }).json()

    assert body["parsed"] == 3          # duplicate dropped
    assert body["to_import"] == 3
    assert body["estimated_units"] == 3 * body["per_channel_units"]
    assert client.get("/api/quota").json()["used"] == 0


def _run_bulk(client, **payload):
    """Start a bulk import and wait for the background job to finish.

    The endpoint returns as soon as the job is queued, so tests have to wait on
    it the same way the UI does.
    """
    import time

    started = client.post("/api/channels/bulk", json=payload)
    assert started.status_code == 202, started.json()
    job_id = started.json()["job_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/jobs/live/{job_id}").json()
        if not job["running"]:
            assert job["status"] != "error", job.get("error")
            return job["result"], job
        time.sleep(0.05)
    raise AssertionError("bulk import did not finish in time")


def test_bulk_import_isolates_a_failing_channel(client, monkeypatch):
    """The whole point: one bad row must not take the batch down with it.

    'boom' fails during the database flush. Without a per-channel SAVEPOINT the
    session is left unusable and every channel after it fails too — which is
    exactly the bug this covers.
    """
    _stub_youtube(monkeypatch, explode_on="boom")
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    body, _job = _run_bulk(client, text="@first\n@boom\n@missing\n@last", video_limit=50)

    by_ref = {r["reference"]: r for r in body["results"]}
    assert by_ref["@first"]["status"] == "imported"
    assert by_ref["@boom"]["status"] == "failed"
    assert by_ref["@missing"]["status"] == "failed"
    # The one that proves isolation: it comes after two failures.
    assert by_ref["@last"]["status"] == "imported"

    assert body["imported"] == 2
    assert body["failed"] == 2

    # And the successful ones are genuinely persisted.
    titles = {c["title"] for c in client.get("/api/channels").json()}
    assert "Channel first" in titles
    assert "Channel last" in titles


def test_bulk_import_reports_a_bad_reference_without_raw_sql(client, monkeypatch):
    """SQLAlchemy embeds the whole statement in str(exc); a table cell can't use it."""
    _stub_youtube(monkeypatch, explode_on="boom")
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    body, _job = _run_bulk(client, text="@boom")
    message = body["results"][0]["message"]

    assert "[SQL:" not in message
    assert "\n" not in message
    assert len(message) <= 200


def test_bulk_import_skips_already_tracked_channels(client, monkeypatch):
    _stub_youtube(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    first, _ = _run_bulk(client, text="@alpha\n@beta")
    assert first["imported"] == 2

    again, _ = _run_bulk(client, text="@alpha\n@beta")
    assert again["imported"] == 0
    assert again["skipped"] == 2
    # Re-running a list must not re-pay for what is already stored.
    assert again["units_spent"] <= 2


def test_bulk_import_refuses_an_unaffordable_batch(client, monkeypatch):
    """The per-operation cap has to apply to the batch, not per channel."""
    _stub_youtube(monkeypatch)
    client.put("/api/settings", json={
        "youtube_api_key": "AIzaTEST", "per_operation_quota_cap": 10,
    })

    references = "\n".join(f"@ch{i}" for i in range(40))
    response = client.post("/api/channels/bulk", json={
        "text": references, "video_limit": 500,
    })

    assert response.status_code == 413
    body = response.json()
    assert "cap" in body["message"] or "cap" in body["hint"]
    # Nothing may be spent by a refused operation.
    assert client.get("/api/quota").json()["used"] == 0


def test_bulk_import_rejects_empty_text(client):
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})
    response = client.post("/api/channels/bulk", json={"text": "   \n\n  "})
    assert response.status_code == 400


def test_bulk_import_returns_immediately_with_a_job_to_poll(client, monkeypatch):
    """The request must not stay open for the minutes a real import takes."""
    _stub_youtube(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    response = client.post("/api/channels/bulk", json={"text": "@a\n@b\n@c"})

    assert response.status_code == 202
    body = response.json()
    assert body["total"] == 3
    assert body["job_id"]

    job = client.get(f"/api/jobs/live/{body['job_id']}").json()
    assert job["total"] == 3
    assert "fraction" in job and "done" in job


def test_import_progress_reaches_completion(client, monkeypatch):
    _stub_youtube(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    result, job = _run_bulk(client, text="@a\n@b\n@c")

    assert job["done"] == job["total"] == 3
    assert job["fraction"] == 1.0
    assert job["running"] is False
    assert result["imported"] == 3


def test_a_running_import_can_be_found_again(client, monkeypatch):
    """A page reopened mid-import has to be able to re-attach to the job."""
    _stub_youtube(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    started = client.post("/api/channels/bulk", json={"text": "@a\n@b"}).json()
    live = client.get("/api/jobs/live", params={"name": "channel.bulk_add"}).json()

    # Either still running and discoverable, or already done — both are fine;
    # what must not happen is the job being invisible while it runs.
    assert live.get("id") == started["job_id"] or live["running"] is False


def test_only_one_import_runs_at_a_time(client, monkeypatch):
    """Two concurrent imports would race on the same quota ledger."""
    import time
    from channel_lens.youtube import client as yt

    _stub_youtube(monkeypatch)
    original = yt.YouTubeClient.resolve_channel

    def slow_resolve(self, reference):
        time.sleep(0.3)
        return original(self, reference)

    monkeypatch.setattr(yt.YouTubeClient, "resolve_channel", slow_resolve)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    first = client.post("/api/channels/bulk", json={"text": "@a\n@b\n@c"})
    assert first.status_code == 202

    second = client.post("/api/channels/bulk", json={"text": "@d"})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]


def test_polling_an_unknown_job_explains_itself(client):
    response = client.get("/api/jobs/live/nosuchjob")
    assert response.status_code == 404
    assert response.json()["hint"]


# ------------------------------------------------- videos from unknown channels


def _stub_search(monkeypatch, *, unfetchable_channel: str = ""):
    """Stub search returning videos from channels not yet in the database.

    That is the situation search uniquely creates: every other path walks a
    channel's own uploads, so the channel row exists by construction.
    """
    from channel_lens.youtube import client as yt

    def search(self, query, **kwargs):
        self.ledger.record(self.session, "search.list")
        return ["vidAAAAAAAA", "vidBBBBBBBB"]

    def get_videos(self, video_ids):
        self.ledger.record(self.session, "videos.list")
        mapping = {"vidAAAAAAAA": "UCknown0000000000000000",
                   "vidBBBBBBBB": unfetchable_channel or "UCknown0000000000000000"}
        return [{
            "id": vid,
            "snippet": {"title": f"Video {vid}", "channelId": mapping[vid],
                        "publishedAt": "2026-08-01T00:00:00Z",
                        "thumbnails": {"high": {"url": "https://x/v.jpg"}},
                        "tags": [], "categoryId": "20"},
            "statistics": {"viewCount": "1193730", "likeCount": "27580",
                           "commentCount": "2335"},
            "contentDetails": {"duration": "PT8M23S"},
        } for vid in video_ids]

    def get_channels(self, channel_ids):
        self.ledger.record(self.session, "channels.list")
        # A suspended or deleted channel simply isn't returned by YouTube.
        return [{
            "id": cid,
            "snippet": {"title": f"Channel {cid[:8]}", "customUrl": "@x",
                        "thumbnails": {"high": {"url": "https://x/t.jpg"}}},
            "statistics": {"subscriberCount": "500000", "videoCount": "900",
                           "viewCount": "1000000"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}},
        } for cid in channel_ids if cid != unfetchable_channel]

    monkeypatch.setattr(yt.YouTubeClient, "search", search)
    monkeypatch.setattr(yt.YouTubeClient, "get_videos", get_videos)
    monkeypatch.setattr(yt.YouTubeClient, "get_channels", get_channels)


def test_search_stores_videos_from_channels_not_yet_known(client, monkeypatch):
    """videos.channel_id is a foreign key, so the channel row must exist first.

    Shipped broken: search inserted videos before fetching their channels and
    died with 'FOREIGN KEY constraint failed' on any channel not already
    tracked — which is most of what a search returns.
    """
    _stub_search(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    response = client.post("/api/discover", json={"query": "dungeons and dragons"})
    assert response.status_code == 200

    # The channel was created as a side effect of storing its videos.
    stored = {c["id"] for c in client.get("/api/channels").json()}
    assert "UCknown0000000000000000" in stored

    detail = client.get("/api/videos/vidAAAAAAAA")
    assert detail.status_code == 200
    assert detail.json()["video"]["views"] == 1193730


def test_a_video_whose_channel_cannot_be_fetched_is_skipped_not_fatal(client, monkeypatch):
    """A deleted or suspended channel must not take the whole search down."""
    _stub_search(monkeypatch, unfetchable_channel="UCgone000000000000000000")
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    response = client.post("/api/discover", json={"query": "anything"})
    assert response.status_code == 200

    # The good video survived; the orphan was dropped rather than crashing.
    assert client.get("/api/videos/vidAAAAAAAA").status_code == 200
    assert client.get("/api/videos/vidBBBBBBBB").status_code == 404


def test_watchlist_add_works_for_an_untracked_channel(client, monkeypatch):
    """Same foreign-key ordering bug lived in the watch-a-video path."""
    _stub_search(monkeypatch)
    client.put("/api/settings", json={"youtube_api_key": "AIzaTEST"})

    response = client.post("/api/watchlist/videos",
                           json={"reference": "https://youtu.be/vidAAAAAAAA"})
    assert response.status_code == 200, response.json()
    assert response.json()["video_id"] == "vidAAAAAAAA"
