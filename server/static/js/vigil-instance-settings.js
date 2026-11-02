// vigil-instance-settings.js
// Owns: the Settings panes backed by /api/v1/instance-settings/ — Scanners,
//   Email, Retention, and the Timezone card inside Display. One renderer
//   drives all of them; the server's field registry decides what appears.
// HTML: templates/pages/_settings.html — any <div class="is-group" data-group="...">
// Depends on: vigil-utils.js (apiJson, showToast, escHtml)
//
// Secrets are write-only by construction: the API never sends one back, so a
// password box renders empty with a note saying one is stored, and posting it
// empty leaves the stored value alone. Clearing is a separate, explicit act.

let _isGroups = null;
const _isCleared = new Set();

async function loadInstanceSettings(force) {
  if (!document.querySelector('.is-group')) return;
  if (_isGroups && !force) { _isRenderAll(); return; }
  try {
    const data = await apiJson('/api/v1/instance-settings/');
    _isGroups = data.groups || [];
  } catch (e) {
    showToast(e.message || 'Could not load settings', 'error');
    return;
  }
  _isRenderAll();
}

function _isRenderAll() {
  document.querySelectorAll('.is-group').forEach((host) => {
    const group = (_isGroups || []).find((g) => g.id === host.dataset.group);
    if (group) host.innerHTML = _isRenderGroup(group);
  });
}

function _isRenderGroup(group) {
  const testable = ['greenbone', 'nessus', 'jackil', 'email'].includes(group.id);
  return (
    `<div class="totp-card">` +
      (group.help ? `<div class="totp-sub" style="margin-bottom:16px;">${escHtml(group.help)}</div>` : '') +
      `<div class="is-fields">${group.fields.map(_isRenderField).join('')}</div>` +
      `<div class="is-actions">` +
        `<button type="button" class="btn btn-mint btn-sm" data-act="saveInstanceSettings" data-a1="${group.id}">Save</button>` +
        (testable
          ? `<button type="button" class="btn btn-outline btn-sm" data-act="testInstanceIntegration" data-a1="${group.id}">Test connection</button>`
          : '') +
        `<span class="is-result muted" id="is-result-${group.id}"></span>` +
      `</div>` +
    `</div>`
  );
}

function _isRenderField(f) {
  const id = 'is-f-' + f.name;
  const locked = f.env_locked;
  const disabled = locked ? ' disabled' : '';
  let control;

  if (f.kind === 'bool') {
    control =
      `<label class="is-check"><input type="checkbox" id="${id}" data-name="${f.name}"` +
      `${f.value ? ' checked' : ''}${disabled}><span>${escHtml(f.label)}</span></label>`;
  } else if (f.kind === 'choice') {
    const opts = (f.choices || []).map((c) =>
      `<option value="${escAttr(c)}"${c === f.value ? ' selected' : ''}>${escHtml(c)}</option>`).join('');
    control = `<select class="form-control" id="${id}" data-name="${f.name}"${disabled}>${opts}</select>`;
  } else if (f.kind === 'secret') {
    control =
      `<input type="password" class="form-control" id="${id}" data-name="${f.name}"` +
      ` autocomplete="new-password" placeholder="${f.is_set ? 'stored — leave blank to keep' : ''}"${disabled}>`;
  } else {
    const type = (f.kind === 'int' || f.kind === 'float') ? 'number' : 'text';
    const step = f.kind === 'float' ? ' step="any"' : '';
    control =
      `<input type="${type}"${step} class="form-control" id="${id}" data-name="${f.name}"` +
      ` value="${escAttr(f.value === null || f.value === undefined ? '' : String(f.value))}"` +
      ` placeholder="${escAttr(f.placeholder || '')}"${disabled}>`;
  }

  const notes = [];
  if (locked) notes.push(`Set by <code>${escHtml(f.name)}</code> in the environment — edit it there.`);
  else if (f.help) notes.push(escHtml(f.help));
  if (f.kind === 'secret' && f.is_set && !locked) {
    notes.push(`<button type="button" class="is-clear" data-act="clearInstanceSecret" data-a1="${f.name}">Clear stored value</button>`);
  }

  return (
    `<div class="is-field${locked ? ' is-locked' : ''}${f.kind === 'bool' ? ' is-field-check' : ''}">` +
      (f.kind === 'bool' ? '' : `<label class="deploy-input-label" for="${id}">${escHtml(f.label)}</label>`) +
      control +
      (notes.length ? `<div class="is-note">${notes.join(' ')}</div>` : '') +
    `</div>`
  );
}

function clearInstanceSecret(name) {
  _isCleared.add(name);
  const input = document.getElementById('is-f-' + name);
  if (input) {
    input.value = '';
    input.placeholder = 'will be cleared on save';
  }
  showToast('Cleared on save.');
}

async function saveInstanceSettings(groupId) {
  const group = (_isGroups || []).find((g) => g.id === groupId);
  if (!group) return;
  const values = {};
  const clear = [];

  // Only what actually changed is sent. The form renders effective values,
  // which include code defaults nobody chose — EMAIL_HOST reads "localhost"
  // on a fresh install. Posting those back would store them as deliberate
  // choices, and storing an SMTP host is what switches mail off the console
  // backend: saving the From address alone would have broken alert email.
  group.fields.forEach((f) => {
    if (f.env_locked) return;
    const el = document.getElementById('is-f-' + f.name);
    if (!el) return;
    if (_isCleared.has(f.name)) { clear.push(f.name); return; }
    if (f.kind === 'bool') {
      if (el.checked !== !!f.value) values[f.name] = el.checked;
    } else if (f.kind === 'secret') {
      if (el.value) values[f.name] = el.value;
    } else {
      const was = (f.value === null || f.value === undefined) ? '' : String(f.value);
      if (el.value !== was) values[f.name] = el.value;
    }
  });

  if (!Object.keys(values).length && !clear.length) {
    _isSetResult(groupId, 'No changes.', '');
    return;
  }

  _isSetResult(groupId, 'Saving…', '');
  let body;
  try {
    const resp = await fetch('/api/v1/instance-settings/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      body: JSON.stringify({ values, clear }),
    });
    body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      // Name the field that was refused. "Some values were rejected" sends
      // someone hunting through a form of a dozen boxes.
      const named = Object.entries(body.errors || {})
        .map(([name, why]) => `${_isLabelFor(groupId, name)}: ${why}`);
      _isSetResult(groupId, named.join(' · ') || body.detail || 'Save failed', 'bad');
      return;
    }
  } catch (e) {
    _isSetResult(groupId, e.message || 'Save failed', 'bad');
    return;
  }

  clear.forEach((n) => _isCleared.delete(n));
  // Re-render first: it replaces the whole card, result line included, so a
  // message written before the reload would vanish the moment it appeared.
  await loadInstanceSettings(true);
  _isSetResult(groupId, body.changed.length
    ? `Saved ${body.changed.length} setting${body.changed.length === 1 ? '' : 's'}.`
    : 'No changes.', 'ok');
}

function _isSetResult(groupId, text, tone) {
  const el = document.getElementById('is-result-' + groupId);
  if (!el) return;
  el.textContent = text;
  el.className = 'is-result ' + (tone === 'ok' ? 'is-ok' : tone === 'bad' ? 'is-bad' : 'muted');
}

function _isLabelFor(groupId, name) {
  const group = (_isGroups || []).find((g) => g.id === groupId);
  const field = group && group.fields.find((f) => f.name === name);
  return field ? field.label : name;
}

async function testInstanceIntegration(target) {
  _isSetResult(target, 'Testing…', '');
  try {
    const body = await apiJson('/api/v1/instance-settings/test/', {
      method: 'POST',
      body: JSON.stringify({ target }),
    });
    _isSetResult(target, body.detail, body.ok ? 'ok' : 'bad');
  } catch (e) {
    _isSetResult(target, e.message || 'Test failed', 'bad');
  }
}


/* ── Jackil: the tickets it has actually opened ───────────────────────────
   A saved form proves nothing. This is the evidence the integration works,
   and the first place to look when someone says it stopped. */
async function loadJackilTickets() {
  const host = document.getElementById('jackil-tickets');
  if (!host) return;
  let data;
  try {
    data = await apiJson('/api/v1/jackil/tickets/');
  } catch (e) {
    host.innerHTML = `<p class="muted">${escHtml(e.message || 'Could not load tickets')}</p>`;
    return;
  }
  if (!data.tickets.length) {
    host.innerHTML = '<p class="muted">No tickets yet. Vigil opens one the next '
      + 'time an alert fires at or above the severity set above.</p>';
    return;
  }
  const rows = data.tickets.map((t) => {
    const edge = t.severity === 'critical' ? 'rose' : t.severity === 'warning' ? 'lemon' : 'sky';
    const title = `#${t.ticket_id} · ${t.host}`;
    const link = t.url
      ? `<a href="${escAttr(t.url)}" target="_blank" rel="noopener">Open in Jackil</a>`
      : '';
    return `<div class="dash-card edge-${edge}">`
      + `<div class="dash-card-main">`
        + `<div class="dash-card-title">${escHtml(title)}</div>`
        + `<div class="dash-card-sub">${escHtml(t.message)}</div>`
      + `</div>`
      + `<div class="dash-card-right">${link}`
        + `<div class="dash-card-sub">${t.cleared_at ? 'cleared' : 'open'}</div>`
      + `</div>`
    + `</div>`;
  }).join('');
  host.innerHTML = `<div class="dash-cards">${rows}</div>`
    + `<p class="muted" style="margin-top:12px;">${data.count} ticket${data.count === 1 ? '' : 's'} opened in total.</p>`;
}

/* ── Jackil: integration keys ──────────────────────────────────────────────
   A key is a standing read grant as its creator, so the card says that in
   words before the button that makes one. The plaintext comes back exactly
   once, from the mint response; it is written into the modal's box and into
   nothing else — not a variable that outlives it, not storage. */
const JK_KEYS_API = '/api/v1/jackil/integration-keys/';

async function loadJackilKeys() {
  const host = document.getElementById('jackil-keys');
  if (!host) return;
  let data;
  try {
    data = await apiJson(JK_KEYS_API);
  } catch (e) {
    // A scoped non-admin reaches the pane and gets 403 from the API. Saying
    // "Only an admin" is truer than echoing the server's machinery words.
    host.innerHTML = /admin/i.test(e.message || '')
      ? '<p class="muted">Only an admin can manage integration keys.</p>'
      : `<p class="muted">${escHtml(e.message || 'Could not load keys')}</p>`;
    return;
  }
  const form = `<p class="totp-sub" style="margin-bottom:14px;">A key lets Jackil `
    + `look up hosts, tasks, playbooks, runs, automations and devices as you, and `
    + `read dashboard widgets. It cannot change anything until you tick a key's `
    + `box below.</p>`
    + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">`
      + `<input type="text" class="form-control" id="jackil-key-name" maxlength="80"`
        + ` placeholder="Key name, e.g. Helpdesk" style="flex:1;min-width:180px;">`
      + `<button type="button" class="btn btn-primary btn-sm" data-jk-create>Create key</button>`
    + `</div>`;
  const rows = (data.keys || []).map(_jkRow).join('');
  host.innerHTML = form
    + (rows ? `<div class="dash-cards" style="margin-top:16px;">${rows}</div>`
            : '<p class="muted" style="margin-top:14px;">No keys yet.</p>');
}

function _jkRow(k) {
  const created = k.created_at ? new Date(k.created_at).toLocaleDateString() : '—';
  // "Last used" is the one line that tells an admin a key is load-bearing
  // before they revoke it, so a revoked key keeps showing its last use.
  const used = k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : 'never';
  const sub = `vjk_${escHtml(k.prefix)}… · created by ${escHtml(k.created_by)}`
    + ` · ${escHtml(created)} · last used ${escHtml(used)}`;
  // The write grant is a checkbox rather than a button: it is a state an admin
  // sets, and seeing it ticked is what stops them wondering why Jackil can add
  // a printer. A revoked key shows its last state, disabled.
  const write = `<label style="display:inline-flex;align-items:center;gap:5px;`
    + `font-size:11px;white-space:nowrap;">`
    + `<input type="checkbox" data-jk-write="${escAttr(String(k.id))}"`
      + `${k.can_write_devices ? ' checked' : ''}${k.revoked_at ? ' disabled' : ''}>`
    + `Can change devices</label>`;
  const right = k.revoked_at ? '<span class="muted">Revoked</span>'
    : `<button type="button" class="btn btn-rose btn-sm wh-act" data-jk-revoke="${escAttr(String(k.id))}">Revoke</button>`;
  // Dimmed rather than flagged with a coloured edge: a revoked key is not an
  // alert, it is history.
  return `<div class="dash-card"${k.revoked_at ? ' style="opacity:.6"' : ''}>`
    + `<div class="dash-card-main">`
      + `<div class="dash-card-title">${escHtml(k.name)}</div>`
      + `<div class="dash-card-sub">${sub}</div>`
    + `</div>`
    + `<div class="dash-card-right">${write}${right}</div>`
  + `</div>`;
}

delegateClick('[data-jk-create]', async () => {
  const input = document.getElementById('jackil-key-name');
  const name = (input?.value || '').trim();
  if (!name) { showToast('Give the key a name', 'error'); return; }
  let created;
  try {
    created = await apiJson(JK_KEYS_API, { method: 'POST', body: JSON.stringify({ name }) });
  } catch (e) {
    showToast(e.message || 'Could not create the key', 'error');
    return;
  }
  if (input) input.value = '';
  _jkShowPlainKey(created.key);
});

function _jkShowPlainKey(raw) {
  const m = mountModal('jackil-key', { variant: 'm-pop' });
  m.setBody(
    `<div class="modal-title">Copy your key now</div>`
    + `<p class="muted" style="margin-bottom:14px;">Vigil keeps only a hash of it `
      + `and cannot show it again.</p>`
    + `<input type="text" class="form-control totp-input" id="jackil-key-plain" readonly`
      + ` value="${escAttr(raw)}" style="font-size:13px;letter-spacing:normal;text-align:left;">`
    + `<div class="is-actions">`
      + `<button type="button" class="btn btn-mint btn-sm" data-jk-copy>Copy</button>`
      + `<button type="button" class="btn btn-outline btn-sm" data-jk-done>Done</button>`
    + `</div>`);
  // The key is read back out of the box when it is copied rather than kept in
  // a closure: the modal is the only place it exists.
  m.modal.querySelector('[data-jk-copy]').onclick = async () => {
    const field = document.getElementById('jackil-key-plain');
    try {
      await navigator.clipboard.writeText(field ? field.value : '');
      showToast('Key copied', 'success');
    } catch (e) {
      showToast('Copy failed — select it and copy manually', 'error');
    }
  };
  m.modal.querySelector('[data-jk-done]').onclick = () => m.close();
  // Done, a scrim click and Esc all close it by dropping `.open`; whichever
  // it was, the key leaves the page with the modal instead of sitting hidden
  // in the DOM for the rest of the session, and the list picks up the new row.
  const forget = new MutationObserver(() => {
    if (m.modal.classList.contains('open')) return;
    forget.disconnect();
    m.setBody('');
    loadJackilKeys();
  });
  forget.observe(m.modal, { attributes: true, attributeFilter: ['class'] });
  m.open();
}

delegateClick('[data-jk-revoke]', async (el) => {
  const yes = await confirmModal(
    'Revoke this key? Jackil stops reading Vigil with it straight away.',
    { title: 'Revoke key', confirmText: 'Revoke', danger: true });
  if (!yes) return;
  try {
    await apiJson(`${JK_KEYS_API}${encodeURIComponent(el.dataset.jkRevoke)}/revoke/`,
      { method: 'POST', body: '{}' });
  } catch (e) {
    showToast(e.message || 'Could not revoke the key', 'error');
    return;
  }
  showToast('Key revoked', 'success');
  loadJackilKeys();
});

// A checkbox, not a delegated click: the click would fire on a disabled box
// too and the server would answer 403 for a revoked key the row already says
// is revoked.
document.addEventListener('change', async (ev) => {
  const box = ev.target.closest && ev.target.closest('[data-jk-write]');
  if (!box) return;
  const enabled = box.checked;
  try {
    await apiJson(`${JK_KEYS_API}${encodeURIComponent(box.dataset.jkWrite)}/devices/`,
      { method: 'POST', body: JSON.stringify({ enabled }) });
  } catch (e) {
    box.checked = !enabled;
    showToast(e.message || 'Could not change what the key may do', 'error');
    return;
  }
  showToast(enabled ? 'The key can now change devices' : 'The key can no longer change devices',
    'success');
});
