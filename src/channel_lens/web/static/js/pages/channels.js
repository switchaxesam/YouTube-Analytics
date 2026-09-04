/* Channels — the watchlist of competitors.
 *
 * Adding a channel spends quota, so the cost is estimated and shown *before*
 * the request goes out. Guessing after the fact is how a day's budget
 * disappears into one careless import.
 */

import { api } from '../api.js';
import { refreshChannels, refreshStatus, state } from '../app.js';
import {
  el, clear, compact, full, ago, empty, notice, loading, toast, toastError,
  modal, withBusy, badge, debounce,
} from '../ui.js';

export async function render(view) {
  const list = el('div', { class: 'stack' });

  view.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Channels' }),
      el('p', { class: 'lede', text:
        'Competitors whose uploads Channel Lens keeps a copy of. Each one gets its own ' +
        'performance baseline, which is what every multiplier in the app is measured ' +
        'against.' })),
    el('div', { class: 'page-actions' },
      el('button', { class: 'btn primary', onClick: () => addDialog(reload) }, 'Add channel'))));

  view.append(list);

  async function reload() {
    clear(list);
    list.append(loading());
    await refreshChannels();
    clear(list);
    list.append(...renderChannels(reload));
  }

  await reload();
}

function renderChannels(reload) {
  const channels = state.channels;
  if (!channels.length) {
    return [empty({
      title: 'No channels tracked yet',
      message: 'Add three to five channels in your niche — ideally a mix of sizes. ' +
               'Importing 50 uploads from a channel costs about 3 quota units out of ' +
               'your 10,000 a day, so this is cheap.',
      actions: [{ label: 'Add your first channel', variant: 'primary',
        onClick: () => addDialog(reload) }],
    })];
  }

  const owned = channels.filter((c) => c.is_owned);
  const tracked = channels.filter((c) => !c.is_owned);
  const nodes = [];

  if (owned.length) {
    nodes.push(el('div', { class: 'card' },
      el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: 'Your channel' }))),
      el('div', { class: 'card-body tight' },
        owned.map((c) => channelRow(c, reload)))));
  }

  nodes.push(el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Tracked channels' }),
        el('div', { class: 'sub', text: `${tracked.length} channel${tracked.length === 1 ? '' : 's'}` }))),
    tracked.length
      ? el('div', { class: 'card-body tight' }, tracked.map((c) => channelRow(c, reload)))
      : el('div', { class: 'card-body' },
          el('p', { class: 'muted small', text: 'None yet.' }))));

  return nodes;
}

function channelRow(channel, reload) {
  const refresh = el('button', { class: 'btn ghost sm', text: 'Refresh' });
  refresh.onclick = () => withBusy(refresh, 'Refreshing…', async () => {
    const result = await api.post(`/api/channels/${channel.id}/refresh`, null,
      { params: { video_limit: 50 } });
    await refreshStatus();
    toast(`${result.videos_refreshed} videos refreshed${
      result.changes_detected ? `, ${result.changes_detected} change(s) spotted` : ''}. ` +
      `${result.units_spent} units spent.`, { kind: 'good' });
    await reload();
  });

  const remove = el('button', { class: 'btn ghost sm', text: 'Remove' });
  remove.onclick = () => removeDialog(channel, reload);

  return el('div', { class: 'channel-row' },
    channel.thumbnail_url
      ? el('img', { class: 'avatar', src: channel.thumbnail_url, alt: '', loading: 'lazy' })
      : el('div', { class: 'avatar' }),
    el('div', { class: 'channel-meta' },
      el('div', { class: 'row' },
        el('a', { class: 'channel-name', href: channel.url, target: '_blank', rel: 'noopener',
          text: channel.title || channel.id }),
        channel.is_owned ? badge('Yours', 'accent') : null),
      el('div', { class: 'small muted', text: [
        channel.subscriber_count_hidden
          ? 'subscribers hidden'
          : `${compact(channel.subscriber_count)} subscribers`,
        `${full(channel.stored_videos)} videos stored`,
        channel.latest_upload ? `last upload ${ago(channel.latest_upload)}` : null,
      ].filter(Boolean).join(' · ') })),
    el('div', { class: 'row' }, refresh, channel.is_owned ? null : remove));
}

/* ------------------------------------------------------------ add flow */

/* One dialog handles both a single channel and a pasted list.
 *
 * There is no separate "bulk" mode to choose: a textarea accepts one reference
 * or fifty, and the preview underneath adapts. Making the user pick the right
 * mode before knowing how many channels they have is a decision the app can
 * make for itself by counting lines.
 */
function addDialog(reload) {
  const input = el('textarea', {
    autofocus: true, rows: 5, spellcheck: false,
    placeholder: '@mkbhd\nhttps://youtube.com/@LinusTechTips\nUCXuqSBlHAE6Xw-yeJA0Tunw\n\nOne per line, or comma separated.',
  });

  const limit = el('select', {},
    [['25', '25 uploads'], ['50', '50 uploads (recommended)'],
     ['100', '100 uploads'], ['200', '200 uploads'], ['500', '500 uploads']]
      .map(([v, label]) => el('option', { value: v, selected: v === '50', text: label })));

  const skipExisting = el('input', { type: 'checkbox', checked: true });
  const preview = el('div', { class: 'estimate' });

  let latest = null;

  async function refreshPreview() {
    const text = input.value.trim();
    if (!text) { clear(preview); latest = null; return; }
    try {
      const result = await api.post('/api/channels/bulk/preview', {
        text, video_limit: Number(limit.value), skip_existing: skipExisting.checked,
      });
      latest = result;
      clear(preview);
      preview.append(...previewNodes(result));
    } catch (err) {
      clear(preview);
      preview.append(el('span', { class: 'muted small', text: 'Could not price that list.' }));
    }
  }

  const debouncedPreview = debounce(refreshPreview, 350);
  input.oninput = debouncedPreview;
  limit.onchange = refreshPreview;
  skipExisting.onchange = refreshPreview;

  modal({
    title: 'Add channels',
    subtitle: 'Paste one channel or a whole list — handles, full URLs, or raw channel IDs, ' +
              'in any mix. Everything is priced before anything is spent.',
    body: el('div', { class: 'stack' },
      el('div', { class: 'field' },
        el('label', { text: 'Channels' }), input,
        el('div', { class: 'help', text:
          'One per line, or separated by commas. Bullets and numbering are stripped, ' +
          'and duplicates are dropped.' })),
      el('div', { class: 'field' },
        el('label', { text: 'Import how many recent uploads from each?' }), limit,
        el('div', { class: 'help', text:
          '50 is enough for a stable baseline on most channels. Go higher for channels ' +
          'that upload several times a week.' })),
      el('label', { class: 'switch' }, skipExisting, el('span', { class: 'track' }),
        el('span', { class: 'small', text: 'Skip channels already tracked' })),
      preview),
    actions: [
      { label: 'Cancel' },
      { label: 'Import', variant: 'primary', onClick: async () => {
          const text = input.value.trim();
          if (!text) { toast('Paste at least one channel first.', { kind: 'warning' }); return 'keep'; }
          if (latest && !latest.within_cap) {
            toast('That import exceeds the per-operation quota cap.', { kind: 'warning',
              hint: 'Import fewer channels at a time, or lower the uploads per channel.' });
            return 'keep';
          }
          try {
            const result = await api.post('/api/channels/bulk', {
              text, video_limit: Number(limit.value), skip_existing: skipExisting.checked,
            });
            await refreshStatus();
            await reload();
            reportResults(result);
          } catch (err) {
            toastError(err, 'Could not import those channels');
            return 'keep';
          }
        } },
    ],
  });

  // Prime the preview if something was pasted before the dialog settled.
  setTimeout(refreshPreview, 60);
}

function previewNodes(result) {
  const nodes = [];

  if (result.truncated) {
    nodes.push(notice('warning', `Only the first ${result.max_references} will be imported`,
      'That is more channels than one import handles at a time. Run the rest as a second batch.'));
  }

  const parts = [`${result.parsed} found`];
  if (result.existing) parts.push(`${result.existing} already tracked`);
  parts.push(`${result.to_import} to import`);

  nodes.push(notice(
    result.affordable && result.within_cap ? 'info' : 'warning',
    `${parts.join(' · ')} — about ${full(result.estimated_units)} quota units`,
    `${result.per_channel_units} units per channel. You have ${full(result.remaining)} left today.` +
    (result.within_cap ? ''
      : ` This exceeds the ${full(result.cap)}-unit per-operation cap, so it will be refused.`) +
    (!result.affordable && result.within_cap
      ? ' There is not enough quota left today for all of them — the import will stop cleanly when it runs out.' : '')));

  if (result.items.length) {
    nodes.push(el('div', { class: 'ref-list' },
      result.items.map((item) => el('div', { class: `ref-row ${item.status}` },
        el('span', { class: 'ref-name truncate', text: item.reference }),
        item.status === 'existing'
          ? el('span', { class: 'small muted', text: item.tracked ? 'already tracked' : 'stored, not tracked' })
          : el('span', { class: 'small muted', text: 'new' })))));
  }

  return nodes;
}

/** Report a batch outcome: a toast for the summary, a modal when rows failed. */
function reportResults(result) {
  const summary = `${result.imported} imported` +
    (result.skipped ? `, ${result.skipped} skipped` : '') +
    (result.failed ? `, ${result.failed} failed` : '') +
    `. ${result.units_spent} units spent` +
    (result.cache_hits ? `, ${result.cache_hits} served from cache` : '') + '.';

  if (!result.failed && !result.quota_ran_out) {
    toast(summary, { kind: 'good', title: 'Import finished' });
    return;
  }

  // Anything that didn't import needs to be readable and re-copyable, so it
  // gets a real list rather than a toast that vanishes in five seconds.
  const problems = result.results.filter((r) => r.status === 'failed' || r.status === 'not_attempted');
  modal({
    title: 'Import finished with problems',
    subtitle: summary,
    body: el('div', { class: 'stack' },
      result.quota_ran_out
        ? notice('warning', 'The daily quota ran out partway through',
            'Channels below marked "not attempted" were never touched. Re-run this import ' +
            'after the quota resets and they will be picked up — anything already imported is skipped.')
        : null,
      el('div', { class: 'ref-list' },
        problems.map((r) => el('div', { class: `ref-row ${r.status}` },
          el('div', { class: 'ref-name' },
            el('div', { class: 'truncate', text: r.reference }),
            el('div', { class: 'small muted', text: r.message || '' }),
            r.hint ? el('div', { class: 'small muted', text: r.hint }) : null),
          el('span', { class: 'small muted nowrap',
            text: r.status === 'failed' ? 'failed' : 'not attempted' })))),
      el('div', { class: 'field' },
        el('label', { text: 'Just the ones that did not import' }),
        el('textarea', { rows: 3, readonly: true, spellcheck: false,
          value: problems.map((r) => r.reference).join('\n') }),
        el('div', { class: 'help', text: 'Copy this to retry them after fixing or waiting.' }))),
    actions: [{ label: 'Close', variant: 'primary' }],
  });
}

function removeDialog(channel, reload) {
  const purge = el('input', { type: 'checkbox' });
  modal({
    title: `Remove ${channel.title}?`,
    subtitle: 'By default the videos already fetched are kept — they cost quota once, and ' +
              're-adding the channel later would cost it again.',
    body: el('div', { class: 'stack' },
      el('label', { class: 'switch' }, purge, el('span', { class: 'track' }),
        el('span', { text: `Also delete ${full(channel.stored_videos)} stored videos and their history` })),
      notice('warning', 'Deleting is permanent',
        'Snapshot history and detected title/thumbnail changes for this channel cannot be ' +
        'recovered — YouTube does not serve historical view counts.')),
    actions: [
      { label: 'Cancel' },
      { label: 'Remove', variant: 'danger', onClick: async () => {
          try {
            await api.del(`/api/channels/${channel.id}`, { purge: purge.checked });
            toast(purge.checked ? 'Channel and its data removed.' : 'Channel untracked.',
              { kind: 'info' });
            await reload();
          } catch (err) { toastError(err); }
        } },
    ],
  });
}
