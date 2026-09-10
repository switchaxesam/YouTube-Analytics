/* Trajectory — videos judged against what was normal at the time.
 *
 * The Outliers page divides by a channel's whole-catalogue median. That is the
 * right question for a stable channel and the wrong one for a growing one: an
 * old video gets held to a bar built from years of later growth, and a recent
 * one is measured against a floor an earlier breakout created.
 *
 * This page divides by a *trailing* window instead, keeps the catalogue figure
 * beside it, and shows the drift between them — because where the two methods
 * disagree is exactly where the catalogue one was reading growth as quality.
 */

import { api } from '../api.js';
import { state, refreshChannels } from '../app.js';
import {
  el, clear, compact, full, ago, dateShort, empty, notice, loading,
  toast, toastError, badge, videoCell, withBusy, modal,
} from '../ui.js';

const options = {
  window_kind: 'count',
  window_count: 15,
  window_days: 180,
  min_prior: 8,
  format: 'all',
  sort: 'trailing',
  direction: 'desc',
  only_sufficient: true,
  channel_id: [],
};

export async function render(view) {
  await refreshChannels();
  const body = el('div', { class: 'stack' });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Trajectory' }),
      el('p', { class: 'lede', text:
        'Every video against what was normal for its channel at the time it went up, ' +
        'rather than against the whole catalogue. For a channel that grew, those are ' +
        'very different questions — and the second one holds early videos to a bar the ' +
        'channel only reached years later.' })),
    el('div', { class: 'page-actions' },
      el('button', { class: 'btn', onClick: () => fullHistoryDialog(load) }, 'Pull full history'))));

  view.append(controls(() => load()));
  view.append(body);

  async function load() {
    clear(body);
    body.append(loading('Recomputing trailing baselines…'));
    try {
      const data = await api.get('/api/trailing', {
        ...options,
        channel_id: options.channel_id.length ? options.channel_id : undefined,
        only_sufficient: options.only_sufficient || undefined,
      });
      clear(body);
      body.append(...renderTrajectory(data, load));
    } catch (err) {
      clear(body);
      body.append(notice('critical', 'Could not compute trailing baselines', err.message, err.hint));
      toastError(err);
    }
  }

  await load();
}

/* -------------------------------------------------------------- controls */

function controls(onChange) {
  const bar = el('div', { class: 'filters' });

  const kind = el('select', {},
    [['count', 'Preceding N uploads'], ['days', 'Preceding N days']].map(([v, l]) =>
      el('option', { value: v, selected: options.window_kind === v, text: l })));

  const count = el('input', { type: 'number', min: 2, max: 200, value: options.window_count });
  const days = el('input', { type: 'number', min: 7, max: 3650, value: options.window_days });
  const countField = fieldOf('Uploads in window', count);
  const daysField = fieldOf('Days in window', days);

  const paintWindow = () => {
    countField.hidden = kind.value !== 'count';
    daysField.hidden = kind.value !== 'days';
  };
  kind.onchange = () => { options.window_kind = kind.value; paintWindow(); onChange(); };
  count.onchange = () => { options.window_count = Number(count.value); onChange(); };
  days.onchange = () => { options.window_days = Number(days.value); onChange(); };
  paintWindow();

  const minPrior = el('input', { type: 'number', min: 2, max: 100, value: options.min_prior });
  minPrior.onchange = () => { options.min_prior = Number(minPrior.value); onChange(); };

  const format = el('select', {},
    [['all', 'All formats'], ['long', 'Long-form'], ['short', 'Shorts']].map(([v, l]) =>
      el('option', { value: v, selected: options.format === v, text: l })));
  format.onchange = () => { options.format = format.value; onChange(); };

  const sort = el('select', {},
    [['trailing', 'vs trailing normal'], ['catalog', 'vs catalogue normal'],
     ['drift', 'Disagreement between them'], ['recent', 'Newest'], ['views', 'Views']]
      .map(([v, l]) => el('option', { value: v, selected: options.sort === v, text: l })));
  sort.onchange = () => { options.sort = sort.value; onChange(); };

  const sufficient = el('input', { type: 'checkbox', checked: options.only_sufficient });
  sufficient.onchange = () => { options.only_sufficient = sufficient.checked; onChange(); };

  bar.append(
    fieldOf('Window', kind), countField, daysField,
    fieldOf('Minimum prior uploads', minPrior),
    fieldOf('Format', format),
    fieldOf('Sort by', sort),
    el('div', { class: 'field' }, el('label', { text: 'History' }),
      el('label', { class: 'switch', title: 'Hide videos with too little history to score' },
        sufficient, el('span', { class: 'track' }),
        el('span', { class: 'small', text: 'Scorable only' }))),
  );

  if (state.channels.length) bar.append(channelPicker(onChange));
  return bar;
}

function fieldOf(label, input) {
  return el('div', { class: 'field' }, el('label', { text: label }), input);
}

function channelPicker(onChange) {
  const wrap = el('div', { class: 'field grow' }, el('label', { text: 'Channels' }));
  const chips = el('div', { class: 'chips' });

  const all = el('button', { class: `chip ${options.channel_id.length ? '' : 'on'}`, text: 'All' });
  all.onclick = () => { options.channel_id = []; paint(); onChange(); };
  chips.append(all);

  for (const channel of state.channels) {
    const chip = el('button', { class: 'chip', text: channel.title || channel.id,
      dataset: { id: channel.id } });
    chip.onclick = () => {
      const i = options.channel_id.indexOf(channel.id);
      if (i >= 0) options.channel_id.splice(i, 1); else options.channel_id.push(channel.id);
      paint(); onChange();
    };
    chips.append(chip);
  }

  function paint() {
    all.classList.toggle('on', options.channel_id.length === 0);
    for (const chip of chips.querySelectorAll('.chip[data-id]')) {
      chip.classList.toggle('on', options.channel_id.includes(chip.dataset.id));
    }
  }
  paint();
  wrap.append(chips);
  return wrap;
}

/* --------------------------------------------------------------- results */

function renderTrajectory(data, reload) {
  if (!data.summaries.length) {
    return [empty({
      title: 'No channels with stored uploads',
      message: 'Trailing baselines need a channel\'s chronological history. Add channels, ' +
               'then use “Pull full history” to fetch the complete upload list.',
      actions: [{ label: 'Add channels', variant: 'primary',
        onClick: () => { window.location.hash = '#/channels'; } }],
    })];
  }

  const nodes = [];

  nodes.push(notice('info', `Comparing each video against ${data.window.description}`,
    `A video needs at least ${data.window.min_prior} prior uploads in the same format ` +
    `before it gets a trailing number at all — below that it is marked “insufficient ` +
    `history” rather than given a figure built on three videos.`));

  nodes.push(...data.summaries.map((s) => summaryCard(s, data)));

  if (data.results.length) nodes.push(videoTable(data));
  return nodes;
}

function summaryCard(summary, data) {
  const rows = [];

  if (summary.breakouts.length) {
    rows.push(el('div', { class: 'mt-md' },
      el('h3', { class: 'mb-sm', text: 'Breakout points' }),
      el('p', { class: 'small dim mb-sm', text:
        'Uploads after which the channel’s floor moved up and stayed up — the median of ' +
        'the videos after clears the median of those before by the threshold.' }),
      summary.breakouts.map((b) => breakoutRow(b, 'breakout'))));
  }

  if (summary.spikes.length) {
    rows.push(el('div', { class: 'mt-md' },
      el('h3', { class: 'mb-sm', text: 'Spikes that did not stick' }),
      el('p', { class: 'small dim mb-sm', text:
        'These cleared the threshold themselves, but the channel came back to roughly ' +
        'where it was. One good video, not a new level — worth separating, because ' +
        'chasing a spike as though it were a format is how a channel wastes a year.' }),
      summary.spikes.map((b) => breakoutRow(b, 'spike'))));
  }

  if (!summary.breakouts.length && !summary.spikes.length) {
    rows.push(el('p', { class: 'small muted mt-md', text:
      summary.scored
        ? 'No step changes detected — this channel’s level has been broadly stable across ' +
          'the history stored.'
        : 'Not enough history stored to detect step changes yet.' }));
  }

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {},
        el('h2', { text: summary.channel_title || summary.channel_id }),
        el('div', { class: 'sub', text:
          `${full(summary.total_videos)} videos · ${full(summary.scored)} scorable · ` +
          `${full(summary.insufficient)} without enough history` }))),
    el('div', { class: 'card-body' },
      el('div', { class: 'grid cols-4' },
        stat('Hit rate at 5×', `${summary.hit_rate_5x}%`, 'of scorable videos'),
        stat('Hit rate at 10×', `${summary.hit_rate_10x}%`, 'of scorable videos'),
        stat('Median multiplier', summary.median_trailing_multiplier
          ? `${summary.median_trailing_multiplier}×` : '—', 'vs trailing normal'),
        stat('Breakouts', String(summary.breakouts.length),
          summary.spikes.length ? `${summary.spikes.length} spike(s) too` : 'step changes')),
      ...rows));
}

function breakoutRow(event, kind) {
  const before = event.median_before || 0;
  const after = event.median_after || 0;
  return el('div', { class: `breakout-row ${kind}` },
    el('div', { class: 'breakout-when' },
      badge(kind === 'breakout' ? 'step change' : 'spike',
        kind === 'breakout' ? 'good' : 'warning',
        kind === 'breakout' ? 'check' : 'alert'),
      el('div', { class: 'small muted', text: dateShort(event.published_at) })),
    el('div', { class: 'breakout-meta' },
      el('a', { class: 'title', target: '_blank', rel: 'noopener',
                href: `https://www.youtube.com/watch?v=${event.video_id}`,
                text: event.title }),
      el('div', { class: 'small dim mt-sm', text:
        `Channel median went ${compact(before)} → ${compact(after)} per video ` +
        `(${event.ratio}×). This upload itself did ${compact(event.views)}.` })));
}

function videoTable(data) {
  const rows = data.results.map((r) => el('tr', {},
    el('td', {}, videoCell(r, { size: 'sm' })),
    el('td', { class: 'num' },
      r.trailing_multiplier
        ? el('span', { class: `badge ${r.trailing_multiplier >= 5 ? 'accent' : ''} multiplier` },
            `${r.trailing_multiplier.toFixed(1)}×`)
        : badge('no history', '')),
    el('td', { class: 'num tnum muted', text: r.trailing_median ? compact(r.trailing_median) : '—' }),
    el('td', { class: 'num tnum', text: r.trailing_percentile !== null ? `${r.trailing_percentile}%` : '—' }),
    el('td', { class: 'num tnum muted',
      text: r.catalog_multiplier ? `${r.catalog_multiplier.toFixed(1)}×` : '—' }),
    el('td', { class: 'num' }, driftCell(r)),
    el('td', { class: 'small muted nowrap', text: ago(r.published_at) }),
    el('td', {}, r.breakout
      ? badge(r.breakout === 'breakout' ? 'step' : 'spike',
              r.breakout === 'breakout' ? 'good' : 'warning')
      : null),
  ));

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {},
        el('h2', { text: 'Every video, both ways' }),
        el('div', { class: 'sub', text:
          `Showing ${data.shown} of ${data.total}. “Drift” is trailing ÷ catalogue — ` +
          `above 1 means the catalogue figure was underrating it.` }))),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', { text: 'Video' }),
          el('th', { class: 'num', text: 'vs trailing' }),
          el('th', { class: 'num', text: 'Trailing median' }),
          el('th', { class: 'num', text: 'Trailing pct' }),
          el('th', { class: 'num', text: 'vs catalogue' }),
          el('th', { class: 'num', text: 'Drift' }),
          el('th', { text: 'Published' }),
          el('th', { text: '' }))),
        el('tbody', {}, rows))));
}

function driftCell(row) {
  if (!row.drift) return el('span', { class: 'muted', text: '—' });
  const big = row.drift >= 1.5 || row.drift <= 0.67;
  return el('span', {
    class: `badge ${big ? 'warning' : ''} multiplier`,
    title: row.drift > 1
      ? 'The catalogue median was holding this video to a bar the channel reached later.'
      : 'The catalogue median is inflated by later, bigger videos.',
  }, `${row.drift.toFixed(2)}×`);
}

function stat(label, value, note) {
  return el('div', { class: 'stat' },
    el('div', { class: 'stat-label', text: label }),
    el('div', { class: 'stat-value sm', text: value }),
    note ? el('div', { class: 'stat-note', text: note }) : null);
}

/* ---------------------------------------------------------- full history */

function fullHistoryDialog(reload) {
  const select = el('select', {},
    state.channels.map((c) => el('option', {
      value: c.id,
      text: `${c.title} — ${full(c.stored_videos)} stored of ${full(c.video_count || 0)}`,
    })));

  modal({
    title: 'Pull full upload history',
    subtitle: 'Trailing baselines and breakout detection walk a channel’s whole timeline, ' +
              'so they need every upload — not just the recent slice imported by default.',
    body: el('div', { class: 'stack' },
      el('div', { class: 'field' }, el('label', { text: 'Channel' }), select),
      notice('info', 'This is cheap',
        'The uploads playlist costs 1 quota unit per 50 videos and fetching them costs ' +
        'another 1 per 50, so a thousand-video channel is roughly 40 units out of your ' +
        '10,000 a day. Anything already stored is refreshed rather than re-paid for.')),
    actions: [
      { label: 'Cancel' },
      { label: 'Pull history', variant: 'primary', onClick: async () => {
          try {
            const started = await api.post(`/api/channels/${select.value}/full-history`);
            toast(`Pulling ${full(started.expected_videos)} uploads from ` +
                  `${started.channel_title} — about ${started.estimated_units} units.`,
                  { kind: 'info', title: 'Started' });
            watchHistory(started.job_id, reload);
          } catch (err) { toastError(err); return 'keep'; }
        } },
    ],
  });
}

async function watchHistory(jobId, reload) {
  for (let i = 0; i < 2000; i++) {
    let job;
    try { job = await api.get(`/api/jobs/live/${jobId}`); }
    catch { return; }
    if (!job.running) {
      if (job.status === 'error') {
        toast(job.error || 'The pull failed.', { kind: 'critical', title: 'Full history failed' });
      } else if (job.result) {
        toast(`Stored ${full(job.result.stored)} of ${full(job.result.listed)} uploads. ` +
              `${job.result.units_spent} units spent.`,
              { kind: 'good', title: 'Full history pulled' });
      }
      await reload();
      return;
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
}
