/* Tracker — view history and detected packaging changes.
 *
 * This is the only screen showing information that cannot be obtained by
 * querying YouTube today: it requires having been watching. A competitor's
 * thumbnail swap leaves no public trace, but if you held snapshots either side
 * of it, you have their A/B test result.
 */

import { api } from '../api.js';
import { refreshStatus } from '../app.js';
import {
  el, clear, compact, full, ago, dateShort, empty, notice, loading,
  toast, toastError, badge, withBusy, modal, thumb,
} from '../ui.js';
import { lineChart, withTableView } from '../charts.js';

export async function render(view) {
  const body = el('div', { class: 'stack' });

  const runBtn = el('button', { class: 'btn primary' }, 'Poll now');
  runBtn.onclick = () => withBusy(runBtn, 'Polling…', async () => {
    const result = await api.post('/api/tracker/run');
    await refreshStatus();
    toast(result.detail || result.error || 'Done.',
      { kind: result.status === 'ok' ? 'good' : 'critical' });
    await load();
  });

  const addBtn = el('button', { class: 'btn' }, 'Watch a video');
  addBtn.onclick = () => watchDialog(load);

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Tracker' }),
      el('p', { class: 'lede', text:
        'Snapshots of every watched video, taken every few hours while the app is open. ' +
        'Two readings give a velocity; a run of them shows whether YouTube is still ' +
        'pushing a video. It also catches title and thumbnail swaps, which is the ' +
        'closest thing to seeing a competitor\'s packaging experiments.' })),
    el('div', { class: 'page-actions' }, addBtn, runBtn)));

  view.append(body);

  async function load() {
    clear(body);
    body.append(loading());
    try {
      const [changes, watchlist] = await Promise.all([
        api.get('/api/changes', { days: 90, limit: 100 }),
        api.get('/api/watchlist'),
      ]);
      clear(body);
      body.append(...renderTracker(changes, watchlist, load));
    } catch (err) {
      clear(body);
      body.append(notice('critical', 'Could not load the tracker', err.message, err.hint));
    }
  }

  await load();
}

function renderTracker(changes, watchlist, reload) {
  const nodes = [];

  const withEffect = changes.filter((c) => c.velocity_before !== null && c.velocity_after !== null);
  nodes.push(el('div', { class: 'grid cols-3' },
    stat('Changes detected', full(changes.length), 'in the last 90 days'),
    stat('With before/after data', full(withEffect.length), 'enough snapshots to compare'),
    stat('Videos watched explicitly', full(watchlist.filter((w) => w.kind === 'video').length))));

  if (!changes.length) {
    nodes.push(el('div', { class: 'card' }, el('div', { class: 'card-body' },
      empty({
        title: 'No changes caught yet',
        message: 'The tracker needs at least two polls of the same video before it can ' +
                 'notice anything. Add channels or individual videos, leave the app open, ' +
                 'and check back — this screen gets more useful the longer it runs.',
        icon: 'clock',
        actions: [{ label: 'Add channels', variant: 'primary',
          onClick: () => { window.location.hash = '#/channels'; } }],
      }))));
  } else {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Detected changes' }),
          el('div', { class: 'sub', text: 'Newest first' }))),
      el('div', { class: 'card-body tight' },
        changes.map((c) => changeRow(c)))));
  }

  if (watchlist.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Watchlist' }),
          el('div', { class: 'sub', text: 'Individually watched videos' }))),
      el('div', { class: 'card-body tight' },
        watchlist.map((entry) => el('div', { class: 'job-row' },
          badge(entry.kind, entry.active ? 'accent' : ''),
          el('div', { class: 'truncate small', text: entry.label || entry.value }),
          el('div', { class: 'row' },
            el('button', { class: 'btn ghost sm', text: 'History',
              onClick: () => historyDialog(entry.value) }),
            el('button', { class: 'btn ghost sm', text: 'Remove',
              onClick: async () => {
                await api.del(`/api/watchlist/${entry.id}`);
                toast('Removed from the watchlist.', { kind: 'info' });
                await reload();
              } })))))));
  }

  return nodes;
}

function changeRow(change) {
  const kind = change.velocity_after === null ? ''
    : change.velocity_after > change.velocity_before * 1.15 ? 'good'
    : change.velocity_after < change.velocity_before * 0.85 ? 'critical' : '';

  return el('div', { class: 'change-detail' },
    el('div', { class: 'row wrap' },
      badge(change.field === 'title' ? 'Title changed' : 'Thumbnail changed', 'accent'),
      el('span', { class: 'small muted', text: `${change.channel_title} · ${ago(change.detected_at)}` }),
      change.views_at_change
        ? el('span', { class: 'small muted', text: `at ${compact(change.views_at_change)} views` })
        : null),

    el('a', { class: 'change-title', href: change.url, target: '_blank', rel: 'noopener',
      text: change.video_title }),

    change.field === 'title'
      ? el('div', { class: 'diff' },
          el('div', { class: 'diff-old' }, el('span', { class: 'diff-tag', text: 'was' }),
            el('span', { text: change.old_value })),
          el('div', { class: 'diff-new' }, el('span', { class: 'diff-tag', text: 'now' }),
            el('span', { text: change.new_value })))
      : el('div', { class: 'diff thumbs' },
          el('figure', {}, thumb(change.old_value),
            el('figcaption', { class: 'small muted', text: 'before' })),
          el('figure', {}, thumb(change.new_value),
            el('figcaption', { class: 'small muted', text: 'after' }))),

    el('div', { class: `effect ${kind}`, text: change.effect }),

    el('button', { class: 'btn ghost sm', text: 'View history',
      onClick: () => historyDialog(change.video_id) }));
}

async function historyDialog(videoId) {
  const body = el('div', {}, loading());
  modal({
    title: 'View history',
    subtitle: 'Every snapshot Channel Lens has taken of this video.',
    body,
    actions: [{ label: 'Close', variant: 'primary' }],
  });

  try {
    const data = await api.get(`/api/videos/${videoId}`);
    const history = data.history || {};
    const snapshots = history.snapshots || [];
    clear(body);

    if (snapshots.length < 2) {
      body.append(notice('info', 'Not enough snapshots yet',
        'A trend needs at least two readings. The tracker takes one every few hours ' +
        'while the app is open.'));
      return;
    }

    const chart = lineChart(
      snapshots.map((s) => ({ x: s.captured_at, y: s.views })),
      { valueLabel: 'Views', annotations: (history.revisions || []).map((r) => ({ x: r.detected_at })) },
    );

    const { table, toggle } = withTableView(chart, {
      label: 'Table view',
      columns: [
        { label: 'Captured', get: (r) => dateShort(r.captured_at) },
        { label: 'Views', num: true, get: (r) => full(r.views) },
        { label: 'Likes', num: true, get: (r) => full(r.likes) },
      ],
      rows: snapshots,
    });

    body.append(
      el('div', { class: 'between mb-sm' },
        el('h3', { text: data.video.title }), toggle),
      chart, table,
      history.velocity_7d
        ? el('div', { class: 'grid cols-2 mt-md' },
            stat('Last 7 days', `${compact(history.velocity_7d.views_per_day)}/day`),
            stat('All time', `${compact(history.velocity_overall?.views_per_day || 0)}/day`))
        : null,
      (history.revisions || []).length
        ? el('div', { class: 'mt-md' },
            el('h3', { class: 'mb-sm', text: 'Changes' }),
            history.revisions.map((r) => el('div', { class: 'small dim mb-sm' },
              `${dateShort(r.detected_at)} — ${r.field} changed. ${r.effect}`)))
        : null,
    );
  } catch (err) {
    clear(body);
    body.append(notice('critical', 'Could not load history', err.message, err.hint));
  }
}

function watchDialog(reload) {
  const input = el('input', { type: 'text', autofocus: true,
    placeholder: 'https://youtube.com/watch?v=… or the video ID' });
  modal({
    title: 'Watch a video',
    subtitle: 'Snapshots it every few hours and flags any title or thumbnail swap. ' +
              'Useful for a competitor\'s breakout you want to watch closely.',
    body: el('div', { class: 'field' }, el('label', { text: 'Video' }), input),
    actions: [
      { label: 'Cancel' },
      { label: 'Watch it', variant: 'primary', onClick: async () => {
          try {
            const result = await api.post('/api/watchlist/videos', { reference: input.value.trim() });
            toast(`Now watching “${result.title}”.`, { kind: 'good' });
            await reload();
          } catch (err) { toastError(err); return 'keep'; }
        } },
    ],
  });
}

function stat(label, value, note) {
  return el('div', { class: 'stat' },
    el('div', { class: 'stat-label', text: label }),
    el('div', { class: 'stat-value sm', text: value }),
    note ? el('div', { class: 'stat-note', text: note }) : null);
}
