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
  const testable = ['greenbone', 'nessus', 'email'].includes(group.id);
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

