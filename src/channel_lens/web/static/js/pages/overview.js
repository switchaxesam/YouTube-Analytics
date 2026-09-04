/* Overview — the landing view.
 *
 * On a fresh install this is a setup checklist; once configured it becomes a
 * digest. It deliberately shows nothing it can't back up: with no data, it says
 * so and points at the action that fixes it, rather than rendering empty charts.
 */

import { api } from '../api.js';
import { refreshChannels, refreshStatus, state } from '../app.js';
import {
  el, clear, compact, full, ago, empty, notice, loading, badge,
  multiplierBadge, videoCell, toastError,
} from '../ui.js';

export async function render(view) {
  const status = state.status || await refreshStatus();
  await refreshChannels();

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Overview' }),
      el('p', { class: 'lede', text:
        'What changed in your niche, and what your own numbers say about it.' }))));

  // The checklist and the digest are not exclusive. Anything already fetched
  // stays readable without an API key — it cost quota once and hiding it would
  // punish the user for a missing credential they may be about to add.
  if (!status.ready) view.append(setupChecklist(status));

  const hasData = state.channels.some((c) => c.stored_videos > 0);
  if (!status.ready && !hasData) return;

  const body = el('div', { class: 'stack mt-md' });
  view.append(body);
  body.append(loading());

  try {
    const [outliers, changes, jobs] = await Promise.all([
      api.get('/api/outliers', { min_multiplier: 2, limit: 8, sort: 'multiplier' }),
      api.get('/api/changes', { days: 14, limit: 6 }),
      api.get('/api/jobs', { limit: 5 }),
    ]);
    clear(body);
    body.append(...digest(status, outliers, changes, jobs));
  } catch (err) {
    clear(body);
    body.append(notice('critical', 'Could not load the overview', err.message, err.hint));
    toastError(err);
  }
}

/* ------------------------------------------------------------- checklist */

function setupChecklist(status) {
  const steps = [
    { key: 'youtube_api_key', label: 'Add a YouTube Data API key',
      done: status.settings.youtube_api_key_set,
      detail: 'Free, and unlocks everything that reads public YouTube data.' },
    { key: 'channels', label: 'Add a few competitor channels',
      done: state.channels.filter((c) => !c.is_owned).length > 0,
      detail: 'Three to five in your niche is enough to start.' },
    { key: 'owned_channel_id', label: 'Link your own channel',
      done: !!status.settings.owned_channel_id,
      detail: 'Lets the app compare your titles and thumbnails against what wins.' },
    { key: 'oauth', label: 'Connect Google for your real CTR',
      done: status.oauth?.connected,
      detail: 'Impressions, click-through rate and retention — visible only to you.' },
  ];

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Getting started' }),
        el('div', { class: 'sub', text: 'Four steps, and only the first is required' }))),
    el('div', { class: 'card-body stack' },
      el('p', { class: 'help', text:
        'Channel Lens works the way the paid tools do — it reads public YouTube data, ' +
        'works out what each channel\'s normal looks like, and finds what beat it. The ' +
        'difference is that it runs on your machine, on your own API key, and shows you ' +
        'the arithmetic.' }),
      el('ol', { class: 'checklist' },
        steps.map((step) => el('li', { class: step.done ? 'done' : '' },
          el('span', { class: 'tick' }, step.done ? '✓' : ''),
          el('div', {},
            el('div', { class: 'label', text: step.label }),
            el('div', { class: 'small muted', text: step.detail })))))
      ,
      el('div', { class: 'row' },
        el('button', { class: 'btn primary',
          onClick: () => { window.location.hash = '#/settings'; } }, 'Open settings'),
        el('button', { class: 'btn',
          onClick: () => { window.location.hash = '#/channels'; } }, 'Add channels'))));
}

/* ---------------------------------------------------------------- digest */

function digest(status, outliers, changes, jobs) {
  const nodes = [];
  const tracked = state.channels.filter((c) => !c.is_owned);
  const storedVideos = state.channels.reduce((sum, c) => sum + (c.stored_videos || 0), 0);

  nodes.push(el('div', { class: 'grid cols-4' },
    stat('Channels tracked', full(tracked.length)),
    stat('Videos stored', full(storedVideos), 'Fetched once, kept forever'),
    stat('Outliers found', full(outliers.total), `at ${outliers.threshold}× and above`),
    stat('Quota used today', `${status.quota.fraction_used * 100 < 1 ? '<1' : Math.round(status.quota.fraction_used * 100)}%`,
      `${full(status.quota.remaining)} units left`)));

  if (!tracked.length) {
    nodes.push(el('div', { class: 'card' }, el('div', { class: 'card-body' },
      empty({
        title: 'No competitors tracked yet',
        message: 'Outlier detection needs channels to compare against. Add a few from your niche.',
        actions: [{ label: 'Add channels', variant: 'primary',
          onClick: () => { window.location.hash = '#/channels'; } }],
      }))));
    return nodes;
  }

  /* Top outliers */
  nodes.push(el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Biggest breakouts' }),
        el('div', { class: 'sub', text: 'Videos furthest above their own channel\'s normal' })),
      el('button', { class: 'btn ghost sm',
        onClick: () => { window.location.hash = '#/outliers'; } }, 'See all')),
    outliers.results.length
      ? el('div', { class: 'table-wrap' }, el('table', {},
          el('tbody', {}, outliers.results.map((r) => el('tr', {},
            el('td', {}, videoCell(r, { size: 'sm' })),
            el('td', { class: 'num' }, multiplierBadge(r.multiplier, outliers.threshold)),
            el('td', { class: 'num tnum nowrap', text: compact(r.views) }))))))
      : el('div', { class: 'card-body' },
          el('p', { class: 'muted small', text:
            'Nothing above 2× yet. Either the channels you track are consistent, or ' +
            'there is not enough history stored for a stable baseline — try importing ' +
            'more uploads per channel.' }))));

  /* Packaging changes */
  nodes.push(el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Recent packaging changes' }),
        el('div', { class: 'sub', text: 'Title and thumbnail swaps caught in the last 14 days' })),
      el('button', { class: 'btn ghost sm',
        onClick: () => { window.location.hash = '#/tracker'; } }, 'Open tracker')),
    changes.length
      ? el('div', { class: 'card-body tight' },
          changes.map((c) => el('div', { class: 'change-row' },
            badge(c.field === 'title' ? 'Title' : 'Thumbnail', 'accent'),
            el('div', { class: 'change-meta' },
              el('div', { class: 'truncate', text: c.video_title }),
              el('div', { class: 'small muted', text: `${c.channel_title} · ${ago(c.detected_at)}` })),
            el('div', { class: 'small muted change-effect', text: c.effect }))))
      : el('div', { class: 'card-body' },
          el('p', { class: 'muted small', text:
            'Nothing yet. The tracker snapshots your watchlist every few hours while the ' +
            'app is open — changes appear here once it has two readings to compare.' }))));

  /* Job log */
  if (jobs.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: 'Recent activity' }))),
      el('div', { class: 'card-body tight' },
        jobs.map((j) => el('div', { class: 'job-row' },
          badge(j.status === 'ok' ? 'ok' : j.status,
            j.status === 'ok' ? 'good' : j.status === 'error' ? 'critical' : '',
            j.status === 'ok' ? 'check' : j.status === 'error' ? 'alert' : ''),
          el('div', { class: 'truncate small', text: j.detail || j.error || j.job }),
          el('div', { class: 'small muted nowrap' },
            j.units_spent ? `${j.units_spent} units · ` : '', ago(j.started_at)))))));
  }

  return nodes;
}

function stat(label, value, note) {
  return el('div', { class: 'stat' },
    el('div', { class: 'stat-label', text: label }),
    el('div', { class: 'stat-value', text: value }),
    note ? el('div', { class: 'stat-note', text: note }) : null);
}
