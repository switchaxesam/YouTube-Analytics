/* Application shell: routing, global state, and the quota meter.
 *
 * Pages are loaded as ES modules on first visit, so the browser only parses
 * what's actually been opened. No bundler is involved — this is exactly what
 * ships.
 */

import { api } from './api.js';
import { clear, el, loading, toastError, compact, full } from './ui.js';

const ROUTES = {
  overview:     () => import('./pages/overview.js'),
  outliers:     () => import('./pages/outliers.js'),
  trajectory:   () => import('./pages/trajectory.js'),
  discover:     () => import('./pages/discover.js'),
  channels:     () => import('./pages/channels.js'),
  tracker:      () => import('./pages/tracker.js'),
  packaging:    () => import('./pages/packaging.js'),
  'my-channel': () => import('./pages/mychannel.js'),
  settings:     () => import('./pages/settings.js'),
};

/** Shared app state. Pages read it; only the shell writes it. */
export const state = {
  status: null,
  channels: [],
};

export async function refreshStatus() {
  try {
    state.status = await api.get('/api/status');
    applyTheme(state.status.settings.theme);
    renderQuota(state.status.quota);
    renderSetupBadge(state.status.required_missing ?? 0);
  } catch (err) {
    toastError(err, 'Could not load app status');
  }
  return state.status;
}

export async function refreshChannels() {
  try {
    state.channels = await api.get('/api/channels');
  } catch {
    state.channels = [];
  }
  return state.channels;
}

/* ---------------------------------------------------------------- theme */

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
  else delete root.dataset.theme;
}

/* ---------------------------------------------------------------- quota */

export function renderQuota(quota) {
  if (!quota) return;
  const fill = document.getElementById('quota-fill');
  const value = document.getElementById('quota-value');
  const reset = document.getElementById('quota-reset');

  const fraction = quota.fraction_used || 0;
  fill.style.width = `${Math.min(100, fraction * 100)}%`;
  fill.className = 'meter-fill' + (fraction >= 0.9 ? ' critical' : fraction >= 0.7 ? ' warn' : '');
  value.textContent = `${compact(quota.used)} / ${compact(quota.budget)}`;
  reset.textContent = `${full(quota.remaining)} left · resets in ${quota.resets_in}`;
  document.getElementById('quota').title =
    `${full(quota.used)} of ${full(quota.budget)} units used today. Resets in ${quota.resets_in}.`;
}

/** Only genuine blockers earn a badge; optional extras are not chores. */
function renderSetupBadge(count) {
  const badge = document.getElementById('nav-badge-setup');
  badge.hidden = !count;
  badge.textContent = String(count);
  badge.title = `${count} required setting still needs configuring`;
}

/** Nudge toward the tracker when competitors have changed something recently. */
export async function refreshChangesBadge() {
  const badge = document.getElementById('nav-badge-changes');
  try {
    const changes = await api.get('/api/changes', { days: 7, limit: 50 });
    badge.hidden = changes.length === 0;
    badge.textContent = String(changes.length);
    badge.title = `${changes.length} packaging change${changes.length === 1 ? '' : 's'} in the last 7 days`;
  } catch {
    badge.hidden = true;
  }
}

/* --------------------------------------------------------------- router */

let currentRoute = null;
let currentCleanup = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '') || 'overview';
  const [path, query] = raw.split('?');
  const [route, ...rest] = path.split('/');
  return {
    route: ROUTES[route] ? route : 'overview',
    params: rest,
    query: Object.fromEntries(new URLSearchParams(query || '')),
  };
}

async function navigate() {
  const { route, params, query } = parseHash();

  for (const link of document.querySelectorAll('.nav-item')) {
    link.classList.toggle('active', link.dataset.route === route);
  }

  if (route === currentRoute && !params.length) {
    // Re-entering the same route (e.g. clicking the active nav item) re-runs
    // the page rather than doing nothing, which is what users expect.
  }

  const view = document.getElementById('view');
  clear(view);
  view.append(loading());

  currentCleanup?.();
  currentCleanup = null;
  currentRoute = route;

  try {
    const module = await ROUTES[route]();
    clear(view);
    currentCleanup = await module.render(view, { params, query }) || null;
  } catch (err) {
    clear(view);
    view.append(renderRouteError(err));
    toastError(err, 'Could not open that page');
  }
  window.scrollTo({ top: 0, behavior: 'instant' });
}

function renderRouteError(err) {
  return el('div', { class: 'card' },
    el('div', { class: 'card-body' },
      el('div', { class: 'empty' },
        el('h3', { text: 'This page failed to load' }),
        el('p', { text: err?.message || String(err) }),
        err?.hint ? el('p', { class: 'muted mt-sm', text: err.hint }) : null,
        el('div', { class: 'actions' },
          el('button', { class: 'btn primary', onClick: () => navigate() }, 'Try again')))));
}

/* ------------------------------------------------------------ bootstrap */

window.addEventListener('hashchange', navigate);

(async function start() {
  await refreshStatus();
  await refreshChannels();
  await refreshChangesBadge();
  await navigate();

  // Keep the quota meter honest without polling hard — it only moves when the
  // app itself spends, plus the daily reset.
  setInterval(async () => {
    try { renderQuota(await api.get('/api/quota')); } catch { /* offline is fine */ }
  }, 60_000);
})();
