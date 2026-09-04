# Channel Lens

A local YouTube strategy workbench. It does what the paid competitor-analysis
tools do — finds videos that beat their channel's normal, tracks what your
competitors change, and breaks down thumbnails and titles — except it runs on
your machine, on your own API key, and shows you the arithmetic.

It also does one thing those tools cannot do at any price: reads your own
channel's real impressions, click-through rate, and retention.

```
run-channel-lens.bat          →  http://localhost:8730
```

## What it does

| Screen | What it answers |
|---|---|
| **Outliers** | Which videos beat what their own channel normally does |
| **Discover** | Is this topic working for anyone, or just for big channels? |
| **Channels** | Who am I tracking, and what has it cost me |
| **Tracker** | What did a competitor change, and did it move the numbers |
| **Packaging** | What do the breakouts have in common, and how do mine differ |
| **My Channel** | Is my CTR or my retention the problem — and which videos are worth repackaging |

## How it works

**Outlier scoring.** For each channel, take the *median* views of its recent
mature uploads; a video's multiplier is its views over that median. Median
rather than mean, because one past viral video would otherwise raise the bar for
months and hide the next breakout. Videos younger than two weeks are scored but
never form the baseline — including them would drag it down and inflate
everything else. Shorts and long-form get separate baselines, since pooling them
produces a number that describes neither.

**Change detection.** Watched videos are snapshotted every few hours. Two
snapshots give a velocity; a run of them shows whether YouTube is still pushing
a video. A changed title or thumbnail URL is recorded with view counts either
side, which is the closest thing to seeing a competitor's A/B test results.
Correlation only, and the app says so every time it reports one.

**Packaging analysis.** Thumbnail colour, contrast, and edge density are
measured locally with Pillow — free, instant, and the metrics that actually
predict whether an image survives being shown at 210×118. With an Anthropic key,
each thumbnail also gets a structured description (faces, expression, text,
legibility). Titles get deterministic feature extraction, then a comparison
between the breakouts' titles and your own.

**Your own channel.** This needs two different Google APIs, which is the
surprising part:

- The **YouTube Analytics API v2** answers on-demand queries — views, watch
  time, average view duration and percentage, traffic sources. It does **not**
  expose impressions or CTR and never has.
- The **YouTube Reporting API** is the only source of thumbnail impressions and
  CTR (`channel_reach_basic_a1`, added January 2026). It's a bulk system: you
  register a job, Google generates a daily CSV, and the first one lands **up to
  48 hours later** with 30 days backfilled.

The headline reading is CTR × retention against your own medians, not industry
benchmarks — what counts as a good CTR depends entirely on your niche and how
much browse traffic you get.

## Quota is the whole design

The YouTube Data API gives you 10,000 units a day, resetting at midnight
US/Pacific. Costs are wildly uneven:

| Call | Cost | Returns |
|---|---|---|
| `videos.list` | **1 unit** | up to 50 videos, fully detailed |
| `playlistItems.list` | **1 unit** | up to 50 uploads |
| `channels.list` | **1 unit** | up to 50 channels |
| `search.list` | **100 units** | up to 50 video *ids* |

Search costs a hundred times what everything else costs and returns less. So
Channel Lens never uses search to answer "what has this channel posted" — it
pages the uploads playlist at 1 unit per 50. Search appears on exactly one
screen, with the price shown before you press it.

Everything fetched is stored permanently in SQLite and never re-fetched.
Responses are cached with per-endpoint TTLs. Every operation estimates its cost
and is refused if the budget can't cover it — so you find out *before* spending,
not from a 403 halfway through.

Concretely: importing 50 uploads from a channel is ~3 units. Tracking 200 videos
costs ~16 units a day. A day's real work is a few hundred units out of 10,000.

## Setup

Python 3.11+. No Node, no build step.

```bash
uv venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/python.exe -m channel_lens
```

Then open Settings. Only the first credential is required:

1. **YouTube Data API key** — free. Google Cloud → new project → enable
   *YouTube Data API v3* → Credentials → API key.
2. **Your channel** — paste a handle, ID, or URL.
3. **Google OAuth client** (for your own CTR) — same project:
   - Enable *YouTube Analytics API* **and** *YouTube Reporting API*. Both are
     needed; CTR comes only from the Reporting one.
   - Configure the OAuth consent screen, user type **External**.
   - Set publishing status to **In production**. In *Testing* mode Google
     expires refresh tokens after 7 days, so the connection would silently die
     every week. Publishing needs no Google verification — you'll see an
     "unverified app" warning once and choose Advanced → Go to Channel Lens.
   - Credentials → OAuth client ID → application type **Desktop app**. That
     type permits the localhost redirect this app uses.
   - Scope requested: `yt-analytics.readonly`, and nothing else. It covers both
     the Analytics and Reporting APIs. Revenue scopes are never requested.
4. **Anthropic API key** (optional) — only for AI thumbnail breakdowns.

The Settings screen carries these steps inline, with a "Test key" button for
each.

## Where things live

Data, settings, credentials, and cached thumbnails go in one folder:

- Windows: `%LOCALAPPDATA%\channel-lens`
- Linux/macOS: `~/.local/share/channel-lens`

Override with `CHANNEL_LENS_HOME`. Nothing leaves your machine except requests
to Google and, if configured, Anthropic.

## Layout

```
src/channel_lens/
  config.py          settings + where state lives
  models.py          SQLAlchemy schema
  db.py              engine, WAL, sessions
  youtube/
    client.py        Data API: batching, caching, retries, typed errors
    quota.py         the unit ledger — checks before spending
    analytics.py     Analytics API v2 + Reporting API (OAuth)
    errors.py        failures phrased for a person, not a log
  services/
    ingest.py        payloads → rows, change detection
    outliers.py      baselines and multipliers
    tracker.py       snapshots, velocity, before/after
    thumbnails.py    local image metrics + Claude vision
    titles.py        deterministic features + comparison
    owned.py         owner analytics sync + CTR×retention diagnosis
  api/               FastAPI routers
  web/static/        the frontend — plain ES modules and CSS
tests/               40 tests, no network
```

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe -m pytest --doctest-modules src/channel_lens -q
```

Every test runs against a temporary `CHANNEL_LENS_HOME`, so a test run can never
touch your real database. Nothing hits the network.

## What this deliberately doesn't do

- **No composite scores.** No "title score: 87/100". A single number hides which
  attribute differs and by how much, which is the only actionable part.
- **No industry benchmarks.** Thresholds come from your own channel's medians.
- **No causal claims.** A thumbnail swap followed by a view increase is reported
  as correlation, because YouTube's promotion decisions move far more traffic
  than any thumbnail.
- **No scraping.** Official APIs only.
