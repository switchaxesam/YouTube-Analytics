/* Settings — also the setup guide.
 *
 * A key field with no explanation of where the key comes from is where local
 * tools usually lose people, so every credential here carries the steps to
 * obtain it and a button that proves whether it works.
 *
 * The UI never receives a real secret. Fields show a masked hint when one is
 * stored, an empty submission means "leave unchanged", and clearing is an
 * explicit action.
 */

import { api } from '../api.js';
import { applyTheme, refreshStatus, state } from '../app.js';
import { el, toast, toastError, notice, withBusy, modal, badge, ICON } from '../ui.js';

export async function render(view) {
  const status = state.status || await refreshStatus();
  const settings = status.settings;

  const page = el('div', { class: 'stack' });

  page.append(el('div', { class: 'page-head' },
    el('div', {},
      el('h1', { text: 'Settings' }),
      el('p', { class: 'lede', text:
        'Channel Lens runs entirely on your machine and talks to YouTube with your own ' +
        'API key. Nothing is sent anywhere else, and every credential below stays in a ' +
        'file on this computer.' }))));

  if (status.missing?.length) {
    const required = status.missing.filter((g) => g.required);
    const optional = status.missing.filter((g) => !g.required);

    page.append(el('div', { class: 'card' },
      el('div', { class: 'card-head' },
        el('div', {},
          el('h2', { text: 'Setup' }),
          el('div', { class: 'sub', text:
            required.length
              ? `${required.length} required, ${optional.length} optional`
              : 'Everything required is configured' }))),
      el('div', { class: 'card-body stack' },
        // Required blockers first, then optional extras clearly marked as
        // such — an optional item shown as a warning reads as a chore.
        required.map((gap) => notice('warning', gap.blocks, '', gap.fix)),
        optionalGaps(optional),
      )));
  }

  page.append(credentialsCard(settings));
  page.append(identityCard(settings));
  page.append(analysisCard(settings));
  page.append(scoringCard(settings));
  page.append(trackerCard(settings));
  page.append(appearanceCard(settings));
  page.append(aboutCard());

  view.append(page);
}

/** Optional setup items, each labelled free or paid.
 *
 * Kept out of the main tree because deep inline nesting is where an unbalanced
 * paren hides — this exact block shipped with one missing and took the whole
 * Settings page down.
 */
function optionalGaps(gaps) {
  if (!gaps.length) return null;

  const row = (gap) => el('div', { class: 'notice info' },
    el('div', { class: 'icon', html: ICON.info }),
    el('div', {},
      el('div', { class: 'row mb-sm' },
        el('strong', { text: gap.blocks }),
        gap.cost === 'paid'
          ? badge('costs money', 'warning', 'alert')
          : badge('free', 'good', 'check')),
      el('p', { class: 'fix', text: gap.fix })));

  return el('div', {},
    el('h3', { class: 'mb-sm', text: 'Optional' }),
    el('div', { class: 'stack' }, gaps.map(row)));
}

/* ----------------------------------------------------------- primitives */

function field(label, input, help) {
  return el('div', { class: 'field' },
    el('label', { text: label }), input,
    help ? el('div', { class: 'help', text: help }) : null);
}

function secretInput(id, settings, placeholder) {
  const isSet = settings[`${id}_set`];
  return el('input', {
    type: 'password', id, placeholder: isSet ? `Stored ${settings[`${id}_hint`]}` : placeholder,
    autocomplete: 'off', spellcheck: false,
  });
}

async function save(patch) {
  const body = await api.put('/api/settings', patch);
  await refreshStatus();
  return body;
}

/** Save on blur/change, so nothing is lost by navigating away mid-edit. */
function autosave(input, key, transform = (v) => v) {
  input.addEventListener('change', async () => {
    try {
      await save({ [key]: transform(input.value) });
      input.classList.add('saved');
      setTimeout(() => input.classList.remove('saved'), 900);
    } catch (err) { toastError(err, 'Could not save'); }
  });
  return input;
}

/* --------------------------------------------------------- credentials */

function credentialsCard(settings) {
  const ytKey = secretInput('youtube_api_key', settings, 'AIza…');
  const anthropicKey = secretInput('anthropic_api_key', settings, 'sk-ant-…');
  const clientId = secretInput('google_client_id', settings, '…apps.googleusercontent.com');
  const clientSecret = secretInput('google_client_secret', settings, 'GOCSPX-…');

  const ytTest = el('button', { class: 'btn sm' }, 'Test key');
  ytTest.onclick = () => withBusy(ytTest, 'Testing…', async () => {
    if (ytKey.value.trim()) await save({ youtube_api_key: ytKey.value.trim() });
    const result = await api.post('/api/settings/test-youtube-key');
    ytKey.value = '';
    toast(result.message, { kind: result.ok ? 'good' : 'critical',
      title: result.ok ? 'Connected' : 'Key rejected', hint: result.hint || '' });
    await refreshStatus();
  });

  const anthropicTest = el('button', { class: 'btn sm' }, 'Test key');
  anthropicTest.onclick = () => withBusy(anthropicTest, 'Testing…', async () => {
    if (anthropicKey.value.trim()) await save({ anthropic_api_key: anthropicKey.value.trim() });
    const result = await api.post('/api/settings/test-anthropic-key');
    anthropicKey.value = '';
    toast(result.message, { kind: result.ok ? 'good' : 'critical' });
    await refreshStatus();
  });

  const saveYt = el('button', { class: 'btn primary sm' }, 'Save');
  saveYt.onclick = () => withBusy(saveYt, 'Saving…', async () => {
    await save({ youtube_api_key: ytKey.value.trim() });
    ytKey.value = ''; toast('YouTube API key saved.', { kind: 'good' });
  });

  const saveAnthropic = el('button', { class: 'btn primary sm' }, 'Save');
  saveAnthropic.onclick = () => withBusy(saveAnthropic, 'Saving…', async () => {
    await save({ anthropic_api_key: anthropicKey.value.trim() });
    anthropicKey.value = ''; toast('Anthropic API key saved.', { kind: 'good' });
  });

  const saveOauth = el('button', { class: 'btn primary sm' }, 'Save');
  saveOauth.onclick = () => withBusy(saveOauth, 'Saving…', async () => {
    await save({
      google_client_id: clientId.value.trim(),
      google_client_secret: clientSecret.value.trim(),
    });
    clientId.value = ''; clientSecret.value = '';
    toast('OAuth client saved. You can connect now.', { kind: 'good' });
    location.reload();
  });

  const connected = state.status?.oauth?.connected;
  const connectBtn = el('button', { class: `btn ${connected ? '' : 'primary'} sm` },
    connected ? 'Reconnect' : 'Connect to Google');
  connectBtn.onclick = () => withBusy(connectBtn, 'Opening…', async () => {
    const result = await api.get('/api/auth/url');
    if (!result.ok) { toast(result.message, { kind: 'critical', hint: result.hint || '' }); return; }
    window.open(result.url, '_blank', 'noopener');
    toast('Approve access in the tab that just opened, then come back.',
      { kind: 'info', title: 'Waiting for Google', timeout: 9000 });
  });

  const disconnectBtn = el('button', { class: 'btn danger sm' }, 'Disconnect');
  disconnectBtn.onclick = () => withBusy(disconnectBtn, 'Disconnecting…', async () => {
    await api.post('/api/auth/disconnect');
    toast('Disconnected.', { kind: 'info' });
    location.reload();
  });

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Credentials' }),
        el('div', { class: 'sub', text: 'Stored locally, never transmitted anywhere but Google and Anthropic' }))),
    el('div', { class: 'card-body stack' },

      /* --- YouTube Data API --- */
      el('div', { class: 'setting-block' },
        el('div', { class: 'between mb-sm' },
          el('h3', { text: 'YouTube Data API key' }),
          settings.youtube_api_key_set ? badge('Configured', 'good', 'check') : badge('Required', 'critical', 'alert')),
        el('p', { class: 'help mb-sm', text:
          'Free. Powers everything that reads public YouTube data. Your project gets ' +
          '10,000 quota units a day, which is plenty — this app is built around ' +
          'spending them carefully.' }),
        el('ol', { class: 'steps' },
          el('li', {}, 'Open ', link('console.cloud.google.com/projectcreate', 'Google Cloud'), ' and create a project.'),
          el('li', {}, 'In APIs & Services → Library, enable ', el('strong', { text: 'YouTube Data API v3' }), '.'),
          el('li', {}, 'In APIs & Services → Credentials, choose Create credentials → API key.'),
          el('li', {}, 'Paste it below.')),
        field('API key', ytKey),
        el('div', { class: 'row mt-sm' }, saveYt, ytTest,
          settings.youtube_api_key_set ? clearBtn('youtube_api_key') : null)),

      el('div', { class: 'divider' }),

      /* --- OAuth for owner analytics --- */
      el('div', { class: 'setting-block' },
        el('div', { class: 'between mb-sm' },
          el('h3', { text: 'Google OAuth client — your own channel' }),
          connected ? badge('Connected', 'good', 'check')
                    : settings.google_client_id_set ? badge('Ready to connect', 'accent')
                    : badge('Not set up', '')),
        el('p', { class: 'help mb-sm', text:
          'Unlocks impressions, click-through rate, and retention for your own videos. ' +
          'These are the numbers no competitor tool can ever show you, because only the ' +
          'channel owner can see them. Read-only access; revenue data is never requested.' }),
        el('ol', { class: 'steps' },
          el('li', {}, 'In the same project, open APIs & Services → Library and enable ',
            el('strong', { text: 'YouTube Analytics API' }), ' and ',
            el('strong', { text: 'YouTube Reporting API' }),
            '. Both are needed — CTR comes only from the Reporting one.'),
          el('li', {}, 'Configure the OAuth consent screen. User type ',
            el('strong', { text: 'External' }), ' (Internal needs a Workspace organisation).'),
          el('li', {}, 'Set publishing status to ', el('strong', { text: 'In production' }),
            '. This matters: in Testing mode Google expires the connection after ' +
            '7 days and you would have to reconnect weekly.'),
          el('li', {}, 'Go to Credentials → Create credentials → OAuth client ID, ' +
            'application type ', el('strong', { text: 'Desktop app' }),
            ' — that type permits the localhost redirect this app uses.'),
          el('li', {}, 'Paste the client ID and secret below, save, then press Connect.')),
        notice('info', 'Expect an “unverified app” warning',
          'Google shows it for any app it has not reviewed, which includes every ' +
          'personal tool. Choose Advanced → Go to Channel Lens. Verification only ' +
          'exists to remove that screen for strangers; you are the only user.'),
        el('div', { class: 'grid cols-2' },
          field('Client ID', clientId),
          field('Client secret', clientSecret)),
        el('div', { class: 'row mt-sm' }, saveOauth,
          settings.google_client_id_set ? connectBtn : null,
          connected ? disconnectBtn : null)),

      el('div', { class: 'divider' }),

      /* --- Anthropic --- */
      el('div', { class: 'setting-block' },
        el('div', { class: 'between mb-sm' },
          el('h3', { text: 'Anthropic API key' }),
          settings.anthropic_api_key_set ? badge('Configured', 'good', 'check') : badge('Optional', '')),
        el('p', { class: 'help mb-sm', text:
          'Used only for AI thumbnail breakdowns. Everything else — colour, contrast, ' +
          'busyness, and every title metric — is computed locally and works without it.' }),
        field('API key', anthropicKey,
          'From console.anthropic.com. Billed per image analysed; each thumbnail is analysed once and cached forever.'),
        el('div', { class: 'row mt-sm' }, saveAnthropic, anthropicTest,
          settings.anthropic_api_key_set ? clearBtn('anthropic_api_key') : null)),
    ));
}

function link(text, label) {
  return el('a', { href: `https://${text}`, target: '_blank', rel: 'noopener',
    class: 'link', text: label || text });
}

function clearBtn(key) {
  const btn = el('button', { class: 'btn ghost sm' }, 'Clear');
  btn.onclick = () => modal({
    title: 'Clear this credential?',
    subtitle: 'It will be removed from the settings file on this machine. You can paste it again at any time.',
    body: el('div', {}),
    actions: [
      { label: 'Cancel' },
      { label: 'Clear it', variant: 'danger', onClick: async () => {
        await save({ clear_secrets: [key] });
        toast('Cleared.', { kind: 'info' });
        location.reload();
      } },
    ],
  });
  return btn;
}

/* -------------------------------------------------------------- identity */

function identityCard(settings) {
  const input = el('input', { type: 'text', value: settings.owned_channel_id || '',
    placeholder: '@yourhandle, UC…, or your channel URL' });

  const linkBtn = el('button', { class: 'btn primary sm' }, 'Link my channel');
  linkBtn.onclick = () => withBusy(linkBtn, 'Linking…', async () => {
    await save({ owned_channel_id: input.value.trim() });
    const result = await api.post('/api/owned/link');
    toast(`Linked ${result.title} and imported ${result.videos_imported} videos.`,
      { kind: 'good', title: 'Channel linked' });
    await refreshStatus();
  });

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Your channel' }),
        el('div', { class: 'sub', text: 'Separates "mine" from "theirs" everywhere in the app' }))),
    el('div', { class: 'card-body' },
      field('Channel', input,
        'Paste anything — a handle, a channel ID, or the URL from your address bar. ' +
        'Linking also imports your recent uploads so your titles can be compared against outliers.'),
      el('div', { class: 'row mt-sm' }, linkBtn)));
}

/* -------------------------------------------------------------- analysis */

function analysisCard(settings) {
  const model = el('select', {},
    ['claude-opus-5', 'claude-sonnet-5', 'claude-haiku-4-5'].map((m) =>
      el('option', { value: m, selected: settings.anthropic_model === m, text: m })));
  const effort = el('select', {},
    ['low', 'medium', 'high'].map((e) =>
      el('option', { value: e, selected: settings.anthropic_effort === e, text: e })));
  const shorts = el('input', { type: 'number', min: 30, max: 600, value: settings.shorts_max_seconds });

  autosave(model, 'anthropic_model');
  autosave(effort, 'anthropic_effort');
  autosave(shorts, 'shorts_max_seconds', Number);

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: 'Analysis' }))),
    el('div', { class: 'card-body grid cols-3' },
      field('Vision model', model, 'Used for thumbnail breakdowns.'),
      field('Reasoning effort', effort,
        'Thumbnail analysis is structured extraction, which gains little from deep reasoning — low keeps the cost down over hundreds of images.'),
      field('Shorts cutoff (seconds)', shorts,
        'Videos at or under this length are scored against a separate baseline. Mixing Shorts with long-form makes both meaningless.')));
}

/* -------------------------------------------------------------- scoring */

function scoringCard(settings) {
  const threshold = el('input', { type: 'number', min: 1.5, max: 20, step: 0.5, value: settings.outlier_threshold });
  const window_ = el('input', { type: 'number', min: 5, max: 200, value: settings.baseline_window });
  const minAge = el('input', { type: 'number', min: 0, max: 90, value: settings.baseline_min_age_days });
  const minVideos = el('input', { type: 'number', min: 2, max: 50, value: settings.baseline_min_videos });
  const budget = el('input', { type: 'number', min: 100, max: 1000000, step: 100, value: settings.daily_quota_budget });
  const cap = el('input', { type: 'number', min: 10, max: 10000, step: 10, value: settings.per_operation_quota_cap });

  autosave(threshold, 'outlier_threshold', Number);
  autosave(window_, 'baseline_window', Number);
  autosave(minAge, 'baseline_min_age_days', Number);
  autosave(minVideos, 'baseline_min_videos', Number);
  autosave(budget, 'daily_quota_budget', Number);
  autosave(cap, 'per_operation_quota_cap', Number);

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Scoring and quota' }),
        el('div', { class: 'sub', text: 'How "normal" is defined, and how much you are willing to spend to find out' }))),
    el('div', { class: 'card-body stack' },
      el('div', { class: 'grid cols-4' },
        field('Outlier threshold (×)', threshold, 'Multiplier at which a video counts as a breakout.'),
        field('Baseline window', window_, 'How many recent uploads define a channel\'s normal.'),
        field('Minimum age (days)', minAge, 'Videos younger than this are scored but never form the baseline.'),
        field('Minimum sample', minVideos, 'Below this, multipliers are flagged as unreliable rather than trusted.')),
      el('div', { class: 'divider' }),
      el('div', { class: 'grid cols-2' },
        field('Daily quota budget', budget,
          'YouTube allows 10,000 units a day. The default of 9,000 leaves headroom so a runaway job trips this guard before Google\'s.'),
        field('Per-operation cap', cap,
          'Refuses any single action estimated to cost more than this, so one careless click can\'t eat the day.'))));
}

/* -------------------------------------------------------------- tracker */

function trackerCard(settings) {
  const enabled = el('input', { type: 'checkbox', checked: settings.tracker_enabled });
  const interval = el('input', { type: 'number', min: 1, max: 48, value: settings.tracker_interval_hours });
  const maxAge = el('input', { type: 'number', min: 7, max: 730, value: settings.tracker_max_video_age_days });

  enabled.addEventListener('change', async () => {
    await save({ tracker_enabled: enabled.checked });
    toast(enabled.checked ? 'Tracker running while the app is open.' : 'Tracker paused.',
      { kind: 'info' });
  });
  autosave(interval, 'tracker_interval_hours', Number);
  autosave(maxAge, 'tracker_max_video_age_days', Number);

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' },
      el('div', {}, el('h2', { text: 'Tracker' }),
        el('div', { class: 'sub', text: 'Builds view history and catches title and thumbnail swaps' }))),
    el('div', { class: 'card-body stack' },
      el('label', { class: 'switch' }, enabled, el('span', { class: 'track' }),
        el('span', { text: 'Poll the watchlist automatically while Channel Lens is open' })),
      el('p', { class: 'help', text:
        'Polling is cheap — 1 unit per 50 videos, so a 200-video watchlist costs about ' +
        '16 units a day at the default interval. It only runs while the app is open, ' +
        'so it can never spend quota behind your back.' }),
      el('div', { class: 'grid cols-2' },
        field('Interval (hours)', interval),
        field('Stop tracking after (days)', maxAge,
          'Keeps each cycle\'s cost flat as your history grows.'))));
}

/* ----------------------------------------------------------- appearance */

function appearanceCard(settings) {
  const buttons = ['system', 'light', 'dark'].map((theme) => {
    const btn = el('button', {
      class: `chip ${settings.theme === theme ? 'on' : ''}`,
      text: theme[0].toUpperCase() + theme.slice(1),
    });
    btn.onclick = async () => {
      applyTheme(theme);
      await save({ theme });
      for (const other of buttons) other.classList.remove('on');
      btn.classList.add('on');
    };
    return btn;
  });

  return el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: 'Appearance' }))),
    el('div', { class: 'card-body' },
      field('Theme', el('div', { class: 'chips' }, buttons))));
}

function aboutCard() {
  return el('div', { class: 'card' },
    el('div', { class: 'card-head' }, el('div', {}, el('h2', { text: 'About' }))),
    el('div', { class: 'card-body stack' },
      notice('info', 'Everything stays on this machine',
        'Your database, settings, credentials, and cached thumbnails live in a folder ' +
        'on this computer. Channel Lens talks to Google and (optionally) Anthropic, and ' +
        'nowhere else.'),
      el('p', { class: 'help' }, 'The HTTP API this interface uses is documented at ',
        el('a', { href: '/api/docs', target: '_blank', class: 'link', text: '/api/docs' }),
        ' if you want to script against it.')));
}
