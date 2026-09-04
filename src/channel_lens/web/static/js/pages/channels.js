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
  modal, withBusy, badge,
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

function addDialog(reload) {
  const reference = el('input', { type: 'text', autofocus: true,
    placeholder: '@mkbhd, a channel URL, or UC…' });
  const limit = el('select', {},
    [['25', '25 uploads'], ['50', '50 uploads (recommended)'],
     ['100', '100 uploads'], ['200', '200 uploads'], ['500', '500 uploads']]
      .map(([v, label]) => el('option', { value: v, selected: v === '50', text: label })));

  const estimate = el('div', { class: 'estimate' });

  async function refreshEstimate() {
    clear(estimate);
    estimate.append(el('span', { class: 'muted small', text: 'Estimating…' }));
    try {
      const result = await api.post('/api/channels/estimate', {
        reference: reference.value || 'x', video_limit: Number(limit.value),
      });
      clear(estimate);
      estimate.append(
        notice(result.affordable && result.within_cap ? 'info' : 'warning',
          `About ${result.estimated_units} quota units`,
          `You have ${full(result.remaining)} left today.` +
          (result.within_cap ? '' : ` This exceeds the ${full(result.cap)}-unit per-operation cap — lower the upload count.`)));
    } catch (err) {
      clear(estimate);
      estimate.append(el('span', { class: 'muted small', text: 'Could not estimate cost.' }));
    }
  }
  limit.onchange = refreshEstimate;
  refreshEstimate();

  modal({
    title: 'Add a channel',
    subtitle: 'Paste anything YouTube gives you — a handle, a full URL, or a raw channel ID. ' +
              'More uploads means a sturdier baseline, but costs slightly more quota.',
    body: el('div', { class: 'stack' },
      el('div', { class: 'field' }, el('label', { text: 'Channel' }), reference),
      el('div', { class: 'field' }, el('label', { text: 'Import how many recent uploads?' }), limit,
        el('div', { class: 'help', text:
          '50 is enough for a stable baseline on most channels. Go higher for channels ' +
          'that upload several times a week.' })),
      estimate),
    actions: [
      { label: 'Cancel' },
      { label: 'Add channel', variant: 'primary', onClick: async (close) => {
          const value = reference.value.trim();
          if (!value) { toast('Enter a channel first.', { kind: 'warning' }); return 'keep'; }
          try {
            const result = await api.post('/api/channels', {
              reference: value, video_limit: Number(limit.value),
            });
            await refreshStatus();
            toast(`Imported ${result.videos_imported} videos from ${result.channel.title}. ` +
                  `${result.units_spent} units spent${result.cache_hits ? `, ${result.cache_hits} served from cache` : ''}.`,
                  { kind: 'good', title: 'Channel added' });
            await reload();
          } catch (err) {
            toastError(err, 'Could not add that channel');
            return 'keep';
          }
        } },
    ],
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
