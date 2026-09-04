# Channel Lens

## What this project is

A local YouTube strategy workbench — the thing the paid competitor-analysis
tools (vidIQ, TubeBuddy, 1of10) sell, rebuilt to run on the user's own machine
with their own API key. Outlier detection, competitor change tracking, thumbnail
and title analysis, plus the one thing those tools structurally cannot offer:
the user's own impressions, CTR, and retention.

Started 2026-09-03. Motivation: the user was researching YouTube growth tooling,
found every creator was selling a subscription, and wanted to understand the
mechanism and build it instead.

## Current state

Complete and running end to end, but **not yet exercised against a real API
key** — every test uses fixtures or the local database, and no live YouTube
request has been made. That is the single biggest open item.

- `config.py` / `db.py` / `models.py` — settings, SQLite (WAL), full schema.
- `youtube/` — Data API client with batching, SQLite response cache, retries and
  typed errors; the quota ledger; the Analytics + Reporting API layer for owner
  data.
- `services/` — ingest, outliers, tracker, thumbnails, titles, owned.
- `api/` — FastAPI routers (system, research, owned).
- `web/static/` — no-build frontend: plain ES modules + CSS, 8 routes.
- 40 tests + 26 doctests, all passing. Verified in headless Chrome over CDP:
  every route renders with zero JS errors.

## Non-negotiable design decisions

**1. Quota is checked before spending, never after.** Every operation declares
its estimated cost and is refused if today's budget can't cover it. Discovering
exhaustion from a 403 means the partial work is already paid for. `QuotaLedger`
is handed the same session as the work it books, so a crash can't leave the
ledger optimistic.

**2. Never reach for `search.list` to answer "what has this channel posted".**
It is 100 units per 50 results; `playlistItems.list` on the uploads playlist is
1 unit per 50 and returns more. Search appears on exactly one screen, with the
cost shown before the user commits. This single ratio drives most of the
architecture.

**3. Median, never mean, for baselines.** One viral video would drag a mean
upward for months, raising the bar and hiding the next breakout — precisely when
the user most wants to see it.

**4. Young videos are scored but never form the baseline.** Averaging in a
two-day-old video drags the baseline down and inflates every other multiplier.
Maturity projections are offered separately and always labelled as projections.

**5. Shorts and long-form get separate baselines.** Their view distributions
differ by an order of magnitude; pooling them describes neither. The seed data
confirmed this works — Shorts at 6–11× the long-form median correctly do *not*
appear as outliers.

**6. No composite scores.** No "title score: 87/100", no "video health". A
single number hides which attribute differs and by how much. This matters most
in the CTR × retention quadrant read, where the two failing quadrants need
*opposite* responses.

**7. Thresholds come from the user's own channel, never industry benchmarks.**
What counts as a good CTR depends entirely on niche and browse-traffic share.

**8. Correlation is never reported as causation.** A thumbnail swap followed by
a view increase gets an explicit hedge every time — YouTube's own promotion
decisions move far more traffic than any thumbnail, and creators often swap
*because* performance changed.

**9. Errors carry a message and a hint, both written for the user.** This is a
local single-user app; the person reading the error is the only person who can
fix it. Every `YouTubeError` subclass carries both, and the API layer passes
them straight through to the UI.

## The two-API surprise (owner analytics)

This cost real research time and is easy to get wrong from memory:

- **YouTube Analytics API v2** (`youtubeanalytics.googleapis.com`) — on-demand
  queries. Views, watch time, `averageViewDuration`, `averageViewPercentage`,
  subscribers, traffic sources. **Does not expose impressions or CTR.** There is
  no `impressions` or `impressionsClickThroughRate` metric; the only
  `impressions` in its docs is `adImpressions`.
- **YouTube Reporting API** (`youtubereporting.googleapis.com`) — bulk CSV. The
  `channel_reach_basic_a1` report type (added **15 January 2026**) carries
  `video_thumbnail_impressions` and `video_thumbnail_impressions_ctr`, keyed by
  `date` / `channel_id` / `video_id`. Register a job, Google generates dailies;
  **first report up to 48 hours later**, 30 days backfilled, 60-day retention.

Both use `yt-analytics.readonly`. Neither spends Data API quota. The 48-hour
wait is Google's, and the UI states it plainly — without that, the screen looks
broken for two days.

## Conventions

- Ingestion is idempotent: re-running updates metadata in place and adds at most
  one snapshot. The tracker, a manual refresh, and an outlier scan can all touch
  the same video within a minute.
- `VideoStat` is append-only. Snapshots are what make velocity answerable at all
  and YouTube serves no historical view counts, so they are irreplaceable.
- Thumbnail analyses key on `(video_id, thumbnail_url)`. YouTube mints a new URL
  per image, so a cached analysis can never be stale.
- Title analyses key on `(video_id, title)`, so a retitle produces a new row and
  the old analysis stays attached to the old title.
- Vision and LLM fields are nullable; every screen must work without an
  Anthropic key.
- Frontend builds DOM through `el()` (textContent/properties), never template
  strings — video titles are arbitrary untrusted text.

## Chart rules (followed deliberately)

Colours come from a pre-validated data-viz palette, used verbatim rather than
picked by eye. Status colours (good/warning/serious/critical) are reserved for
things that genuinely mean good or bad — the quadrant plot uses them because
those *are* states; multiplier badges do **not**, because a magnitude is not a
status and the number is already printed. Scatter plots cap at three categorical
hues. 2px lines, 10% area washes, markers ≥8px with a 2px surface ring, bars
≤24px with a rounded data-end and square baseline, hairline *solid* gridlines.
Every chart has a table-view twin.

## Machine notes

`VIRTUAL_ENV` is exported globally in this machine's PowerShell profile, so
`uv pip install` targets the wrong venv unless `--python
.venv\Scripts\python.exe` is passed. This already polluted `mp4-compressor`'s
venv once.

Node is not installed — hence the no-build frontend, and hence headless Chrome
over CDP (via the `websockets` package uvicorn already pulls in) for browser
verification rather than Playwright.

## Next steps

1. **Run it against a real API key.** Every code path is fixture-tested; none
   has met the live API. Expect the first surprises in `resolve_channel`
   (`forHandle` support varies) and in the exact error `reason` strings.
2. **Confirm `channel_reach_basic_a1` end to end** — the column names come from
   documentation, not an observed CSV. `parse_reach_rows` is the place to adjust
   if they differ.
3. **Empirical maturity curves are unproven.** `empirical_maturity_curve` needs
   ~40 snapshots on one channel before it replaces the generic curve; nothing
   has that much history yet.
4. **Retune `BUSY_EDGE_DENSITY` and `LOW_CONTRAST`** in `services/thumbnails.py`
   against real thumbnails — the current values are reasoned, not calibrated.
5. Consider a "compare two channels head to head" view; the data supports it and
   nothing exposes it yet.
