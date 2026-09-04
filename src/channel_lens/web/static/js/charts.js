/* Inline SVG charts. No library — these are a few dozen lines each, and a
 * charting dependency would cost more in bundle and API surface than it saves.
 *
 * Mark specs are fixed and deliberate: 2px lines, area washes at 10% opacity,
 * markers at r>=4 wearing a 2px surface ring so they stay legible where they
 * overlap, bars capped at 24px with a 4px rounded data-end and a square
 * baseline, and hairline *solid* gridlines one step off the surface. Dashed
 * gridlines read as thresholds; thick saturated blocks read as loud.
 *
 * Every chart ships a hover layer and a table-view twin, so no value is ever
 * reachable only by hovering.
 */

import { el, clear, compact, full, pct } from './ui.js';

const NS = 'http://www.w3.org/2000/svg';

function s(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
}

/** Round to a clean axis number so ticks read 0 / 1,000 / 2,000. */
function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalised = value / magnitude;
  const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  return step * magnitude;
}

/* ------------------------------------------------------------- tooltips */

function makeTooltip(container) {
  const tip = el('div', {
    class: 'chart-tip', hidden: true, role: 'tooltip',
  });
  container.append(tip);
  return {
    show(x, y, nodes) {
      clear(tip);
      tip.append(...nodes);
      tip.hidden = false;
      const bounds = container.getBoundingClientRect();
      const width = tip.offsetWidth;
      // Flip before the tip would leave the card, rather than after.
      const left = Math.min(Math.max(4, x - width / 2), bounds.width - width - 4);
      tip.style.left = `${left}px`;
      tip.style.top = `${Math.max(4, y - tip.offsetHeight - 12)}px`;
    },
    hide() { tip.hidden = true; },
  };
}

function tipRow(label, value) {
  return el('div', { class: 'tip-row' },
    el('span', { class: 'tip-label', text: label }),
    el('span', { class: 'tip-value', text: value }));
}

/* ----------------------------------------------------------- line chart */

/**
 * Single-series time series. One series means no legend box — the card title
 * already names what is plotted, and a one-swatch legend just restates it.
 */
export function lineChart(points, {
  height = 200, valueLabel = 'Views', formatValue = full, color = 'var(--series-1)',
  annotations = [],
} = {}) {
  const wrap = el('div', { class: 'chart' });
  if (!points || points.length < 2) {
    wrap.append(el('div', { class: 'chart-empty', text: 'Not enough data points yet to draw a trend.' }));
    return wrap;
  }

  const pad = { top: 12, right: 14, bottom: 26, left: 52 };
  const width = 720;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const xs = points.map((p) => new Date(p.x).getTime());
  const ys = points.map((p) => p.y);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMax = niceMax(Math.max(...ys));
  const xSpan = xMax - xMin || 1;

  const px = (t) => pad.left + ((t - xMin) / xSpan) * plotW;
  const py = (v) => pad.top + plotH - (v / yMax) * plotH;

  const svg = s('svg', {
    viewBox: `0 0 ${width} ${height}`, class: 'chart-svg',
    preserveAspectRatio: 'none', role: 'img',
    'aria-label': `${valueLabel} over time`,
  });

  // Gridlines + y ticks: hairline, solid, recessive.
  for (let i = 0; i <= 4; i++) {
    const value = (yMax / 4) * i;
    const y = py(value);
    svg.append(s('line', {
      x1: pad.left, x2: width - pad.right, y1: y, y2: y,
      stroke: 'var(--grid)', 'stroke-width': 1, 'shape-rendering': 'crispEdges',
    }));
    svg.append(s('text', {
      x: pad.left - 8, y: y + 3.5, class: 'axis-text', 'text-anchor': 'end',
    }, compact(value)));
  }

  const linePath = points.map((p, i) => `${i ? 'L' : 'M'}${px(new Date(p.x).getTime())},${py(p.y)}`).join(' ');
  const areaPath = `${linePath} L${px(xMax)},${py(0)} L${px(xMin)},${py(0)} Z`;

  svg.append(s('path', { d: areaPath, fill: color, 'fill-opacity': 0.1 }));
  svg.append(s('path', {
    d: linePath, fill: 'none', stroke: color, 'stroke-width': 2,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // Vertical markers for events (a title or thumbnail change).
  for (const note of annotations) {
    const x = px(new Date(note.x).getTime());
    if (x < pad.left || x > width - pad.right) continue;
    svg.append(s('line', {
      x1: x, x2: x, y1: pad.top, y2: pad.top + plotH,
      stroke: 'var(--series-2)', 'stroke-width': 1.5, 'stroke-opacity': .75,
    }));
    svg.append(s('circle', {
      cx: x, cy: pad.top - 1, r: 3.5, fill: 'var(--series-2)',
      stroke: 'var(--surface)', 'stroke-width': 2,
    }));
  }

  // End marker: >=8px with a 2px surface ring.
  const last = points[points.length - 1];
  svg.append(s('circle', {
    cx: px(new Date(last.x).getTime()), cy: py(last.y), r: 4.5,
    fill: color, stroke: 'var(--surface)', 'stroke-width': 2,
  }));

  // x-axis endpoints only — a label per point would be unreadable.
  svg.append(s('text', { x: pad.left, y: height - 8, class: 'axis-text' },
    new Date(xMin).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })));
  svg.append(s('text', { x: width - pad.right, y: height - 8, class: 'axis-text', 'text-anchor': 'end' },
    new Date(xMax).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })));

  // Crosshair + nearest-point tooltip across the whole plot band, so the hit
  // target is the full column rather than the 9px dot.
  const cursor = s('line', {
    y1: pad.top, y2: pad.top + plotH, stroke: 'var(--axis)', 'stroke-width': 1, opacity: 0,
  });
  const hoverDot = s('circle', {
    r: 4.5, fill: color, stroke: 'var(--surface)', 'stroke-width': 2, opacity: 0,
  });
  svg.append(cursor, hoverDot);

  const overlay = s('rect', {
    x: pad.left, y: pad.top, width: plotW, height: plotH, fill: 'transparent',
  });
  svg.append(overlay);
  wrap.append(svg);
  const tip = makeTooltip(wrap);

  const move = (event) => {
    const box = svg.getBoundingClientRect();
    const ratio = width / box.width;
    const localX = (event.clientX - box.left) * ratio;
    const t = xMin + ((localX - pad.left) / plotW) * xSpan;
    let nearest = points[0], best = Infinity;
    for (const p of points) {
      const d = Math.abs(new Date(p.x).getTime() - t);
      if (d < best) { best = d; nearest = p; }
    }
    const nx = px(new Date(nearest.x).getTime());
    const ny = py(nearest.y);
    cursor.setAttribute('x1', nx); cursor.setAttribute('x2', nx); cursor.setAttribute('opacity', 1);
    hoverDot.setAttribute('cx', nx); hoverDot.setAttribute('cy', ny); hoverDot.setAttribute('opacity', 1);
    tip.show(nx / ratio, ny / ratio, [
      el('div', { class: 'tip-title', text: new Date(nearest.x).toLocaleString(undefined,
        { month: 'short', day: 'numeric', hour: 'numeric' }) }),
      tipRow(valueLabel, formatValue(nearest.y)),
    ]);
  };

  svg.addEventListener('pointermove', move);
  svg.addEventListener('pointerleave', () => {
    cursor.setAttribute('opacity', 0);
    hoverDot.setAttribute('opacity', 0);
    tip.hide();
  });

  return wrap;
}

/* ------------------------------------------------------- horizontal bar */

/**
 * One measure across named categories. One series, so one colour for every bar
 * — colouring darker-where-bigger would double-encode the length the bar
 * already shows. `highlight` marks a semantic subset in a second slot.
 */
export function barChart(rows, {
  formatValue = full, highlightLabel = '', barHeight = 22, gap = 10,
} = {}) {
  const wrap = el('div', { class: 'chart' });
  if (!rows || !rows.length) {
    wrap.append(el('div', { class: 'chart-empty', text: 'No data to show yet.' }));
    return wrap;
  }

  const labelW = 168, valueW = 74, padX = 6;
  const width = 720;
  const plotW = width - labelW - valueW - padX * 2;
  const height = rows.length * (barHeight + gap) + gap;
  const max = Math.max(...rows.map((r) => r.value), 1);

  const svg = s('svg', {
    viewBox: `0 0 ${width} ${height}`, class: 'chart-svg', role: 'img',
    'aria-label': 'Breakdown by category',
  });

  rows.forEach((row, i) => {
    const y = gap + i * (barHeight + gap);
    const w = Math.max(2, (row.value / max) * plotW);
    const color = row.highlight ? 'var(--series-1)' : 'var(--series-3)';
    const r = Math.min(4, w / 2);

    svg.append(s('text', {
      x: labelW - 10, y: y + barHeight / 2 + 4, class: 'axis-text strong', 'text-anchor': 'end',
    }, row.label));

    // Square at the baseline, 4px rounded at the data end.
    svg.append(s('path', {
      d: `M${labelW},${y} H${labelW + w - r} a${r},${r} 0 0 1 ${r},${r}` +
         ` V${y + barHeight - r} a${r},${r} 0 0 1 ${-r},${r} H${labelW} Z`,
      fill: color,
    }));

    svg.append(s('text', {
      x: labelW + w + 9, y: y + barHeight / 2 + 4, class: 'axis-text tnum',
    }, formatValue(row.value)));
  });

  wrap.append(svg);

  if (highlightLabel) {
    wrap.append(el('div', { class: 'legend' },
      el('span', { class: 'legend-item' },
        el('span', { class: 'legend-swatch', style: { background: 'var(--series-1)' } }),
        highlightLabel),
      el('span', { class: 'legend-item' },
        el('span', { class: 'legend-swatch', style: { background: 'var(--series-3)' } }),
        'Everything else'),
    ));
  }
  return wrap;
}

/* -------------------------------------------------------- quadrant plot */

const QUADRANT_COLOR = {
  working:    'var(--good)',
  packaging:  'var(--warning)',
  overpromise:'var(--serious)',
  topic:      'var(--critical)',
  unknown:    'var(--ink-muted)',
};

/**
 * CTR against retention, split by the channel's own medians.
 *
 * Colour here is the reserved *status* palette rather than categorical slots,
 * because these four values genuinely are states with a valence, not arbitrary
 * series. That also sidesteps the all-pairs colourblind limit that applies to
 * scatter plots — and position already encodes the quadrant, so colour is
 * redundant reinforcement rather than the sole channel.
 */
export function quadrantChart(items, { medianCtr, medianRetention, onSelect } = {}) {
  const wrap = el('div', { class: 'chart' });
  const points = (items || []).filter(
    (d) => d.ctr !== null && d.ctr !== undefined &&
           d.average_view_percentage !== null && d.average_view_percentage !== undefined);

  if (!points.length || medianCtr === null || medianCtr === undefined) {
    wrap.append(el('div', { class: 'chart-empty',
      text: 'Needs CTR and retention for at least three videos before the quadrants mean anything.' }));
    return wrap;
  }

  const pad = { top: 16, right: 18, bottom: 40, left: 54 };
  const width = 720, height = 380;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const xMax = niceMax(Math.max(...points.map((d) => d.average_view_percentage), medianRetention * 1.3));
  const yMax = niceMax(Math.max(...points.map((d) => d.ctr), medianCtr * 1.3));

  const px = (v) => pad.left + (v / xMax) * plotW;
  const py = (v) => pad.top + plotH - (v / yMax) * plotH;

  const svg = s('svg', {
    viewBox: `0 0 ${width} ${height}`, class: 'chart-svg', role: 'img',
    'aria-label': 'Click-through rate against average view percentage',
  });

  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (plotH / 4) * i;
    svg.append(s('line', { x1: pad.left, x2: width - pad.right, y1: y, y2: y,
      stroke: 'var(--grid)', 'stroke-width': 1, 'shape-rendering': 'crispEdges' }));
    svg.append(s('text', { x: pad.left - 8, y: y + 3.5, class: 'axis-text', 'text-anchor': 'end' },
      `${(yMax - (yMax / 4) * i).toFixed(1)}%`));
  }
  for (let i = 0; i <= 4; i++) {
    const x = pad.left + (plotW / 4) * i;
    svg.append(s('text', { x, y: height - 20, class: 'axis-text', 'text-anchor': 'middle' },
      `${((xMax / 4) * i).toFixed(0)}%`));
  }

  // Median reference lines — the axes the quadrants are actually defined by.
  const mx = px(medianRetention), my = py(medianCtr);
  svg.append(s('line', { x1: mx, x2: mx, y1: pad.top, y2: pad.top + plotH,
    stroke: 'var(--axis)', 'stroke-width': 1.5 }));
  svg.append(s('line', { x1: pad.left, x2: width - pad.right, y1: my, y2: my,
    stroke: 'var(--axis)', 'stroke-width': 1.5 }));
  svg.append(s('text', { x: mx + 6, y: pad.top + 11, class: 'axis-text' },
    `median retention ${medianRetention.toFixed(0)}%`));
  svg.append(s('text', { x: width - pad.right, y: my - 6, class: 'axis-text', 'text-anchor': 'end' },
    `median CTR ${medianCtr.toFixed(1)}%`));

  svg.append(s('text', { x: pad.left + plotW / 2, y: height - 4, class: 'axis-text strong',
    'text-anchor': 'middle' }, 'Average view percentage  →'));
  svg.append(s('text', { x: 12, y: pad.top + plotH / 2, class: 'axis-text strong',
    'text-anchor': 'middle', transform: `rotate(-90 12 ${pad.top + plotH / 2})` }, 'Click-through rate  →'));

  const tip = makeTooltip(wrap);

  for (const d of points) {
    const cx = px(d.average_view_percentage), cy = py(d.ctr);
    // Marker >=8px with a 2px surface ring so overlaps stay readable.
    const dot = s('circle', {
      cx, cy, r: 5.5, fill: QUADRANT_COLOR[d.quadrant] || 'var(--ink-muted)',
      stroke: 'var(--surface)', 'stroke-width': 2, class: 'dot',
      tabindex: '0', role: 'button', 'aria-label': `${d.title}: CTR ${d.ctr}%`,
    });
    // A generous invisible hit target — never make the user land on the dot.
    const hit = s('circle', { cx, cy, r: 13, fill: 'transparent', style: 'cursor:pointer' });

    const show = () => {
      const box = svg.getBoundingClientRect();
      const ratio = box.width / width;
      dot.setAttribute('r', 7);
      tip.show(cx * ratio, cy * ratio, [
        el('div', { class: 'tip-title', text: d.title }),
        tipRow('CTR', pct(d.ctr)),
        tipRow('Retention', pct(d.average_view_percentage, 0)),
        tipRow('Impressions', compact(d.impressions)),
        el('div', { class: 'tip-note', text: d.headline }),
      ]);
    };
    const hide = () => { dot.setAttribute('r', 5.5); tip.hide(); };

    hit.addEventListener('pointerenter', show);
    hit.addEventListener('pointerleave', hide);
    dot.addEventListener('focus', show);
    dot.addEventListener('blur', hide);
    if (onSelect) hit.addEventListener('click', () => onSelect(d));

    svg.append(dot, hit);
  }

  wrap.append(svg);
  wrap.append(el('div', { class: 'legend' },
    Object.entries({
      packaging: 'Packaging holding it back', working: 'Working',
      overpromise: 'Overpromising', topic: 'Topic or execution',
    }).map(([key, label]) => el('span', { class: 'legend-item' },
      el('span', { class: 'legend-swatch', style: { background: QUADRANT_COLOR[key] } }), label)),
  ));

  return wrap;
}

/* ------------------------------------------------------------ sparkline */

export function sparkline(values, { width = 92, height = 26, color = 'var(--series-1)' } = {}) {
  if (!values || values.length < 2) return el('span', { class: 'muted small', text: '—' });
  const max = Math.max(...values), min = Math.min(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const d = values.map((v, i) =>
    `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(height - 2 - ((v - min) / span) * (height - 4)).toFixed(1)}`
  ).join(' ');
  const svg = s('svg', { viewBox: `0 0 ${width} ${height}`, class: 'sparkline',
    width, height, 'aria-hidden': 'true' });
  svg.append(s('path', { d, fill: 'none', stroke: color, 'stroke-width': 2,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
  return svg;
}

/* ----------------------------------------------------------- table twin */

/** Every chart needs a WCAG-clean equivalent; this builds the toggle for one. */
export function withTableView(chartNode, { columns, rows, label = 'Table view' }) {
  const table = el('div', { class: 'table-wrap', hidden: true },
    el('table', {},
      el('thead', {}, el('tr', {}, columns.map((c) =>
        el('th', { class: c.num ? 'num' : '', text: c.label })))),
      el('tbody', {}, rows.map((row) =>
        el('tr', {}, columns.map((c) =>
          el('td', { class: c.num ? 'num' : '', text: c.get(row) })))))),
  );

  const toggle = el('button', { class: 'btn ghost sm', onClick: () => {
    const showing = table.hidden;
    table.hidden = !showing;
    chartNode.hidden = showing;
    toggle.textContent = showing ? 'Chart view' : label;
  } }, label);

  return { table, toggle };
}
