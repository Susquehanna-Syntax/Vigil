/* Notification channels pane (sprint M7).
   HTML: templates/pages/_settings.html (data-pane="notifications")
   API:  GET /api/v1/alerts/channels/types/  (each type's form fields)
         GET/POST /api/v1/alerts/channels/, GET/PATCH/DELETE .../<id>/
         POST .../<id>/test/  — all admin-only.

   The form is built from each type's fields, so a new channel type on the
   server shows up here with no change to this file. A secret field is never
   filled in when editing: the server does not return secrets, and leaving it
   blank keeps the stored one. Rows reuse the relay row styles. */

let _channelTypes = null;
let _channelsById = {};

async function _channelTypesLoad() {
  if (!_channelTypes) _channelTypes = await apiJson('/api/v1/alerts/channels/types/');
  return _channelTypes;
}

/* ── What each channel type is, and which edition it comes with ──────────
   Every type was one flat list, so nothing said which are free, which need
   Business, and which talk to a system you run. "Webhook" in particular read
   as the Business event webhooks under API & Webhooks; it is the free alert
   channel, and its description now says so. */

//: [group, description] per kind. Unknown kinds fall into Integrations.
function _channelTypeInfo(kind) {
  const info = {
    slack: ['chat', t('Free. Posts to a Slack channel through an incoming webhook (Mattermost and Rocket.Chat work too).')],
    teams: ['chat', t('Free. Posts to a Teams channel through a Workflows URL.')],
    discord: ['chat', t('Free. Posts to a Discord channel through its webhook URL.')],
    ntfy: ['chat', t('Free. Pushes to an ntfy topic, on ntfy.sh or your own server.')],
    gotify: ['chat', t('Free. Pushes to your Gotify server.')],
    pushover: ['chat', t('Free. Pushes to your phone through Pushover.')],
    email: ['mail', t('Free. Sends through the mail server set under Settings › Email.')],
    webhook: ['integration', t('Free. Posts each alert as JSON to a URL you run. This is the alert channel; event webhooks for tasks and hosts are the Business feature under API & Webhooks.')],
    ticket: ['integration', t('Free. Opens a ticket in your helpdesk through its create-ticket API.')],
  }[kind];
  return info || ['integration', ''];
}

function _channelTypeAbout(ty) {
  if (!ty) return '';
  if (!ty.available) return t('Comes with SQSY Business. See Settings › License.');
  return _channelTypeInfo(ty.kind)[1];
}

function _channelTypeOptions(types) {
  const groups = [
    ['chat', t('Chat and push (free)')],
    ['mail', t('Email (free)')],
    ['integration', t('Integrations (free)')],
    ['business', t('SQSY Business')],
  ];
  const groupOf = (ty) => (ty.feature ? 'business' : _channelTypeInfo(ty.kind)[0]);
  return groups.map(([key, label]) => {
    const rows = types.filter((ty) => groupOf(ty) === key);
    if (!rows.length) return '';
    return `<optgroup label="${escAttr(label)}">` + rows.map((ty) => `<option value="${escAttr(ty.kind)}"`
      + `${ty.available ? '' : ' disabled'}>${escHtml(ty.label)}${ty.available ? '' : ` ${escHtml(t('(Business)'))}`}</option>`).join('')
      + '</optgroup>';
  }).join('');
}

function _channelType(kind) {
  return (_channelTypes || []).find((t) => t.kind === kind) || null;
}

function _channelStatusHtml(c) {
  if (!c.enabled) return `<span class="relay-status off">${escHtml(t('Off'))}</span>`;
  if (c.last_ok === null) return `<span class="relay-status off">${escHtml(t('Not used yet'))}</span>`;
  const when = timeAgo(c.last_status_at);
  const cls = c.last_ok ? 'ok' : 'bad';
  const label = c.last_ok ? t('Delivered') : t('Failing');
  return '<div class="relay-status-col">'
    + `<span class="relay-status ${cls}">${label}</span>`
    + (c.last_ok ? '' : `<span class="relay-err">${escHtml(c.last_status || '')}</span>`)
    + (when ? `<span class="relay-meta">${escHtml(when)}</span>` : '')
    + '</div>';
}

function _channelSummary(c) {
  const cfg = c.config || {};
  if (cfg.url) return cfg.url;
  if (Array.isArray(cfg.recipients)) return cfg.recipients.join(', ');
  const first = Object.values(cfg).find((v) => typeof v === 'string' && v);
  return first || '';
}

function _channelRowHtml(c) {
  const events = [c.on_firing ? t('firing') : '', c.on_resolved ? t('resolved') : '']
    .filter(Boolean).join(` ${t('and')} `) || t('no events');
  const id = escAttr(c.id);
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(c.name)}<span class="relay-badge">${escHtml(c.kind_label)}</span></div>`
      + `<div class="relay-url mono">${escHtml(_channelSummary(c))}</div>`
      + `<div class="relay-meta">${escHtml(t('Sends on {events}', { events }))}</div>`
    + '</div>'
    + `<div class="relay-status-cell">${_channelStatusHtml(c)}`
      + '<div class="relay-actions">'
        + `<button class="btn btn-sm btn-sky" data-act="testChannel" data-a1="${id}">${escHtml(t('Send test'))}</button>`
        + `<button class="btn btn-sm btn-outline" data-act="editChannel" data-a1="${id}">${escHtml(t('Edit'))}</button>`
        + `<button class="btn btn-sm btn-outline" data-act="toggleChannel" data-a1="${id}">${c.enabled ? escHtml(t('Turn off')) : escHtml(t('Turn on'))}</button>`
        + `<button class="btn btn-sm btn-rose" data-act="deleteChannel" data-a1="${id}">${escHtml(t('Delete'))}</button>`
      + '</div>'
    + '</div>'
  + '</div>';
}

async function loadChannels() {
  const list = document.getElementById('channels-list');
  if (!list) return;
  let rows;
  try {
    [rows] = await Promise.all([apiJson('/api/v1/alerts/channels/'), _channelTypesLoad()]);
  } catch (e) {
    list.innerHTML = `<p class="muted">${escHtml(t('Could not load channels: {error}', { error: e.message }))}</p>`;
    return;
  }
  _channelsById = {};
  rows.forEach((c) => { _channelsById[c.id] = c; });
  list.innerHTML = rows.length ? rows.map(_channelRowHtml).join('')
    : `<p class="muted">${escHtml(t('No channels yet. Alerts are shown in Vigil but sent nowhere.'))}</p>`;
}

function _channelFieldHtml(f, value, secretSet) {
  const id = `chf-${escAttr(f.name)}`;
  const optional = f.required ? '' : ` ${t('(optional)')}`;
  const label = `<label class="form-label" for="${id}">${escHtml(f.label)}${escHtml(optional)}</label>`;
  const help = f.help ? `<div class="totp-sub">${escHtml(f.help)}</div>` : '';
  const attrs = `id="${id}" data-field="${escAttr(f.name)}" data-kind="${escAttr(f.kind)}" autocomplete="off"`;
  let input;
  if (f.kind === 'secret' || f.kind === 'secret_url') {
    const ph = secretSet ? t('Saved. Leave blank to keep it.') : '';
    input = `<input type="password" class="form-control" ${attrs} placeholder="${escAttr(ph)}">`;
  } else if (f.kind === 'list') {
    const text = Array.isArray(value) ? value.join('\n') : (value || '');
    input = `<textarea class="form-control" rows="3" ${attrs}>${escHtml(text)}</textarea>`;
  } else if (f.kind === 'json') {
    input = `<textarea class="form-control channel-json" rows="6" spellcheck="false" ${attrs}>${escHtml(value || '')}</textarea>`;
  } else if (f.kind === 'choice') {
    const cur = value || f.default || '';
    input = `<select class="form-control" ${attrs}>${f.choices.map((ch) =>
      `<option value="${escAttr(ch)}"${ch === cur ? ' selected' : ''}>${escHtml(ch)}</option>`).join('')}</select>`;
  } else {
    const type = f.kind === 'url' ? 'url' : 'text';
    input = `<input type="${type}" class="form-control" ${attrs} value="${escAttr(value || '')}"`
      + ` placeholder="${escAttr(f.default || '')}">`;
  }
  return `<div class="form-group">${label}${input}${help}</div>`;
}

function _channelFormFields(m, type, channel) {
  const box = m.modal.querySelector('#chf-fields');
  const cfg = (channel && channel.config) || {};
  const secrets = (channel && channel.secrets_set) || [];
  box.innerHTML = type ? type.fields.map((f) =>
    _channelFieldHtml(f, cfg[f.name], secrets.includes(f.name))).join('') : '';
}

function _channelFormConfig(m) {
  const config = {};
  m.modal.querySelectorAll('#chf-fields [data-field]').forEach((el) => {
    config[el.dataset.field] = el.value;
  });
  return config;
}

/* Field errors come back as {config: {field: "reason"}} or {name: [...]}. */
function _channelErrorText(e) {
  const body = e.body || {};
  if (body.config && typeof body.config === 'object') {
    return Object.entries(body.config).map(([k, v]) => `${k}: ${v}`).join('; ');
  }
  for (const key of ['name', 'kind']) {
    if (Array.isArray(body[key])) return body[key].join(' ');
  }
  return e.message;
}

async function openChannelForm(channel) {
  let types;
  try {
    types = await _channelTypesLoad();
  } catch (e) {
    showToast(t('Could not load channel types: {message}', { message: e.message }), 'error');
    return;
  }
  const editing = Boolean(channel && channel.id);
  const m = mountModal('channel-form', { wide: true });
  const typeOptions = _channelTypeOptions(types);
  m.setBody(`
    <div class="modal-title"><span id="chf-title"></span>
      <button class="modal-close" id="chf-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="form-group"><label class="form-label" for="chf-name">${escHtml(t('Name'))}</label>
      <input type="text" class="form-control" id="chf-name" maxlength="255" autocomplete="off">
      <div class="totp-sub">${escHtml(t('Tasks can send to a channel by this name.'))}</div></div>
    <div class="form-group"><label class="form-label" for="chf-kind">${escHtml(t('Type'))}</label>
      <select class="form-control" id="chf-kind"${editing ? ' disabled' : ''}>${typeOptions}</select>
      <div class="totp-sub chf-kind-about" id="chf-kind-about"></div></div>
    <div id="chf-fields"></div>
    <div class="form-group" id="chf-site-field" hidden><label class="form-label" for="chf-site">${escHtml(t('Site'))}</label>
      <select class="form-control" id="chf-site"></select>
      <div class="totp-sub">${escHtml(t("A site's channel hears only that site's alerts. A global one hears every site's."))}</div></div>
    <div class="form-group">
      <label class="setting-check"><input type="checkbox" id="chf-firing"> ${escHtml(t('Send when an alert fires'))}</label>
      <label class="setting-check"><input type="checkbox" id="chf-resolved"> ${escHtml(t('Send when an alert resolves'))}</label>
    </div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="chf-cancel">${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" id="chf-save">${escHtml(t('Save'))}</button>
    </div>`);
  const $ = (sel) => m.modal.querySelector(sel);
  $('#chf-title').textContent = editing ? t('Edit {name}', { name: channel.name }) : t('Add a notification channel');
  $('#chf-name').value = editing ? channel.name : '';
  const kindSel = $('#chf-kind');
  if (editing) kindSel.value = channel.kind;
  else {
    const first = types.find((t) => t.available);
    if (first) kindSel.value = first.kind;
  }
  $('#chf-firing').checked = editing ? channel.on_firing : true;
  $('#chf-resolved').checked = editing ? channel.on_resolved : true;
  const render = () => {
    _channelFormFields(m, _channelType(kindSel.value), editing ? channel : null);
    $('#chf-kind-about').textContent = _channelTypeAbout(_channelType(kindSel.value));
  };
  kindSel.onchange = render;
  render();
  sitePickFill($('#chf-site'), $('#chf-site-field'), editing ? channel.site_id : null, { editing });

  const close = () => m.close();
  $('#chf-x').onclick = close;
  $('#chf-cancel').onclick = close;
  $('#chf-save').onclick = async () => {
    const body = {
      name: $('#chf-name').value.trim(),
      config: _channelFormConfig(m),
      on_firing: $('#chf-firing').checked,
      on_resolved: $('#chf-resolved').checked,
    };
    if (!editing) body.kind = kindSel.value;
    const site = sitePickValue($('#chf-site'), $('#chf-site-field'));
    if (site !== undefined) body.site_id = site;
    if (!body.name) { showToast(t('Give the channel a name'), 'error'); return; }
    try {
      await apiJson(editing ? `/api/v1/alerts/channels/${channel.id}/` : '/api/v1/alerts/channels/', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body),
      });
      close();
      showToast(editing ? t('Channel saved') : t('Channel added. Send a test to check it.'), 'success');
      loadChannels();
    } catch (e) {
      showToast(e.status === 402 ? `${e.message} ${t('See Settings → License.')}` : _channelErrorText(e), 'error');
    }
  };
  requestAnimationFrame(() => { m.open(); $('#chf-name').focus(); });
}

function addChannel() { openChannelForm(null); }

function editChannel(id) {
  const c = _channelsById[id];
  if (c) openChannelForm(c);
}

async function testChannel(id) {
  const c = _channelsById[id];
  if (!c) return;
  try {
    const r = await apiJson(`/api/v1/alerts/channels/${id}/test/`, { method: 'POST' });
    showToast(r.ok ? t('Test sent to {name}', { name: c.name }) : `${c.name}: ${r.detail}`, r.ok ? 'success' : 'error');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadChannels();
}

async function toggleChannel(id) {
  const c = _channelsById[id];
  if (!c) return;
  try {
    await apiJson(`/api/v1/alerts/channels/${id}/`, {
      method: 'PATCH', body: JSON.stringify({ enabled: !c.enabled }),
    });
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadChannels();
}

async function deleteChannel(id) {
  const c = _channelsById[id];
  if (!c) return;
  const ok = await confirmModal(t('Delete {name}? Alerts stop going there, and tasks that name it will send nothing.', { name: c.name }),
    { title: t('Delete channel'), confirmText: t('Delete'), danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/alerts/channels/${id}/`, { method: 'DELETE' });
    showToast(t('{name} deleted', { name: c.name }), 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadChannels();
}
