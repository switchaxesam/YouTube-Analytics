/* Discover — keyword search.
 *
 * The only expensive screen in the app. YouTube charges 100 quota units per 50
 * search results, versus 1 unit per 50 for every other endpoint, so the cost is
 * shown before the search runs and results are cached for 12 hours.
 */

import { api } from '../api.js';
import { refreshStatus } from '../app.js';
import {
  el, clear, compact, full, ago, empty, notice, loading, toast, toastError,
  multiplierBadge, videoCell, badge, withBusy,
} from '../ui.js';

export async function render(view) {
  const results = el('div', { class: 'stack' });

  const query = el('input', { type: 'search', autofocus: true,
    placeholder: 'e.g. beginner woodworking mistakes' });
  const limit = el('select', {},
    [['10', '10 results'], ['25', '25 results'], ['50', '50 results']].map(([v, l]) =>
      el('option', { value: v, selected: v === '25', text: l })));
  const window_ = el('select', {},
    [['', 'Any time'], ['7', 'Past week'], ['30', 'Past month'],
     ['90', 'Past 3 months'], ['365', 'Past year']].map(([v, l]) =>
      el('option', { value: v, selected: v === '90', text: l })));

  const cost = el('div', { class: 'cost-hint' });

  async function updateCost() {
    try {
      const est = await api.post('/api/discover/estimate', {
        query: query.value || 'x', limit: Number(limit.value),
      });
      clear(cost);
      cost.append(el('span', {
        class: est.affordable ? 'muted small' : 'small',
        text: `≈ ${est.estimated_units} units (${est.search_units} to search, ` +
              `${est.fetch_units} to fetch) · ${full(est.remaining)} left today`,
      }));
    } catch { clear(cost); }
  }
  limit.onchange = updateCost;
  updateCost();

  const search = el('button', { class: 'btn primary' }, 'Search');
  const run = () => withBusy(search, 'Searching…', async () => {
    const q = query.value.trim();
    if (!q) { toast('Type something to search for.', { kind: 'warning' }); return; }

    clear(results);
    results.append(loading('Searching YouTube…'));
    try {
      const data = await api.post('/api/discover', {
        query: q, limit: Number(limit.value),
        published_within_days: window_.value ? Number(window_.value) : null,
      });
      await refreshStatus();
      await updateCost();
      clear(results);
      results.append(...renderResults(data, q));
    } catch (err) {
      clear(results);
      results.append(notice('critical', 'Search failed', err.message, err.hint));
      toastError(err);
    }
  });
  search.onclick = run;
  query.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Discover' }),
      el('p', { class: 'lede', text:
        'Find videos by keyword, then price each one against its own channel\'s ' +
        'baseline. Useful for sizing up a topic before you make it — if the only ' +
        'videos on a subject are ordinary for their channels, the topic may be the ' +
        'reason.' }))));

  view.append(el('div', { class: 'filters' },
    el('div', { class: 'field grow' }, el('label', { text: 'Search YouTube' }), query),
    el('div', { class: 'field' }, el('label', { text: 'Results' }), limit),
    el('div', { class: 'field' }, el('label', { text: 'Published' }), window_),
    el('div', { class: 'field' }, el('label', { text: ' ' }), search)));

  view.append(el('div', { class: 'mb-md' },
    notice('info', 'Search is the expensive one',
      'YouTube charges 100 quota units per 50 search results — about a hundred times ' +
      'what every other call costs. Channel Lens uses search only to find which videos ' +
      'exist, then pulls the actual data with the cheap endpoint, and caches the results ' +
      'for 12 hours so repeating a search today is free.'),
    cost));

  view.append(results);
}

function renderResults(data, query) {
  const nodes = [];

  if (data.message) return [empty({ title: 'No results', message: data.message, icon: 'search' })];

  nodes.push(el('div', { class: 'row wrap mb-sm' },
    badge(`${data.units_spent} units spent`, data.cache_hits ? 'good' : ''),
    data.cache_hits ? badge(`${data.cache_hits} served from cache`, 'good', 'check') : null));

  if (data.results.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Priced against their channels' }),
          el('div', { class: 'sub', text:
            `${data.results.length} result${data.results.length === 1 ? '' : 's'} for “${query}”` }))),
      el('div', { class: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {},
          el('th', { text: 'Video' }),
          el('th', { class: 'num', text: 'vs normal' }),
          el('th', { class: 'num', text: 'Views' }),
          el('th', { class: 'num', text: 'Channel median' }),
          el('th', { text: 'Published' }))),
        el('tbody', {}, data.results.map((r) => el('tr', {},
          el('td', {}, videoCell(r)),
          el('td', { class: 'num' }, multiplierBadge(r.multiplier)),
          el('td', { class: 'num tnum', text: compact(r.views) }),
          el('td', { class: 'num tnum muted', text: compact(r.baseline_views) }),
          el('td', { class: 'small muted nowrap', text: ago(r.published_at) }))))))));
  }

  if (data.unscored?.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Found, but not priceable' }),
          el('div', { class: 'sub', text: data.unscored_note }))),
      el('div', { class: 'table-wrap' }, el('table', {},
        el('tbody', {}, data.unscored.map((r) => el('tr', {},
          el('td', {}, videoCell(r)),
          el('td', { class: 'num tnum', text: compact(r.views) }),
          el('td', {}, addChannelButton(r)))))))));
  }

  return nodes;
}

function addChannelButton(row) {
  const btn = el('button', { class: 'btn ghost sm', text: 'Track this channel' });
  btn.onclick = () => withBusy(btn, 'Adding…', async () => {
    const result = await api.post('/api/channels', {
      reference: row.channel_id || row.url, video_limit: 50,
    });
    toast(`Tracking ${result.channel.title}. Re-run the search to see it priced.`,
      { kind: 'good' });
    await refreshStatus();
  });
  return btn;
}
