/* Outliers — the flagship view.
 *
 * Filtering is free: scores are computed at ingest and stored, so every control
 * here re-queries the local database and spends no quota. That's why the
 * filters can be this liberal.
 *
 * The previous render is held at reduced opacity during a refetch rather than
 * replaced by a skeleton, so changing a filter never causes a layout jump.
 */

import { api } from '../api.js';
import { state, refreshChannels } from '../app.js';
import {
  el, clear, compact, full, ago, empty, notice, loading, toastError,
  multiplierBadge, videoCell, debounce, badge, withBusy, modal,
} from '../ui.js';

const filters = {
  min_multiplier: 2,
  format: 'all',
  max_age_days: '',
  min_views: '',
  channel_id: [],
  reliable_only: false,
  search: '',
  sort: 'multiplier',
  limit: 100,
};

export async function render(view) {
  await refreshChannels();

  const results = el('div', { class: 'stack' });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Outliers' }),
      el('p', { class: 'lede', text:
        'Videos that beat what their own channel normally does. Dividing by each ' +
        'channel\'s own median is what lets a small creator\'s breakout show up next to ' +
        'a large channel\'s routine upload — and it uses the median, not the mean, so ' +
        'one past viral video doesn\'t hide the next one.' })),
    el('div', { class: 'page-actions' }, rescoreButton(() => load()))));

  view.append(filterBar(() => load()));
  view.append(results);

  async function load() {
    const first = !results.dataset.loaded;
    if (first) { clear(results); results.append(loading('Scoring…')); }
    else results.classList.add('is-refetching');

    try {
      const data = await api.get('/api/outliers', {
        ...filters,
        channel_id: filters.channel_id.length ? filters.channel_id : undefined,
        reliable_only: filters.reliable_only || undefined,
        max_age_days: filters.max_age_days || undefined,
        min_views: filters.min_views || undefined,
        search: filters.search || undefined,
      });
      clear(results);
      results.dataset.loaded = '1';
      results.append(...renderResults(data));
    } catch (err) {
      clear(results);
      results.append(errorState(err, load));
    } finally {
      results.classList.remove('is-refetching');
    }
  }

  await load();
}

/* -------------------------------------------------------------- filters */

function filterBar(onChange) {
  const bar = el('div', { class: 'filters' });
  const fire = debounce(onChange, 260);

  const search = el('input', { type: 'search', placeholder: 'Search titles…' });
  search.oninput = () => { filters.search = search.value.trim(); fire(); };

  const multiplier = el('select', {},
    [['1', 'Everything'], ['2', '2× and up'], ['3', '3× and up'],
     ['5', '5× and up'], ['10', '10× and up']].map(([v, label]) =>
      el('option', { value: v, selected: String(filters.min_multiplier) === v, text: label })));
  multiplier.onchange = () => { filters.min_multiplier = Number(multiplier.value); onChange(); };

  const format = el('select', {},
    [['all', 'All formats'], ['long', 'Long-form'], ['short', 'Shorts']].map(([v, label]) =>
      el('option', { value: v, selected: filters.format === v, text: label })));
  format.onchange = () => { filters.format = format.value; onChange(); };

  const age = el('select', {},
    [['', 'Any age'], ['7', 'Last 7 days'], ['30', 'Last 30 days'],
     ['90', 'Last 90 days'], ['365', 'Last year']].map(([v, label]) =>
      el('option', { value: v, selected: String(filters.max_age_days) === v, text: label })));
  age.onchange = () => { filters.max_age_days = age.value; onChange(); };

  const views = el('select', {},
    [['', 'Any views'], ['1000', '1K+'], ['10000', '10K+'],
     ['100000', '100K+'], ['1000000', '1M+']].map(([v, label]) =>
      el('option', { value: v, selected: String(filters.min_views) === v, text: label })));
  views.onchange = () => { filters.min_views = views.value; onChange(); };

  const sort = el('select', {},
    [['multiplier', 'Multiplier'], ['views', 'Views'],
     ['recent', 'Newest'], ['percentile', 'Percentile']].map(([v, label]) =>
      el('option', { value: v, selected: filters.sort === v, text: label })));
  sort.onchange = () => { filters.sort = sort.value; onChange(); };

  const reliable = el('input', { type: 'checkbox', checked: filters.reliable_only });
  reliable.onchange = () => { filters.reliable_only = reliable.checked; onChange(); };

  bar.append(
    fieldOf('Search', search, 'grow'),
    fieldOf('Multiplier', multiplier),
    fieldOf('Format', format),
    fieldOf('Published', age),
    fieldOf('Min views', views),
    fieldOf('Sort by', sort),
    el('div', { class: 'field' }, el('label', { text: 'Confidence' }),
      el('label', { class: 'switch', title: 'Hide videos whose baseline is too small to trust' },
        reliable, el('span', { class: 'track' }), el('span', { class: 'small', text: 'Reliable only' }))),
  );

  if (state.channels.length) bar.append(channelFilter(onChange));
  return bar;
}

function fieldOf(label, input, extra = '') {
  return el('div', { class: `field ${extra}` }, el('label', { text: label }), input);
}

function channelFilter(onChange) {
  const wrap = el('div', { class: 'field grow' }, el('label', { text: 'Channels' }));
  const chips = el('div', { class: 'chips' });

  const all = el('button', { class: `chip ${filters.channel_id.length ? '' : 'on'}`, text: 'All' });
  all.onclick = () => { filters.channel_id = []; onChange(); paint(); };
  chips.append(all);

  for (const channel of state.channels) {
    const chip = el('button', { class: 'chip', text: channel.title || channel.id,
      dataset: { id: channel.id } });
    chip.onclick = () => {
      const i = filters.channel_id.indexOf(channel.id);
      if (i >= 0) filters.channel_id.splice(i, 1); else filters.channel_id.push(channel.id);
      onChange(); paint();
    };
    chips.append(chip);
  }

  function paint() {
    all.classList.toggle('on', filters.channel_id.length === 0);
    for (const chip of chips.querySelectorAll('.chip[data-id]')) {
      chip.classList.toggle('on', filters.channel_id.includes(chip.dataset.id));
    }
  }
  paint();
  wrap.append(chips);
  return wrap;
}

/* -------------------------------------------------------------- results */

function renderResults(data) {
  if (!data.results.length) {
    if (!state.channels.length) {
      return [empty({
        title: 'No channels yet',
        message: 'Add a few competitors and Channel Lens will pull their recent uploads, ' +
                 'work out each channel\'s normal, and rank everything that beat it.',
        actions: [{ label: 'Add a channel', variant: 'primary',
          onClick: () => { window.location.hash = '#/channels'; } }],
      })];
    }
    return [empty({
      title: 'Nothing matches these filters',
      message: 'Try lowering the multiplier or widening the date range. Filtering costs ' +
               'no quota, so experiment freely.',
      icon: 'search',
      actions: [{ label: 'Reset filters', onClick: () => location.reload() }],
    })];
  }

  const nodes = [];

  const strong = data.results.filter((r) => r.is_outlier).length;
  const unreliable = data.results.filter((r) => !r.reliable).length;

  nodes.push(el('div', { class: 'grid cols-4' },
    stat('Matching videos', full(data.total)),
    stat(`At or above ${data.threshold}×`, full(strong)),
    stat('Top multiplier', `${data.results[0].multiplier.toFixed(1)}×`),
    stat('Channels compared', full(new Set(data.results.map((r) => r.channel_id)).size))));

  if (unreliable) {
    nodes.push(notice('warning', `${unreliable} of these rest on a thin baseline`,
      'Their channel has too few mature uploads for the median to mean much. They stay ' +
      'in the list but are marked, because a 12× against four videos is not the same ' +
      'finding as a 12× against forty.'));
  }

  const rows = data.results.map((r) => el('tr', {},
    el('td', {}, videoCell(r)),
    el('td', { class: 'num' }, multiplierBadge(r.multiplier, data.threshold)),
    el('td', { class: 'num tnum', text: compact(r.views) }),
    el('td', { class: 'num tnum muted', text: compact(r.baseline_views) }),
    el('td', { class: 'num tnum', text: `${r.percentile.toFixed(0)}%` }),
    el('td', { class: 'nowrap muted small', text: ago(r.published_at) }),
    el('td', {}, el('div', { class: 'row' },
      r.is_short ? badge('Short') : null,
      !r.reliable ? badge('thin baseline', 'warning', 'alert') : null,
      detailButton(r))),
  ));

  nodes.push(el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Ranked results' }),
        el('div', { class: 'sub', text: `Showing ${data.shown} of ${data.total}` }))),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', { text: 'Video' }),
          el('th', { class: 'num', text: 'vs normal' }),
          el('th', { class: 'num', text: 'Views' }),
          el('th', { class: 'num', text: 'Channel median' }),
          el('th', { class: 'num', text: 'Percentile' }),
          el('th', { text: 'Published' }),
          el('th', { text: '' }))),
        el('tbody', {}, rows)))));

  return nodes;
}

function stat(label, value, note) {
  return el('div', { class: 'stat' },
    el('div', { class: 'stat-label', text: label }),
    el('div', { class: 'stat-value', text: value }),
    note ? el('div', { class: 'stat-note', text: note }) : null);
}

function detailButton(row) {
  const btn = el('button', { class: 'btn ghost sm', text: 'Why?' });
  btn.onclick = () => modal({
    title: row.title,
    subtitle: `${row.channel_title} · ${ago(row.published_at)}`,
    body: el('div', { class: 'stack' },
      el('div', { class: 'grid cols-3' },
        stat('Views', full(row.views)),
        stat('Channel median', full(row.baseline_views)),
        stat('Multiplier', `${row.multiplier.toFixed(2)}×`)),
      el('p', { class: 'help', text:
        `This video has ${full(row.views)} views against a channel median of ` +
        `${full(row.baseline_views)} for the same format — ${row.multiplier.toFixed(1)} ` +
        `times normal, placing it in the ${row.percentile.toFixed(0)}th percentile of ` +
        `its channel's recent uploads.` }),
      row.projected_multiplier
        ? notice('info', 'Still maturing',
            `On a typical accrual curve this is on course for roughly ` +
            `${row.projected_multiplier.toFixed(1)}× — a projection, not a measurement.`)
        : null,
      ...row.caveats.map((c) => notice('warning', '', c)),
    ),
    actions: [
      { label: 'Open on YouTube', onClick: () => window.open(row.url, '_blank', 'noopener') },
      { label: 'Close', variant: 'primary' },
    ],
  });
  return btn;
}

function rescoreButton(onDone) {
  const btn = el('button', { class: 'btn' }, 'Rescore');
  btn.title = 'Recompute every multiplier from stored data. Costs no quota.';
  btn.onclick = () => withBusy(btn, 'Rescoring…', async () => {
    const result = await api.post('/api/outliers/rescore');
    await onDone();
    return result;
  });
  return btn;
}

function errorState(err, retry) {
  return el('div', { class: 'card' }, el('div', { class: 'card-body' },
    empty({
      title: err.isUnconfigured ? 'Not set up yet' : 'Could not load outliers',
      message: err.message + (err.hint ? ` ${err.hint}` : ''),
      actions: err.isUnconfigured
        ? [{ label: 'Open settings', variant: 'primary',
             onClick: () => { window.location.hash = '#/settings'; } }]
        : [{ label: 'Try again', variant: 'primary', onClick: retry }],
    })));
}
