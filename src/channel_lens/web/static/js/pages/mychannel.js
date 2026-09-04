/* My Channel — the only screen built on measurements rather than inference.
 *
 * Everything else in the app reasons from public numbers. This reads
 * impressions, click-through rate, and retention straight from YouTube, which
 * only the channel owner can see.
 *
 * The headline is the CTR × retention cross-read, because the two failing
 * quadrants need opposite responses: low CTR with high retention is a
 * packaging problem worth fixing, while high CTR with low retention means the
 * packaging is already outrunning the video. A single "health score" would
 * average those into something useless.
 */

import { api } from '../api.js';
import { refreshStatus, state } from '../app.js';
import {
  el, clear, compact, full, pct, ago, empty, notice, loading, toast, toastError,
  badge, withBusy, videoCell,
} from '../ui.js';
import { quadrantChart, barChart, withTableView } from '../charts.js';

const QUADRANT_KIND = {
  working: 'good', packaging: 'warning', overpromise: 'serious',
  topic: 'critical', unknown: '',
};

let quadrantFilter = '';
let days = 90;

export async function render(view) {
  const body = el('div', { class: 'stack' });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'My Channel' }),
      el('p', { class: 'lede', text:
        'Your real impressions, click-through rate and retention. No third-party tool ' +
        'can show you these at any price — they are visible only to the channel owner, ' +
        'through an authenticated request.' }))));

  view.append(body);

  // Only the body is re-rendered on reload; the page header stays put, so a
  // sync doesn't make the whole screen jump.
  async function load() {
    clear(body);
    body.append(loading());
    try {
      const status = await api.get('/api/owned/status');
      clear(body);

      if (!status.channel_id) { body.append(needsChannel()); return; }
      if (!status.oauth.connected) { body.append(needsOauth(status)); return; }

      body.append(...await connectedView(status, load));
    } catch (err) {
      clear(body);
      body.append(notice('critical', 'Could not load your channel', err.message, err.hint));
      toastError(err);
    }
  }

  await load();
}

/* ------------------------------------------------------------ gate views */

function needsChannel() {
  return el('div', { class: 'card' }, el('div', { class: 'card-body' },
    empty({
      title: 'Tell Channel Lens which channel is yours',
      message: 'Once linked, the app can separate your videos from competitors\' ' +
               'everywhere, and compare your packaging against what wins in your niche.',
      icon: 'key',
      actions: [{ label: 'Open settings', variant: 'primary',
        onClick: () => { window.location.hash = '#/settings'; } }],
    })));
}

function needsOauth(status) {
  return el('div', { class: 'stack' },
    el('div', { class: 'card' }, el('div', { class: 'card-body' },
      empty({
        title: 'Connect your Google account',
        message: 'Impressions, click-through rate and retention require an authenticated ' +
                 'connection to your own channel. Read-only, and revenue data is never ' +
                 'requested.',
        icon: 'key',
        actions: [{ label: 'Set up the connection', variant: 'primary',
          onClick: () => { window.location.hash = '#/settings'; } }],
      }))),
    notice('info', 'Why this is worth the setup',
      'Every competitor-analysis tool infers from public view counts. None of them can ' +
      'see how many times your thumbnail was shown, or what fraction of those people ' +
      'clicked. That number is the difference between "nobody wants this topic" and ' +
      '"the thumbnail is losing a fight it could win".'));
}

/* -------------------------------------------------------- connected view */

async function connectedView(status, reload) {
  const nodes = [];

  const syncBtn = el('button', { class: 'btn primary' }, 'Sync analytics');
  syncBtn.onclick = () => withBusy(syncBtn, 'Syncing…', async () => {
    const result = await api.post('/api/owned/sync', null, { params: { days } });
    toast(`Updated ${result.analytics.videos_updated} videos` +
          `${result.ctr.imported ? `, imported ${result.ctr.imported} CTR rows` : ''}.`,
      { kind: 'good', hint: result.note });
    await reload();
  });

  nodes.push(el('div', { class: 'between' },
    el('div', { class: 'row' },
      status.channel?.thumbnail_url
        ? el('img', { class: 'avatar', src: status.channel.thumbnail_url, alt: '' }) : null,
      el('div', {},
        el('h2', { text: status.channel?.title || status.channel_id }),
        el('div', { class: 'small muted', text: status.channel
          ? `${compact(status.channel.subscriber_count)} subscribers · ${full(status.channel.video_count)} videos`
          : '' }))),
    syncBtn));

  /* CTR reporting state — the 48-hour wait needs saying out loud. */
  const reach = status.ctr_reporting || {};
  if (!reach.job_exists) {
    const enable = el('button', { class: 'btn primary sm' }, 'Enable CTR reporting');
    enable.onclick = () => withBusy(enable, 'Requesting…', async () => {
      const result = await api.post('/api/owned/ctr/enable');
      toast(result.message, { kind: 'good', title: 'Requested', timeout: 11000 });
      await reload();
    });
    nodes.push(el('div', { class: 'card' }, el('div', { class: 'card-body' },
      el('div', { class: 'between' },
        el('div', {},
          el('h3', { class: 'mb-sm', text: 'Click-through rate is not switched on yet' }),
          el('p', { class: 'small dim', style: { maxWidth: '62ch' }, text:
            'Google delivers thumbnail impressions and CTR as a daily bulk report rather ' +
            'than an on-demand query, so it has to be requested once. The first report ' +
            'arrives within 48 hours and backfills the previous 30 days.' })),
        enable))));
  } else if (!reach.reports_available) {
    nodes.push(notice('info', 'Waiting on Google', reach.message));
  }

  /* Diagnosis */
  const data = await api.get('/api/owned/diagnosis', { days, quadrant: quadrantFilter || undefined });

  if (data.meta.reason) {
    nodes.push(notice('warning', 'The quadrant read is not ready yet', data.meta.reason));
  } else {
    const counts = data.counts || {};
    nodes.push(el('div', { class: 'grid cols-4' },
      quadStat('packaging', 'Packaging holding them back', counts.packaging || 0,
        'Clicks are the bottleneck — the video already keeps people'),
      quadStat('working', 'Working', counts.working || 0, 'The formula to repeat'),
      quadStat('overpromise', 'Overpromising', counts.overpromise || 0,
        'Packaging outruns the video'),
      quadStat('topic', 'Topic or execution', counts.topic || 0,
        'Repackaging is unlikely to rescue these')));

    const chart = quadrantChart(data.results, {
      medianCtr: data.meta.median_ctr,
      medianRetention: data.meta.median_retention,
    });
    const { table, toggle } = withTableView(chart, {
      columns: [
        { label: 'Video', get: (r) => r.title },
        { label: 'CTR', num: true, get: (r) => pct(r.ctr) },
        { label: 'Retention', num: true, get: (r) => pct(r.average_view_percentage, 0) },
        { label: 'Impressions', num: true, get: (r) => full(r.impressions) },
        { label: 'Read', get: (r) => r.headline },
      ],
      rows: data.results.filter((r) => r.ctr !== null),
    });

    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Click-through rate against retention' }),
          el('div', { class: 'sub', text:
            `Split at your own medians — CTR ${data.meta.median_ctr}%, retention ` +
            `${data.meta.median_retention}% — across ${data.meta.sample_size} videos. ` +
            `Industry benchmarks would be meaningless here; what counts as good depends ` +
            `entirely on your niche and how much browse traffic you get.` })),
        toggle),
      el('div', { class: 'card-body' }, chart, table)));

    /* The actionable list */
    const opportunities = data.results.filter((r) => r.quadrant === 'packaging' && r.upside_views);
    if (opportunities.length) {
      nodes.push(el('div', { class: 'card' },
        el('div', { class: 'card-head' },
          el('div', {}, el('h2', { text: 'Biggest recoverable upside' }),
            el('div', { class: 'sub', text:
              'Videos people stay for but don\'t click. The estimate is what the ' +
              'impressions they already have would have produced at your median CTR.' }))),
        el('div', { class: 'table-wrap' }, el('table', {},
          el('thead', {}, el('tr', {},
            el('th', { text: 'Video' }),
            el('th', { class: 'num', text: 'CTR' }),
            el('th', { class: 'num', text: 'Retention' }),
            el('th', { class: 'num', text: 'Impressions' }),
            el('th', { class: 'num', text: 'Missed views' }))),
          el('tbody', {}, opportunities.slice(0, 15).map((r) => el('tr', {},
            el('td', {}, videoCell(r, { size: 'sm' })),
            el('td', { class: 'num tnum', text: pct(r.ctr) }),
            el('td', { class: 'num tnum', text: pct(r.average_view_percentage, 0) }),
            el('td', { class: 'num tnum muted', text: compact(r.impressions) }),
            el('td', { class: 'num tnum', text: `+${compact(r.upside_views)}` }))))))));
    }

    /* Per-video readings */
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Every video, read individually' }))),
      el('div', { class: 'card-body tight' },
        data.results.slice(0, 40).map((r) => el('div', { class: 'diagnosis-row' },
          el('div', { class: 'row' },
            badge(data.labels[r.quadrant] || r.quadrant, QUADRANT_KIND[r.quadrant],
              r.quadrant === 'working' ? 'check' : r.quadrant === 'unknown' ? '' : 'alert')),
          videoCell(r, { size: 'sm' }),
          el('p', { class: 'small dim', text: r.detail }),
          r.caveats?.length
            ? el('p', { class: 'small muted', text: r.caveats.join(' ') }) : null)))));
  }

  /* Traffic sources */
  try {
    const traffic = await api.get('/api/owned/traffic', { days });
    if (traffic.sources?.length) {
      const chart = barChart(
        traffic.sources.slice(0, 10).map((s) => ({
          label: prettySource(s.source), value: s.views, highlight: s.packaging_sensitive,
        })),
        { highlightLabel: 'Packaging competes here' });

      nodes.push(el('div', { class: 'card' },
        el('div', { class: 'card-head' },
          el('div', {}, el('h2', { text: 'Where your views come from' }),
            el('div', { class: 'sub', text: traffic.reading }))),
        el('div', { class: 'card-body' }, chart)));
    }
  } catch (err) {
    if (!err.isAuth) nodes.push(notice('warning', 'Traffic sources unavailable', err.message));
  }

  return nodes;
}

function quadStat(key, label, count, note) {
  const tile = el('div', { class: `stat quad-stat ${quadrantFilter === key ? 'on' : ''}` },
    el('div', { class: 'row' },
      el('span', { class: `quad-dot ${key}` }),
      el('div', { class: 'stat-label', text: label })),
    el('div', { class: 'stat-value', text: String(count) }),
    el('div', { class: 'stat-note', text: note }));
  return tile;
}

/** YouTube's SCREAMING_SNAKE source names are unreadable in a chart. */
function prettySource(source) {
  return {
    YT_SEARCH: 'YouTube search', SUGGESTED: 'Suggested videos', BROWSE: 'Browse / home',
    RELATED_VIDEO: 'Related video', EXT_URL: 'External sites', NO_LINK_OTHER: 'Direct or unknown',
    PLAYLIST: 'Playlists', YT_CHANNEL: 'Channel page', NOTIFICATION: 'Notifications',
    SUBSCRIBER: 'Subscriptions feed', YT_OTHER_PAGE: 'Other YouTube pages',
    END_SCREEN: 'End screens', ANNOTATION: 'Cards and annotations', SHORTS: 'Shorts feed',
    HASHTAGS: 'Hashtags', SOUND_PAGE: 'Sound page',
  }[source] || source.replace(/_/g, ' ').toLowerCase();
}
