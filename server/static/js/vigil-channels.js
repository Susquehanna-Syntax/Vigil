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

function _channelType(kind) {
  return (_channelTypes || []).find((t) => t.kind === kind) || null;
}

function _channelStatusHtml(c) {
  if (!c.enabled) return '<span class="relay-status off">Off</span>';
  if (c.last_ok === null) return '<span class="relay-status off">Not used yet</span>';
  const when = timeAgo(c.last_status_at);
  const cls = c.last_ok ? 'ok' : 'bad';
  const label = c.last_ok ? 'Delivered' : 'Failing';
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
  const events = [c.on_firing ? 'firing' : '', c.on_resolved ? 'resolved' : '']
    .filter(Boolean).join(' and ') || 'no events';
  const id = escAttr(c.id);
  return '<div class="relay-row">'
    + '<div class="relay-main">'
      + `<div class="relay-name">${escHtml(c.name)}<span class="relay-badge">${escHtml(c.kind_label)}</span></div>`
      + `<div class="relay-url mono">${escHtml(_channelSummary(c))}</div>`
      + `<div class="relay-meta">Sends on ${escHtml(events)}</div>`
    + '</div>'
    + `<div class="relay-status-cell">${_channelStatusHtml(c)}`
      + '<div class="relay-actions">'
        + `<button class="btn btn-sm btn-sky" data-act="testChannel" data-a1="${id}">Send test</button>`
        + `<button class="btn btn-sm btn-outline" data-act="editChannel" data-a1="${id}">Edit</button>`
        + `<button class="btn btn-sm btn-outline" data-act="toggleChannel" data-a1="${id}">${c.enabled ? 'Turn off' : 'Turn on'}</button>`
        + `<button class="btn btn-sm btn-rose" data-act="deleteChannel" data-a1="${id}">Delete</button>`
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
    list.innerHTML = `<p class="muted">Could not load channels: ${escHtml(e.message)}</p>`;
    return;
  }
  _channelsById = {};
  rows.forEach((c) => { _channelsById[c.id] = c; });
  list.innerHTML = rows.length ? rows.map(_channelRowHtml).join('')
    : '<p class="muted">No channels yet. Alerts are shown in Vigil but sent nowhere.</p>';
}

function _channelFieldHtml(f, value, secretSet) {
  const id = `chf-${escAttr(f.name)}`;
  const label = `<label class="form-label" for="${id}">${escHtml(f.label)}${f.required ? '' : ' (optional)'}</label>`;
  const help = f.help ? `<div class="totp-sub">${escHtml(f.help)}</div>` : '';
  const attrs = `id="${id}" data-field="${escAttr(f.name)}" data-kind="${escAttr(f.kind)}" autocomplete="off"`;
  let input;
  if (f.kind === 'secret' || f.kind === 'secret_url') {
    const ph = secretSet ? 'Saved. Leave blank to keep it.' : '';
    input = `<input type="password" class="form-control" ${attrs} placeholder="${escAttr(ph)}">`;
  } else if (f.kind === 'list') {
    const text = Array.isArray(value) ? value.join('\n') : (value || '');
    input = `<textarea class="form-control" rows="3" ${attrs}>${escHtml(text)}</textarea>`;
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
    showToast(`Could not load channel types: ${e.message}`, 'error');
    return;
  }
  const editing = Boolean(channel && channel.id);
  const m = mountModal('channel-form', { wide: true });
  const typeOptions = types.map((t) => `<option value="${escAttr(t.kind)}"`
    + `${t.available ? '' : ' disabled'}>${escHtml(t.label)}${t.available ? '' : ' (Business)'}</option>`).join('');
  m.setBody(`
    <div class="modal-title"><span id="chf-title"></span>
      <button class="modal-close" id="chf-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="form-group"><label class="form-label" for="chf-name">Name</label>
      <input type="text" class="form-control" id="chf-name" maxlength="255" autocomplete="off">
      <div class="totp-sub">Tasks can send to a channel by this name.</div></div>
    <div class="form-group"><label class="form-label" for="chf-kind">Type</label>
      <select class="form-control" id="chf-kind"${editing ? ' disabled' : ''}>${typeOptions}</select></div>
    <div id="chf-fields"></div>
    <div class="form-group" id="chf-site-field" hidden><label class="form-label" for="chf-site">Site</label>
      <select class="form-control" id="chf-site"></select>
      <div class="totp-sub">A site's channel hears only that site's alerts. A global one hears every site's.</div></div>
    <div class="form-group">
      <label class="setting-check"><input type="checkbox" id="chf-firing"> Send when an alert fires</label>
      <label class="setting-check"><input type="checkbox" id="chf-resolved"> Send when an alert resolves</label>
    </div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="chf-cancel">Cancel</button>
      <button class="btn btn-mint btn-sm" id="chf-save">Save</button>
    </div>`);
  const $ = (sel) => m.modal.querySelector(sel);
  $('#chf-title').textContent = editing ? `Edit ${channel.name}` : 'Add a notification channel';
  $('#chf-name').value = editing ? channel.name : '';
  const kindSel = $('#chf-kind');
  if (editing) kindSel.value = channel.kind;
  else {
    const first = types.find((t) => t.available);
    if (first) kindSel.value = first.kind;
  }
  $('#chf-firing').checked = editing ? channel.on_firing : true;
  $('#chf-resolved').checked = editing ? channel.on_resolved : true;
  const render = () => _channelFormFields(m, _channelType(kindSel.value), editing ? channel : null);
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
    if (!body.name) { showToast('Give the channel a name', 'error'); return; }
    try {
      await apiJson(editing ? `/api/v1/alerts/channels/${channel.id}/` : '/api/v1/alerts/channels/', {
        method: editing ? 'PATCH' : 'POST', body: JSON.stringify(body),
      });
      close();
      showToast(editing ? 'Channel saved' : 'Channel added. Send a test to check it.', 'success');
      loadChannels();
    } catch (e) {
      showToast(e.status === 402 ? `${e.message} See Settings → License.` : _channelErrorText(e), 'error');
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
    showToast(r.ok ? `Test sent to ${c.name}` : `${c.name}: ${r.detail}`, r.ok ? 'success' : 'error');
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
  const ok = await confirmModal(`Delete ${c.name}? Alerts stop going there, and tasks that name it will send nothing.`,
    { title: 'Delete channel', confirmText: 'Delete', danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/alerts/channels/${id}/`, { method: 'DELETE' });
    showToast(`${c.name} deleted`, 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
  loadChannels();
}
