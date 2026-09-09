/* Packaging — thumbnails and titles.
 *
 * Two tabs over the same idea: what do the videos that beat their baseline have
 * in common, and how does your own work differ?
 *
 * Nothing here produces a composite "packaging score". A single number would
 * hide the only thing that's actionable — *which* attribute differs and by how
 * much — and would imply a precision this evidence doesn't have.
 */

import { api } from '../api.js';
import { state, refreshChannels } from '../app.js';
import {
  el, clear, compact, full, pct, empty, notice, loading, toast, toastError,
  badge, withBusy, videoCell, multiplierBadge,
} from '../ui.js';

let tab = 'thumbnails';
let minMultiplier = 3;

export async function render(view) {
  await refreshChannels();
  const body = el('div', { class: 'stack' });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Packaging' }),
      el('p', { class: 'lede', text:
        'What the breakouts in your niche have in common, visually and verbally — and ' +
        'where your own titles and thumbnails sit against them.' }))));

  const tabs = el('div', { class: 'tabs' },
    tabButton('thumbnails', 'Thumbnails', () => load()),
    tabButton('titles', 'Titles', () => load()));

  const threshold = el('select', {},
    [['2', '2× and up'], ['3', '3× and up'], ['5', '5× and up'], ['10', '10× and up']]
      .map(([v, l]) => el('option', { value: v, selected: String(minMultiplier) === v, text: l })));
  threshold.onchange = () => { minMultiplier = Number(threshold.value); load(); };

  view.append(el('div', { class: 'filters' },
    el('div', { class: 'field' }, el('label', { text: 'View' }), tabs),
    el('div', { class: 'field' }, el('label', { text: 'Count as a breakout at' }), threshold),
    el('div', { class: 'spacer' })));

  view.append(body);

  async function load() {
    clear(body);
    body.append(loading());
    try {
      if (tab === 'thumbnails') body.replaceChildren(...await thumbnailsView(load));
      else body.replaceChildren(...await titlesView());
    } catch (err) {
      clear(body);
      body.append(notice('critical', 'Could not load', err.message, err.hint));
      toastError(err);
    }
  }

  function tabButton(key, label, onClick) {
    const btn = el('button', { class: `tab ${tab === key ? 'on' : ''}`, text: label });
    btn.onclick = () => {
      tab = key;
      for (const other of tabs.querySelectorAll('.tab')) other.classList.remove('on');
      btn.classList.add('on');
      onClick();
    };
    return btn;
  }

  await load();
}

/* ----------------------------------------------------------- thumbnails */

async function thumbnailsView(reload) {
  const [patterns, outliers, stored] = await Promise.all([
    api.get('/api/thumbnails/patterns', { min_multiplier: minMultiplier }),
    api.get('/api/outliers', { min_multiplier: minMultiplier, limit: 24 }),
    api.get('/api/thumbnails/analyses', { min_multiplier: minMultiplier }),
  ]);
  const analyses = stored.results || [];

  const nodes = [];
  const high = patterns.high_performers;

  if (!outliers.results.length) {
    return [empty({
      title: 'No breakouts to learn from yet',
      message: `Nothing is at ${minMultiplier}× or above. Lower the threshold, or track ` +
               `more channels so there is more to compare.`,
      actions: [{ label: 'Add channels', variant: 'primary',
        onClick: () => { window.location.hash = '#/channels'; } }],
    })];
  }

  const unanalysed = outliers.results.filter((r) => r.thumbnail_url);
  const hasVision = state.status?.settings?.anthropic_api_key_set;
  const analyseBtn = el('button', { class: 'btn primary' },
    `Analyse ${unanalysed.length} thumbnails`);
  analyseBtn.onclick = () => withBusy(analyseBtn, 'Analysing…', async () => {
    const result = await api.post('/api/thumbnails/analyse', {
      video_ids: unanalysed.map((r) => r.video_id), use_vision: true,
    });
    toast(
      `Analysed ${result.analysed}. ${result.vision_calls} new AI call${result.vision_calls === 1 ? '' : 's'}` +
      `${result.vision_available ? '' : ' — no Anthropic key, so only local measurements ran'}.`,
      { kind: 'good' });
    if (result.errors?.length) {
      toast(`${result.errors.length} thumbnail(s) could not be fetched.`, { kind: 'warning' });
    }
    await reload();
  });

  nodes.push(el('div', { class: 'between mb-sm' },
    el('div', {},
      el('h2', { text: 'The visual formula' }),
      el('p', { class: 'small muted', text:
        high.sample_size
          ? `Measured across ${high.sample_size} analysed thumbnail${high.sample_size === 1 ? '' : 's'}` +
            (high.vision_sample_size ? `, ${high.vision_sample_size} with AI breakdown` : ', local measurements only')
          : 'Nothing analysed yet' })),
    analyseBtn));

  if (!hasVision) {
    // Say plainly that this screen works unpaid. Without this, "analyse"
    // beside an unset API key reads as a paywall, and it isn't one.
    nodes.push(notice('info', 'This works without an API key',
      'Colour, contrast, brightness and busyness are measured on your own machine, ' +
      'and every title metric is computed locally. An Anthropic key is optional — it ' +
      'adds a written description of what each thumbnail shows (faces, expression, ' +
      'overlaid text, legibility at sidebar size), billed per image and cached forever.'));
  }

  if (!high.sample_size) {
    nodes.push(el('div', { class: 'card' }, el('div', { class: 'card-body' },
      empty({
        title: 'Nothing analysed yet',
        message: hasVision
          ? 'Analysing measures colour, contrast and busyness locally, and describes what ' +
            'each thumbnail shows. Each image is analysed once and cached forever.'
          : 'Analysing measures colour, contrast, brightness and busyness on your own ' +
            'machine — free, and the metrics that actually predict whether a thumbnail ' +
            'survives being shrunk to sidebar size.',
        actions: [{ label: 'Analyse now', variant: 'primary', onClick: () => analyseBtn.click() }],
      }))));
  } else {
    nodes.push(el('div', { class: 'grid cols-2' },
      patternCard('Breakouts', high, 'series-1'),
      patternCard('Everything else', patterns.everything_else, 'series-3')));

    if (high.palette?.length) {
      nodes.push(el('div', { class: 'card' },
        el('div', { class: 'card-head' },
          el('div', {}, el('h2', { text: 'Colours that show up in breakouts' }),
            el('div', { class: 'sub', text: 'Dominant colours pooled across the analysed set' }))),
        el('div', { class: 'card-body' },
          el('div', { class: 'palette' },
            high.palette.map((hex) => el('span', { class: 'sw', style: { background: hex }, title: hex }))))));
    }
  }

  // The legibility view: every analysed thumbnail rendered at the size it is
  // actually chosen at, worst first. Reading a number about an image is far
  // less convincing than seeing the image lose its detail.
  if (analyses.length) nodes.push(legibilityCard(analyses, outliers.results));

  nodes.push(el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'The breakout thumbnails' }),
        el('div', { class: 'sub', text: 'The sample every pattern above is drawn from' }))),
    el('div', { class: 'card-body' },
      el('div', { class: 'thumb-grid' },
        outliers.results.map((r) => el('a', {
          class: 'thumb-tile', href: r.url, target: '_blank', rel: 'noopener', title: r.title,
        },
          tileImage(r.thumbnail_url),
          el('div', { class: 'thumb-tile-meta' },
            multiplierBadge(r.multiplier, outliers.threshold),
            el('span', { class: 'small muted', text: compact(r.views) }))))))));

  return nodes;
}

/* Sizes a thumbnail is genuinely chosen at. Rendering the real image at these
 * dimensions is not a simulation — it is the same downscale the browser does
 * on YouTube itself. */
const DISPLAY_W = 210, MOBILE_W = 168;

function legibilityCard(analyses, results) {
  const byId = new Map(results.map((r) => [r.video_id, r]));

  // Worst first, ranked by how much survives the downscale.
  const ranked = analyses
    .filter((a) => a.detail_retention !== null && a.detail_retention !== undefined)
    .sort((a, b) => a.detail_retention - b.detail_retention);

  if (!ranked.length) return null;

  const row = (a) => {
    const video = byId.get(a.video_id) || {};
    const notes = [];
    if (a.detail_retention < 0.45) {
      notes.push(`Only ${Math.round(a.detail_retention * 100)}% of its detail survives this size.`);
    }
    if (a.small_contrast !== null && a.small_contrast < 38) {
      notes.push(`Contrast collapses when scaled down (${a.small_contrast.toFixed(0)} here vs ${(a.contrast || 0).toFixed(0)} full size).`);
    }
    if (a.text_area_estimate > 0.30) {
      notes.push(`Text-like detail covers about ${Math.round(a.text_area_estimate * 100)}% of the frame.`);
    }
    if ((a.composition_note || '').startsWith('far ')) {
      notes.push(`Visual weight sits ${a.composition_note}.`);
    }

    // The cached copy, not the live URL: these pictures sit beside numbers
    // measured from that exact file, and a since-replaced thumbnail would make
    // the pairing a lie.
    const cached = `/api/thumbnails/image?url=${encodeURIComponent(a.thumbnail_url)}`;

    return el('div', { class: 'legibility-row' },
      el('div', { class: 'legibility-sizes' },
        el('figure', {},
          tileImage(cached, `${DISPLAY_W}px`),
          el('figcaption', { class: 'small muted', text: `${DISPLAY_W}px — desktop` })),
        el('figure', {},
          tileImage(cached, `${MOBILE_W}px`),
          el('figcaption', { class: 'small muted', text: `${MOBILE_W}px — phone` }))),
      el('div', { class: 'legibility-meta' },
        el('a', { class: 'title', href: video.url || '#', target: '_blank', rel: 'noopener',
                  text: video.title || a.video_id }),
        el('div', { class: 'row wrap mt-sm' },
          metricChip('Detail kept', `${Math.round((a.detail_retention || 0) * 100)}%`,
            a.detail_retention < 0.45),
          metricChip('Contrast at size', (a.small_contrast || 0).toFixed(0),
            (a.small_contrast || 0) < 38),
          metricChip('Text-like area', `${Math.round((a.text_area_estimate || 0) * 100)}%`,
            (a.text_area_estimate || 0) > 0.30),
          metricChip('Weight', a.composition_note || '—',
            (a.composition_note || '').startsWith('far '))),
        notes.length
          ? el('ul', { class: 'legibility-notes' },
              notes.map((n) => el('li', { class: 'small dim', text: n })))
          : el('p', { class: 'small muted mt-sm',
              text: 'Nothing measurably wrong at display size.' })));
  };

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {},
        el('h2', { text: 'How they hold up at real size' }),
        el('div', { class: 'sub', text:
          'The same images at the sizes YouTube actually shows them, worst first. ' +
          'All measured on your machine — no API key involved.' }))),
    el('div', { class: 'card-body' }, ranked.slice(0, 12).map(row)));
}

function metricChip(label, value, bad) {
  return el('span', { class: `metric-chip ${bad ? 'bad' : ''}` },
    el('span', { class: 'metric-label', text: label }),
    el('span', { class: 'metric-value', text: String(value) }));
}

/** Grid tile image that falls back to an empty tile rather than a broken glyph.
 *
 * ``width`` renders the real image at a real display size — the browser does
 * the same downscale YouTube's page does, so this is the actual thing rather
 * than an approximation of it.
 */
function tileImage(url, width) {
  const style = width ? { width, flex: `0 0 ${width}` } : null;
  if (!url) return el('div', { class: 'tile-blank', style });
  const img = el('img', { src: url, alt: '', loading: 'lazy', style });
  img.addEventListener('error',
    () => img.replaceWith(el('div', { class: 'tile-blank', style })), { once: true });
  return img;
}

function patternCard(title, summary, colorVar) {
  if (!summary.sample_size) {
    return el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: title }))),
      el('div', { class: 'card-body' },
        el('p', { class: 'muted small', text: 'Nothing analysed in this group yet.' })));
  }

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: title }),
        el('div', { class: 'sub', text: `${summary.sample_size} thumbnails` }))),
    el('div', { class: 'card-body' },
      summary.patterns.map((p) => el('div', { class: 'pattern-row' },
        el('div', { class: 'pattern-label' },
          el('div', { text: p.label }),
          p.note ? el('div', { class: 'pattern-note', text: p.note }) : null),
        el('div', { class: 'pattern-track' },
          el('div', { class: 'pattern-fill',
            style: { width: `${p.share}%`, background: `var(--${colorVar})` } })),
        el('div', { class: 'pattern-value', text: `${p.share}%` }))),
      summary.median_clarity !== null && summary.median_clarity !== undefined
        ? el('div', { class: 'mt-md small dim',
            text: `Median legibility at sidebar size: ${summary.median_clarity}/100.` })
        : null));
}

/* --------------------------------------------------------------- titles */

async function titlesView() {
  const data = await api.get('/api/titles/compare', { min_multiplier: minMultiplier });
  const nodes = [];

  if (!data.outlier_sample) {
    return [empty({
      title: 'No breakout titles yet',
      message: `Nothing at ${minMultiplier}× or above to learn from. Lower the threshold or track more channels.`,
    })];
  }

  if (!data.has_own_channel) {
    nodes.push(notice('info', 'Link your channel to see the comparison',
      'The patterns below describe what works in your niche. Linking your own channel ' +
      'adds the half that matters — how your titles differ from them.',
      'Settings → Your channel.'));
  } else if (!data.comparisons.length) {
    nodes.push(notice('warning', 'Not enough titles to compare yet',
      `Comparison needs at least five titles on each side; there are ${data.outlier_sample} ` +
      `breakouts and ${data.own_sample} of yours. Reporting a difference from fewer would ` +
      `be noise dressed as insight.`));
  }

  if (data.comparisons.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {}, el('h2', { text: 'Your titles vs the breakouts' }),
          el('div', { class: 'sub', text:
            `${data.own_sample} of yours against ${data.outlier_sample} breakouts` }))),
      el('div', { class: 'card-body' },
        data.comparisons.map((c) => el('div', { class: 'compare-row' },
          el('div', { class: 'compare-label', text: c.label }),
          el('div', { class: 'compare-values' },
            el('span', { class: 'tnum', text: fmtValue(c.metric, c.subject_value) }),
            el('span', { class: 'muted small', text: 'you' }),
            el('span', { class: 'compare-arrow', text: '·' }),
            el('span', { class: 'tnum', text: fmtValue(c.metric, c.reference_value) }),
            el('span', { class: 'muted small', text: 'breakouts' })),
          el('div', { class: 'compare-reading small dim', text: c.reading }))))));
  }

  nodes.push(el('div', { class: 'grid cols-2' },
    listCard('Breakout title openings', data.outlier_openings,
      'The first two words. Formula is usually the most copyable thing about a set of winning titles.',
      (o) => `“${o.phrase}…”`, (o) => `${o.count}×`),
    listCard('Words that recur in breakouts', data.outlier_terms,
      'Content words, stopwords removed.',
      (t) => t.term, (t) => `${t.count}×`)));

  if (data.has_own_channel) {
    nodes.push(el('div', { class: 'grid cols-2' },
      listCard('Your title openings', data.own_openings, '', (o) => `“${o.phrase}…”`, (o) => `${o.count}×`),
      listCard('Words that recur in yours', data.own_terms, '', (t) => t.term, (t) => `${t.count}×`)));
  }

  return nodes;
}

function fmtValue(metric, value) {
  const rates = ['has_number', 'has_brackets', 'has_question', 'truncation_risk'];
  return rates.includes(metric) ? `${value.toFixed(0)}%` : value.toFixed(1);
}

function listCard(title, items, sub, label, value) {
  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: title }),
        sub ? el('div', { class: 'sub', text: sub }) : null)),
    el('div', { class: 'card-body tight' },
      items?.length
        ? items.map((item) => el('div', { class: 'job-row' },
            el('div', { class: 'truncate', text: label(item) }),
            el('div', { class: 'small muted tnum', text: value(item) })))
        : el('p', { class: 'muted small', style: { padding: '8px' },
            text: 'Nothing repeated often enough to call a pattern.' })));
}
