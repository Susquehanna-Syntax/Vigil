// vigil-api-keys.js
// Owns: Settings → API & Webhooks — minting and revoking scoped API keys,
//   and adding, testing, pausing and deleting webhooks.
// HTML: templates/pages/_settings.html — #api-keys and #webhooks in the
//   data-pane="api" pane.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr, mountModal,
//   confirmModal, showToast, delegateClick)
//
// Both endpoints live in the SQSY Business package: a 404 means the package
// is not installed and the card says so in one line. A licence can be missing
// on top of an installed package — then the list still renders, because a key
// minted under an old licence has to be revocable, and only the create form
// says it needs a licence.
//
// A key or a secret arrives exactly once, from the create (or rotate)
// response, and is written into the modal's box and nowhere else; closing the
// modal empties it out of the DOM.

const AK_KEYS_API = '/api/v1/api-keys/';
const AK_WEBHOOKS_API = '/api/v1/webhooks/';
const AK_NOT_LICENSED = 'Scoped API keys and webhooks come with SQSY Business.';
const AK_ADMIN_ONLY = 'Only an admin can manage these.';

const AK_EVENTS = [
  ['alert.sent', 'Alert fires'],
  ['alert.resolved', 'Alert clears'],
  ['task.finished', 'Task finishes'],
  ['host.approved', 'Host approved'],
];

let _akState = null;

async function loadApiPane() {
  const keysHost = document.getElementById('api-keys');
  const hooksHost = document.getElementById('webhooks');
  if (!keysHost && !hooksHost) return;
  const [keys, hooks, lic, sites] = await Promise.all([
    _akFetch(AK_KEYS_API), _akFetch(AK_WEBHOOKS_API), _akFetch('/api/v1/license/'),
    _akFetch('/api/v1/sites/'),
  ]);
  _akState = {
    keys: keys.body ? (keys.body.keys || []) : null,
    keysStatus: keys.status,
    keysMessage: keys.message,
    hooks: hooks.body ? (hooks.body.webhooks || []) : null,
    hooksStatus: hooks.status,
    hooksMessage: hooks.message,
    features: lic.body ? (lic.body.features || []) : [],
    sites: sites.body && Array.isArray(sites.body) ? sites.body : [],
  };
  _renderKeys();
  _renderWebhooks();
}

// One fetch, three outcomes: the body, or the status we branch on. A refused
// read still tells the card which refusal it was — "not installed" and "not
// an admin" are different sentences, and anything else gets the server's own
// words in a muted line.
async function _akFetch(url) {
  try {
    return { body: await apiJson(url) };
  } catch (e) {
    return { status: e.status || 0, message: e.message || 'Could not load' };
  }
}

function _akFeatureActive(name) {
  const f = (_akState.features || []).find((x) => x.name === name);
  return !!(f && f.active);
}

// 404 — the Business package is not here at all. 403 — a scoped session that
// reaches the pane but may not manage it. The sites endpoint is optional in
// the same way: 404 on a source build, and then there are no pickers.
function _akAbsence(status) {
  if (status === 404) return AK_NOT_LICENSED;
  if (status === 403) return AK_ADMIN_ONLY;
  return null;
}

/* ── Scoped API keys ───────────────────────────────────────────────────── */

function _renderKeys() {
  const host = document.getElementById('api-keys');
  if (!host || !_akState) return;
  const note = _akAbsence(_akState.keysStatus);
  if (note) { host.innerHTML = `<p class="muted">${escHtml(note)}</p>`; return; }
  // The list needs no licence — a key minted under one that lapsed must still
  // be revocable — but nothing can be read without the package either.
  if (_akState.keys === null) {
    host.innerHTML = `<p class="muted">${escHtml(_akState.keysMessage || 'Could not load keys')}</p>`;
    return;
  }
  const form = _akFeatureActive('api_keys') ? _akKeyForm()
    : '<p class="muted">Creating keys needs an SQSY Business licence.</p>';
  const rows = _akState.keys.map(_akKeyRow).join('');
  host.innerHTML = `<p class="muted" style="margin-bottom:14px;">A scoped key lets a script `
    + `or Jackil use Vigil&rsquo;s API as you, limited to the sites you pick. `
    + `Read-only keys cannot change anything.</p>`
    + form
    + (rows ? `<div class="dash-cards" style="margin-top:16px;">${rows}</div>`
            : '<p class="muted" style="margin-top:14px;">No keys yet.</p>');
}

function _akKeyForm() {
  return `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`
    + `<input type="text" class="form-control" id="api-key-name" maxlength="80"`
      + ` placeholder="Key name, e.g. Backups" style="flex:1;min-width:180px;">`
    + `<select class="form-control" id="api-key-access" style="width:auto;">`
      + `<option value="read" selected>Read only</option>`
      + `<option value="write">Read and write</option>`
    + `</select>`
    + `<button type="button" class="btn btn-primary btn-sm" data-ak-create>Create key</button>`
    + `</div>`
    + _akSitePicker('data-ak-site');
}

// A shared picker for both cards: with no sites there is nothing to choose,
// and the server's "everything you can see" default stands.
function _akSitePicker(attr) {
  const sites = (_akState && _akState.sites) || [];
  if (!sites.length) return '';
  return `<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;">`
    + sites.map((s) => `<label class="totp-sub" style="display:flex;gap:6px;align-items:center;">`
      + `<input type="checkbox" ${attr}="${escAttr(String(s.id))}">`
      + `${escHtml(s.name)}</label>`).join('')
    + `</div>`;
}

function _akCheckedIds(attr) {
  return [...document.querySelectorAll(`input[${attr}]:checked`)].map((el) => el.getAttribute(attr));
}

function _akKeyRow(k) {
  const created = k.created_at ? new Date(k.created_at).toLocaleDateString() : '—';
  const used = k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : 'never';
  const sites = (k.sites || []).map((s) => s.name).join(', ') || 'All your sites';
  const sub = `vsk_${escHtml(k.prefix)}… · ${k.read_only ? 'Read only' : 'Read and write'}`
    + ` · ${escHtml(sites)} · created by ${escHtml(k.created_by)}`
    + ` · ${escHtml(created)} · last used ${escHtml(used)}`;
  const right = k.revoked_at ? '<span class="muted">Revoked</span>'
    : `<button type="button" class="btn btn-rose btn-sm" data-ak-revoke="${escAttr(String(k.id))}">Revoke</button>`;
  return `<div class="dash-card"${k.revoked_at ? ' style="opacity:.6"' : ''}>`
    + `<div class="dash-card-main">`
      + `<div class="dash-card-title">${escHtml(k.name)}</div>`
      + `<div class="dash-card-sub">${sub}</div>`
    + `</div>`
    + `<div class="dash-card-right">${right}</div>`
  + `</div>`;
}

delegateClick('[data-ak-create]', async () => {
  const input = document.getElementById('api-key-name');
  const name = (input?.value || '').trim();
  if (!name) { showToast('Give the key a name', 'error'); return; }
  const access = document.getElementById('api-key-access')?.value || 'read';
  let created;
  try {
    created = await apiJson(AK_KEYS_API, {
      method: 'POST',
      body: JSON.stringify({ name, site_ids: _akCheckedIds('data-ak-site'), read_only: access === 'read' }),
    });
  } catch (e) {
    showToast(e.message || 'Could not create the key', 'error');
    return;
  }
  if (input) input.value = '';
  _akShowPlainKey(created.key);
});

function _akShowPlainKey(raw) {
  const m = mountModal('api-key-plain', { variant: 'm-pop' });
  m.setBody(
    `<div class="modal-title">Copy your key now</div>`
    + `<p class="muted" style="margin-bottom:14px;">Vigil keeps only a hash of it `
      + `and cannot show it again.</p>`
    + `<input type="text" class="form-control totp-input" id="api-key-plain" readonly`
      + ` value="${escAttr(raw)}" style="font-size:13px;letter-spacing:normal;text-align:left;">`
    + `<div class="is-actions">`
      + `<button type="button" class="btn btn-mint btn-sm" data-ak-copy>Copy</button>`
      + `<button type="button" class="btn btn-outline btn-sm" data-ak-done>Done</button>`
    + `</div>`);
  m.modal.querySelector('[data-ak-copy]').onclick = async () => {
    const field = document.getElementById('api-key-plain');
    try {
      await navigator.clipboard.writeText(field ? field.value : '');
      showToast('Key copied', 'success');
    } catch (e) {
      showToast('Copy failed — select it and copy manually', 'error');
    }
  };
  m.modal.querySelector('[data-ak-done]').onclick = () => m.close();
  // However it closes — Done, the scrim, Esc — the key leaves the page with
  // the modal rather than sitting hidden in the DOM, and the list reloads.
  const forget = new MutationObserver(() => {
    if (m.modal.classList.contains('open')) return;
    forget.disconnect();
    m.setBody('');
    loadApiPane();
  });
  forget.observe(m.modal, { attributes: true, attributeFilter: ['class'] });
  m.open();
}

delegateClick('[data-ak-revoke]', async (el) => {
  const yes = await confirmModal(
    'Revoke this key? Anything using it stops working straight away.',
    { title: 'Revoke key', confirmText: 'Revoke', danger: true });
  if (!yes) return;
  try {
    await apiJson(`${AK_KEYS_API}${encodeURIComponent(el.dataset.akRevoke)}/revoke/`,
      { method: 'POST', body: '{}' });
  } catch (e) {
    showToast(e.message || 'Could not revoke the key', 'error');
    return;
  }
  showToast('Key revoked', 'success');
  loadApiPane();
});

/* ── Webhooks ──────────────────────────────────────────────────────────── */

function _renderWebhooks() {
  const host = document.getElementById('webhooks');
  if (!host || !_akState) return;
  const note = _akAbsence(_akState.hooksStatus);
  if (note) { host.innerHTML = `<p class="muted">${escHtml(note)}</p>`; return; }
  if (_akState.hooks === null) {
    host.innerHTML = `<p class="muted">${escHtml(_akState.hooksMessage || 'Could not load webhooks')}</p>`;
    return;
  }
  const form = _akFeatureActive('webhooks') ? _akHookForm()
    : '<p class="muted">Adding webhooks needs an SQSY Business licence.</p>';
  const rows = _akState.hooks.map(_akHookRow).join('');
  host.innerHTML = `<p class="muted" style="margin-bottom:14px;">Vigil posts signed JSON to `
    + `these addresses. Check the X-Vigil-Signature header: sha256 HMAC of the body `
    + `with the webhook&rsquo;s secret.</p>`
    + form
    + (rows ? `<div class="dash-cards" style="margin-top:16px;">${rows}</div>`
            : '<p class="muted" style="margin-top:14px;">No webhooks yet.</p>');
}

function _akHookForm() {
  return `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`
    + `<input type="text" class="form-control" id="wh-name" maxlength="80"`
      + ` placeholder="Name, e.g. Helpdesk" style="flex:1;min-width:140px;">`
    + `<input type="url" class="form-control" id="wh-url" style="flex:2;min-width:220px;"`
      + ` placeholder="https://example.com/hooks/vigil">`
    + `<button type="button" class="btn btn-primary btn-sm" data-wh-create>Add webhook</button>`
    + `</div>`
    + `<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;">`
    + AK_EVENTS.map(([name, label], i) => `<label class="totp-sub" style="display:flex;`
      + `gap:6px;align-items:center;">`
      + `<input type="checkbox" data-wh-event="${escAttr(name)}"${i < 2 ? ' checked' : ''}>`
      + `${escHtml(label)}</label>`).join('')
    + `</div>`
    + _akSitePicker('data-wh-site');
}

function _akEventLabel(name) {
  const found = AK_EVENTS.find((row) => row[0] === name);
  return found ? found[1] : name;
}

function _akDeliveryText(w) {
  const d = w.last_delivery;
  if (!d) return 'not sent yet';
  const at = d.created_at ? new Date(d.created_at).toLocaleString() : '—';
  if (d.status_code) return `last sent ${escHtml(at)}, HTTP ${escHtml(String(d.status_code))}`;
  return `last sent ${escHtml(at)}, failed: ${escHtml(d.error || 'no response')}`;
}

function _akHookRow(w) {
  const events = (w.events || []).map(_akEventLabel).join(', ');
  const sub = `${escHtml(w.url)} · ${escHtml(events)} · ${_akDeliveryText(w)}`;
  const right = `<button type="button" class="btn btn-outline btn-sm wh-act" data-wh-test="${escAttr(String(w.id))}">Send test</button>`
    + `<button type="button" class="btn btn-outline btn-sm wh-act" data-wh-log="${escAttr(String(w.id))}">Deliveries</button>`
    + `<button type="button" class="btn btn-outline btn-sm wh-act" data-wh-toggle="${escAttr(String(w.id))}"`
      + ` data-active="${escAttr(w.is_active ? '1' : '0')}">${w.is_active ? 'Pause' : 'Resume'}</button>`
    + `<button type="button" class="btn btn-outline btn-sm wh-act" data-wh-rotate="${escAttr(String(w.id))}">New secret</button>`
    + `<button type="button" class="btn btn-rose btn-sm wh-act" data-wh-delete="${escAttr(String(w.id))}">Delete</button>`;
  return `<div class="dash-card"${w.is_active ? '' : ' style="opacity:.6"'}>`
    + `<div class="dash-card-main">`
      + `<div class="dash-card-title">${escHtml(w.name)}</div>`
      + `<div class="dash-card-sub">${sub}</div>`
    + `</div>`
    + `<div class="dash-card-right">${right}</div>`
  + `</div>`;
}

delegateClick('[data-wh-create]', async () => {
  const nameInput = document.getElementById('wh-name');
  const urlInput = document.getElementById('wh-url');
  const name = (nameInput?.value || '').trim();
  const url = (urlInput?.value || '').trim();
  if (!name || !url) { showToast('Give the webhook a name and a URL', 'error'); return; }
  const events = [...document.querySelectorAll('input[data-wh-event]:checked')]
    .map((el) => el.dataset.whEvent);
  if (!events.length) { showToast('Pick at least one event', 'error'); return; }
  let created;
  try {
    created = await apiJson(AK_WEBHOOKS_API, {
      method: 'POST',
      body: JSON.stringify({ name, url, events, site_ids: _akCheckedIds('data-wh-site') }),
    });
  } catch (e) {
    showToast(e.message || 'Could not add the webhook', 'error');
    return;
  }
  if (nameInput) nameInput.value = '';
  if (urlInput) urlInput.value = '';
  _akShowSecret(created.secret, 'Copy your secret now');
});

delegateClick('[data-wh-test]', async (el) => {
  const id = encodeURIComponent(el.dataset.whTest);
  let result;
  try {
    result = await apiJson(`${AK_WEBHOOKS_API}${id}/test/`, { method: 'POST', body: '{}' });
  } catch (e) {
    showToast(e.message || 'Test failed', 'error');
    return;
  }
  showToast(result.error ? `Test failed: ${result.error}` : `Test sent: HTTP ${result.status_code}`,
    result.error ? 'error' : 'success');
  loadApiPane();
});

delegateClick('[data-wh-log]', async (el) => {
  const id = encodeURIComponent(el.dataset.whLog);
  let body;
  try {
    body = await apiJson(`${AK_WEBHOOKS_API}${id}/deliveries/`);
  } catch (e) {
    showToast(e.message || 'Could not load deliveries', 'error');
    return;
  }
  const rows = (body.deliveries || []).slice(0, 50);
  const m = mountModal('webhook-log', { wide: true });
  m.setBody(`<div class="modal-title">`
    + `<span>Deliveries</span>`
    + `<button class="modal-close" data-wh-log-close aria-label="Close">`
      + `<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`
      + `</button></div>`
    + (rows.length ? `<div class="dash-cards">${rows.map(_akDeliveryRow).join('')}</div>`
                   : '<p class="muted">Nothing has been sent yet.</p>'));
  m.modal.querySelector('[data-wh-log-close]').onclick = () => m.close();
  m.open();
});

function _akDeliveryRow(d) {
  const at = d.created_at ? new Date(d.created_at).toLocaleString() : '—';
  const outcome = d.status_code ? `HTTP ${escHtml(String(d.status_code))}` : 'failed';
  const ms = d.duration_ms === null || d.duration_ms === undefined ? '' : ` · ${escHtml(String(d.duration_ms))} ms`;
  return `<div class="dash-card">`
    + `<div class="dash-card-main">`
      + `<div class="dash-card-title">${escHtml(_akEventLabel(d.event))} · ${outcome}${ms}</div>`
      + `<div class="dash-card-sub">${escHtml(at)}</div>`
      + (d.error ? `<div class="dash-card-sub">${escHtml(d.error)}</div>` : '')
    + `</div>`
  + `</div>`;
}

delegateClick('[data-wh-toggle]', async (el) => {
  const id = encodeURIComponent(el.dataset.whToggle);
  try {
    await apiJson(`${AK_WEBHOOKS_API}${id}/`, {
      method: 'PATCH', body: JSON.stringify({ is_active: el.dataset.active !== '1' }),
    });
  } catch (e) {
    showToast(e.message || 'Could not change the webhook', 'error');
    return;
  }
  loadApiPane();
});

delegateClick('[data-wh-rotate]', async (el) => {
  const id = encodeURIComponent(el.dataset.whRotate);
  let body;
  try {
    body = await apiJson(`${AK_WEBHOOKS_API}${id}/rotate-secret/`, { method: 'POST', body: '{}' });
  } catch (e) {
    showToast(e.message || 'Could not create a new secret', 'error');
    return;
  }
  _akShowSecret(body.secret, 'Copy your new secret now');
});

delegateClick('[data-wh-delete]', async (el) => {
  const yes = await confirmModal(
    'Delete this webhook? Vigil stops posting to it and its delivery history goes too.',
    { title: 'Delete webhook', confirmText: 'Delete', danger: true });
  if (!yes) return;
  try {
    await apiJson(`${AK_WEBHOOKS_API}${encodeURIComponent(el.dataset.whDelete)}/`, { method: 'DELETE' });
  } catch (e) {
    showToast(e.message || 'Could not delete the webhook', 'error');
    return;
  }
  showToast('Webhook deleted', 'success');
  loadApiPane();
});

// Keys and secrets share one shape of dialog: a read-only box, a copy button,
// and a MutationObserver that empties it whichever way the modal closed.
function _akShowSecret(secret, title) {
  const m = mountModal('webhook-secret', { variant: 'm-pop' });
  m.setBody(
    `<div class="modal-title">${escHtml(title)}</div>`
    + `<p class="muted" style="margin-bottom:14px;">Vigil shows this secret once. `
      + `Verify each delivery with it: the X-Vigil-Signature header is a sha256 `
      + `HMAC of the body, sent as sha256=&lt;digest&gt;.</p>`
    + `<input type="text" class="form-control totp-input" id="webhook-secret-plain" readonly`
      + ` value="${escAttr(secret)}" style="font-size:13px;letter-spacing:normal;text-align:left;">`
    + `<div class="is-actions">`
      + `<button type="button" class="btn btn-mint btn-sm" data-wh-copy>Copy</button>`
      + `<button type="button" class="btn btn-outline btn-sm" data-wh-done>Done</button>`
    + `</div>`);
  m.modal.querySelector('[data-wh-copy]').onclick = async () => {
    const field = document.getElementById('webhook-secret-plain');
    try {
      await navigator.clipboard.writeText(field ? field.value : '');
      showToast('Secret copied', 'success');
    } catch (e) {
      showToast('Copy failed — select it and copy manually', 'error');
    }
  };
  m.modal.querySelector('[data-wh-done]').onclick = () => m.close();
  const forget = new MutationObserver(() => {
    if (m.modal.classList.contains('open')) return;
    forget.disconnect();
    m.setBody('');
    loadApiPane();
  });
  forget.observe(m.modal, { attributes: true, attributeFilter: ['class'] });
  m.open();
}
