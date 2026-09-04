/* Shared UI primitives: DOM building, formatting, toasts, modals, empty states.
 *
 * `el()` is a tiny hyperscript rather than template strings, because every
 * value that reaches the DOM here is set through textContent or a property —
 * so a video title containing markup can never become markup. Titles are
 * exactly the kind of untrusted, arbitrary text this app is full of.
 */

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;      // only for trusted icon markup
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in node && key !== 'list') {
      node[key] = value;
    } else {
      node.setAttribute(key, value === true ? '' : value);
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* ------------------------------------------------------------ formatting */

export function compact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(abs >= 1e10 ? 0 : 1).replace(/\.0$/, '') + 'B';
  if (abs >= 1e6) return (n / 1e6).toFixed(abs >= 1e7 ? 0 : 1).replace(/\.0$/, '') + 'M';
  if (abs >= 1e3) return (n / 1e3).toFixed(abs >= 1e4 ? 0 : 1).replace(/\.0$/, '') + 'K';
  return String(Math.round(n));
}

export function full(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Math.round(n).toLocaleString();
}

export function pct(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `${n.toFixed(digits)}%`;
}

export function duration(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

export function ago(iso) {
  if (!iso) return '—';
  const then = new Date(iso);
  const mins = (Date.now() - then.getTime()) / 60000;
  if (mins < 1) return 'just now';
  if (mins < 60) return `${Math.round(mins)}m ago`;
  const hours = mins / 60;
  if (hours < 24) return `${Math.round(hours)}h ago`;
  const days = hours / 24;
  if (days < 30) return `${Math.round(days)}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${(days / 365).toFixed(1)}y ago`;
}

export function dateShort(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/* ---------------------------------------------------------------- icons */

export const ICON = {
  check:  '<svg viewBox="0 0 16 16"><path d="M13.5 4.5 6.5 12 2.5 8.2l1.4-1.45 2.6 2.5 5.6-6z"/></svg>',
  alert:  '<svg viewBox="0 0 16 16"><path d="M8 1.5 15.3 14H.7L8 1.5zm0 4.2a.9.9 0 0 0-.9 1v3a.9.9 0 0 0 1.8 0v-3a.9.9 0 0 0-.9-1zm0 6.1a1 1 0 1 0 0 2 1 1 0 0 0 0-2z"/></svg>',
  info:   '<svg viewBox="0 0 16 16"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm0 3a1 1 0 1 1 0 2 1 1 0 0 1 0-2zm1 8.5H7V7h2v5.5z"/></svg>',
  x:      '<svg viewBox="0 0 16 16"><path d="M12.5 4.9 11.1 3.5 8 6.6 4.9 3.5 3.5 4.9 6.6 8l-3.1 3.1 1.4 1.4L8 9.4l3.1 3.1 1.4-1.4L9.4 8z"/></svg>',
  clock:  '<svg viewBox="0 0 16 16"><path d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1zm.9 3.4v3.3l2.6 1.6-.8 1.3-3.2-2V4.4z"/></svg>',
  spark:  '<svg viewBox="0 0 16 16"><path d="M8 .8 9.7 6l5.3.1-4.2 3.2 1.5 5.1L8 11.3 3.7 14.4l1.5-5.1L1 6.1 6.3 6z"/></svg>',
  empty:  '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M8 5v14"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>',
  key:    '<svg viewBox="0 0 24 24"><circle cx="8" cy="12" r="4"/><path d="M12 12h9M18 12v3M15.5 12v2.5"/></svg>',
  chart:  '<svg viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>',
};

/* --------------------------------------------------------------- toasts */

const toastRoot = () => document.getElementById('toasts');

export function toast(message, { kind = 'info', title = '', hint = '', timeout = 5200 } = {}) {
  const iconName = { good: 'check', critical: 'alert', warning: 'alert', info: 'info' }[kind] || 'info';
  const node = el('div', { class: `toast ${kind}`, role: 'status' },
    el('div', { class: 'icon', html: ICON[iconName] }),
    el('div', {},
      title ? el('strong', { text: title }) : null,
      el('div', { class: 'msg', text: message }),
      hint ? el('div', { class: 'hint', text: hint }) : null,
    ),
    el('button', { class: 'close', title: 'Dismiss', 'aria-label': 'Dismiss',
                   onClick: () => dismiss(node) }, '×'),
  );
  toastRoot().append(node);
  // Quota and auth problems need reading, so they don't auto-dismiss.
  if (timeout) setTimeout(() => dismiss(node), timeout);
  return node;
}

function dismiss(node) {
  if (!node.isConnected) return;
  node.classList.add('out');
  setTimeout(() => node.remove(), 200);
}

/** Report an ApiError with its hint, and never auto-dismiss the ones that matter. */
export function toastError(err, fallbackTitle = 'Something went wrong') {
  if (err?.name === 'AbortError') return;
  const sticky = err?.isQuota || err?.isUnconfigured || err?.isAuth;
  toast(err?.message || String(err), {
    kind: err?.isQuota ? 'warning' : 'critical',
    title: err?.isQuota ? 'Out of API quota'
         : err?.isUnconfigured ? 'Not set up yet'
         : err?.isAuth ? 'Not connected'
         : fallbackTitle,
    hint: err?.hint || '',
    timeout: sticky ? 0 : 6500,
  });
}

/* --------------------------------------------------------------- modals */

export function modal({ title, subtitle, body, actions = [], onClose }) {
  const root = document.getElementById('modal-root');
  clear(root);
  root.hidden = false;

  const close = () => {
    root.hidden = true;
    clear(root);
    document.removeEventListener('keydown', onKey);
    onClose?.();
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);

  const card = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' },
    el('div', { class: 'modal-head' },
      el('h2', { text: title }),
      subtitle ? el('p', { class: 'sub', text: subtitle }) : null,
    ),
    el('div', { class: 'modal-body' }, body),
    actions.length
      ? el('div', { class: 'modal-foot' },
          actions.map((a) => el('button', {
            class: `btn ${a.variant || ''}`,
            onClick: async () => { const keep = await a.onClick?.(close); if (keep !== 'keep') close(); },
          }, a.label)))
      : null,
  );

  root.append(card);
  root.onclick = (e) => { if (e.target === root) close(); };
  card.querySelector('input, select, textarea, button')?.focus();
  return { close, card };
}

/* -------------------------------------------------------- states & bits */

export function empty({ title, message, icon = 'empty', actions = [] }) {
  return el('div', { class: 'empty' },
    el('div', { class: 'icon', html: ICON[icon] || ICON.empty }),
    el('h3', { text: title }),
    el('p', { text: message }),
    actions.length
      ? el('div', { class: 'actions' },
          actions.map((a) => el('button', { class: `btn ${a.variant || ''}`, onClick: a.onClick }, a.label)))
      : null,
  );
}

export function notice(kind, title, message, fix) {
  const iconName = { good: 'check', critical: 'alert', warning: 'alert', info: 'info' }[kind] || 'info';
  return el('div', { class: `notice ${kind}` },
    el('div', { class: 'icon', html: ICON[iconName] }),
    el('div', {},
      title ? el('strong', { text: title }) : null,
      message ? el('p', { text: message }) : null,
      fix ? el('div', { class: 'fix', text: fix }) : null,
    ),
  );
}

export function loading(label = 'Loading…') {
  return el('div', { class: 'loading' }, el('div', { class: 'spinner' }), el('span', { text: label }));
}

export function badge(text, kind = '', icon = '') {
  return el('span', { class: `badge ${kind}` },
    icon ? el('span', { html: ICON[icon] || '' }) : null, text);
}

/** A multiplier badge: emphasised at or above the threshold, quiet below.
 *
 * Deliberately two states rather than a colour ramp. The number is printed
 * right there, so shading it by size would re-encode information already on
 * screen — and the status palette (green/amber/red) is reserved for things
 * that genuinely mean good or bad, which a magnitude is not.
 */
export function multiplierBadge(value, threshold = 3) {
  if (value === null || value === undefined) return badge('—');
  return el('span', { class: `badge ${value >= threshold ? 'accent' : ''} multiplier` },
    `${value.toFixed(1)}×`);
}

/** Run an async action with a button spinner and automatic error reporting. */
export async function withBusy(button, label, fn) {
  const original = button.textContent;
  const wasDisabled = button.disabled;
  button.disabled = true;
  clear(button);
  button.append(el('div', { class: 'spinner' }), document.createTextNode(label));
  try {
    return await fn();
  } catch (err) {
    toastError(err);
    return undefined;
  } finally {
    button.disabled = wasDisabled;
    clear(button);
    button.textContent = original;
  }
}

/** Debounce, for filter inputs that shouldn't refetch on every keystroke. */
export function debounce(fn, ms = 300) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/** A thumbnail that degrades to a blank tile instead of a broken-image icon.
 *
 * YouTube genuinely 404s thumbnails for deleted and private videos, and a page
 * full of broken-image glyphs reads as a bug in this app rather than a fact
 * about the video.
 */
export function thumb(url, size = '') {
  if (!url) return el('div', { class: `thumb ${size}` });
  const img = el('img', { class: `thumb ${size}`, src: url, alt: '', loading: 'lazy' });
  img.addEventListener('error', () => img.replaceWith(el('div', { class: `thumb ${size}` })),
    { once: true });
  return img;
}

export function videoCell(video, { size = '' } = {}) {
  return el('div', { class: 'video-cell' },
    thumb(video.thumbnail_url, size),
    el('div', { class: 'meta' },
      el('a', { class: 'title', href: video.url || `https://www.youtube.com/watch?v=${video.video_id || video.id}`,
                target: '_blank', rel: 'noopener', text: video.title || '(untitled)' }),
      el('div', { class: 'sub', text: [video.channel_title, video.published_at ? ago(video.published_at) : null]
        .filter(Boolean).join(' · ') }),
    ),
  );
}
