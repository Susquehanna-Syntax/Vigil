// vigil-stacks.js
// Owns: Vigil-managed compose stacks on the Monitor page's Containers section
//   (M11) — the list, the editor (compose file + masked .env), revisions,
//   deploy and take-down. Free, one host at a time.
// HTML: #managed-stacks in templates/pages/_monitor.html; the editor modal is
//   built here on first use.
// Depends on: vigil-utils.js (escHtml, escAttr, apiJson, showToast, getCsrf).
// API: /api/v1/stacks/ (CRUD), /<id>/revisions/, /<id>/env/reveal/ (TOTP),
//      /<id>/deploy/ (TOTP), /<id>/remove/ (TOTP)

const stackEd = { hostId: null, stack: null, env: [], stacks: [] };

async function renderManagedStacks(hostId) {
  const wrap = document.getElementById('managed-stacks');
  if (!wrap) return;
  stackEd.hostId = hostId;
  let rows = [];
  try { rows = (await apiJson(`/api/v1/stacks/?host=${encodeURIComponent(hostId)}`)).results || []; }
  catch { rows = null; }
  if (rows === null) { wrap.innerHTML = ''; return; }   // not an admin: nothing to manage
  stackEd.stacks = rows;
  wrap.innerHTML = `<div class="managed-stacks-bar">
      <span class="managed-stacks-label">Managed by Vigil</span>
      ${rows.map(s => `<button class="chip chip-muted managed-stack-chip" type="button" data-stack-edit="${escAttr(s.id)}">${escHtml(s.name)} <span class="apps-zero">r${s.revision}</span></button>`).join('')}
      <button class="btn btn-xs btn-mint" type="button" data-stack-new>New stack</button>
    </div>`;
}

function _stackEls() {
  let modal = document.getElementById('stack-modal');
  if (!modal) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'stack-overlay';
    modal = document.createElement('div');
    modal.className = 'modal modal-wide';
    modal.id = 'stack-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'stack-title');
    modal.innerHTML = `<div class="modal-title"><span id="stack-title">New stack</span>
        <button class="modal-close" type="button" data-stack-close aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button></div>
      <div class="modal-body">
        <div class="bl-field"><label for="stack-name">Name</label>
          <input class="form-control" id="stack-name" maxlength="63" placeholder="media" autocomplete="off">
          <div class="bl-hint">Lowercase letters, digits, _ and -. It lives in /opt/vigil/stacks/&lt;name&gt; on the host.</div></div>
        <div class="bl-field"><label for="stack-compose">compose.yaml</label>
          <textarea class="form-control stack-compose" id="stack-compose" spellcheck="false"></textarea></div>
        <div class="bl-field"><label>.env <span class="pol-opt">(values stay hidden; leave one blank to keep it)</span></label>
          <div id="stack-env" class="stack-env"></div>
          <div class="stack-env-actions">
            <button class="btn btn-outline btn-xs" type="button" data-stack-env-add>Add variable</button>
            <button class="btn btn-ghost btn-xs" type="button" data-stack-env-reveal>Show values</button>
          </div></div>
        <div class="bl-field" id="stack-revisions-field" hidden><label>Revisions</label>
          <div id="stack-revisions" class="stack-revisions"></div></div>
        <div class="editor-error" id="stack-error"></div>
      </div>
      <div class="modal-actions">
        <button class="btn btn-ghost" type="button" data-stack-remove hidden>Take down…</button>
        <button class="btn btn-ghost" type="button" data-stack-close>Cancel</button>
        <button class="btn btn-lav" type="button" data-stack-deploy hidden>Deploy</button>
        <button class="btn btn-mint" type="button" data-stack-save>Save</button>
      </div>`;
    document.body.append(overlay, modal);
    overlay.addEventListener('click', closeStackEditor);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && modal.classList.contains('open')) closeStackEditor();
    });
  }
  return modal;
}

function _renderStackEnv() {
  const wrap = document.getElementById('stack-env');
  wrap.innerHTML = stackEd.env.map((e, i) => `<div class="stack-env-row" data-env-row="${i}">
      <input class="form-control" data-env-field="key" value="${escAttr(e.key)}" placeholder="KEY" aria-label="Variable name" autocomplete="off">
      <input class="form-control" data-env-field="value" type="${e.revealed ? 'text' : 'password'}" value="${escAttr(e.value || '')}"
        placeholder="${e.stored ? '•••••• (unchanged)' : 'value'}" aria-label="Value of ${escAttr(e.key || 'variable')}" autocomplete="off">
      <button class="btn btn-ghost btn-xs" type="button" data-env-remove="${i}" aria-label="Remove">Remove</button>
    </div>`).join('') || '<div class="apps-empty-state">No variables.</div>';
}

async function openStackEditor(stackId) {
  const modal = _stackEls();
  const stack = stackId ? stackEd.stacks.find(s => s.id === stackId) : null;
  stackEd.stack = stack;
  stackEd.env = stack ? stack.env.map(e => ({ key: e.key, value: '', stored: true })) : [];
  document.getElementById('stack-title').textContent = stack ? `Stack ${stack.name}` : 'New stack';
  const name = document.getElementById('stack-name');
  name.value = stack ? stack.name : '';
  name.disabled = !!stack;
  document.getElementById('stack-compose').value = stack ? stack.compose_yaml
    : 'services:\n  app:\n    image: nginx:stable\n    restart: unless-stopped\n';
  document.getElementById('stack-error').classList.remove('show');
  modal.querySelector('[data-stack-deploy]').hidden = !stack;
  modal.querySelector('[data-stack-remove]').hidden = !stack;
  document.getElementById('stack-revisions-field').hidden = !stack;
  const report = (stack && stack.adopt_report) || {};
  const recreate = report.recreate || [];
  if (stack && stack.adopted) {
    const err = document.getElementById('stack-error');
    err.textContent = recreate.length
      ? `Adopted from ${stack.working_dir}/${stack.compose_file}. Deploying this file would recreate: ${recreate.join(', ')} — their running config differs from the file.`
      : `Adopted from ${stack.working_dir}/${stack.compose_file}. Deploying it unchanged recreates nothing.`;
    err.classList.add('show');
  }
  _renderStackEnv();
  document.getElementById('stack-overlay').classList.add('open');
  modal.classList.add('open');
  (stack ? document.getElementById('stack-compose') : name).focus();
  if (stack) _loadStackRevisions();
}

function closeStackEditor() {
  document.getElementById('stack-overlay')?.classList.remove('open');
  document.getElementById('stack-modal')?.classList.remove('open');
  stackEd.env = [];   // forget any revealed values
}

function _stackError(message) {
  const el = document.getElementById('stack-error');
  el.textContent = message;
  el.classList.add('show');
}

async function _loadStackRevisions() {
  const wrap = document.getElementById('stack-revisions');
  try {
    const revs = (await apiJson(`/api/v1/stacks/${stackEd.stack.id}/revisions/`)).results || [];
    wrap.innerHTML = revs.map(r => `<div class="apps-detail-item">
        <span class="apps-name">r${r.number}</span><span class="apps-detail-scope">${escHtml(r.note || '')} · ${escHtml(r.created_by || '')} · ${escHtml(timeAgo(r.created_at) || '')}</span>
        ${r.number !== stackEd.stack.revision ? `<button class="btn btn-ghost btn-xs" type="button" data-stack-load-rev="${r.number}">Load this file</button>` : '<span class="chip chip-mint">current</span>'}
      </div>`).join('');
    stackEd.revisions = revs;
  } catch { wrap.innerHTML = ''; }
}

async function saveStack() {
  const compose = document.getElementById('stack-compose').value;
  const env = stackEd.env.filter(e => e.key.trim()).map(e => (e.stored && !e.value && !e.revealed)
    ? { key: e.key.trim(), keep: true } : { key: e.key.trim(), value: e.value || '' });
  const editing = stackEd.stack;
  const body = editing ? { compose_yaml: compose, env, note: 'edited' }
    : { host_id: stackEd.hostId, name: document.getElementById('stack-name').value.trim(), compose_yaml: compose, env };
  try {
    const saved = await apiJson(editing ? `/api/v1/stacks/${editing.id}/` : '/api/v1/stacks/',
      { method: editing ? 'PUT' : 'POST', body: JSON.stringify(body) });
    showToast(`Saved ${saved.name} as revision ${saved.revision} — deploy it to apply`, 'success');
    await renderManagedStacks(stackEd.hostId);
    openStackEditor(saved.id);
  } catch (e) { _stackError(e.message); }
}

async function _stackTotpAction(path, payload, done) {
  const code = window.prompt('This needs your TOTP code:');
  if (!code) return null;
  try {
    return await apiJson(path, { method: 'POST', body: JSON.stringify({ ...payload, totp: code.trim() }) });
  } catch (e) { _stackError(e.message); return null; }
}

async function revealStackEnv() {
  if (!stackEd.stack) return;
  const body = await _stackTotpAction(`/api/v1/stacks/${stackEd.stack.id}/env/reveal/`, {});
  if (!body) return;
  const values = Object.fromEntries(body.env.map(e => [e.key, e.value]));
  stackEd.env = stackEd.env.map(e => (e.stored && e.key in values) ? { ...e, value: values[e.key], revealed: true } : e);
  _renderStackEnv();
}

async function deployStack() {
  const body = await _stackTotpAction(`/api/v1/stacks/${stackEd.stack.id}/deploy/`, {});
  if (body) showToast(`Deploying ${stackEd.stack.name} r${stackEd.stack.revision} — the agent picks it up at its next check-in`, 'success');
}

async function removeStack() {
  const wipe = window.confirm(`Take down ${stackEd.stack.name}? OK also deletes its folder on the host; Cancel keeps the files.`);
  const body = await _stackTotpAction(`/api/v1/stacks/${stackEd.stack.id}/remove/`, { delete_files: wipe });
  if (body) showToast(`Taking down ${stackEd.stack.name}`, 'success');
}

/* ── Wiring ──────────────────────────────────────────────────────────── */
delegateClick('[data-stack-new]', () => openStackEditor(null));
delegateClick('[data-stack-edit]', (el) => openStackEditor(el.dataset.stackEdit));
delegateClick('[data-stack-close]', () => closeStackEditor());
delegateClick('[data-stack-save]', () => saveStack());
delegateClick('[data-stack-deploy]', () => deployStack());
delegateClick('[data-stack-remove]', () => removeStack());
delegateClick('[data-stack-env-reveal]', () => revealStackEnv());
delegateClick('[data-stack-env-add]', () => {
  stackEd.env.push({ key: '', value: '', stored: false });
  _renderStackEnv();
});
delegateClick('[data-env-remove]', (el) => {
  stackEd.env.splice(parseInt(el.dataset.envRemove, 10), 1);
  _renderStackEnv();
});
delegateClick('[data-stack-load-rev]', (el) => {
  const rev = (stackEd.revisions || []).find(r => r.number === parseInt(el.dataset.stackLoadRev, 10));
  if (rev) {
    document.getElementById('stack-compose').value = rev.compose_yaml;
    showToast(`Loaded r${rev.number}'s compose file — Save makes it the next revision`, 'success');
  }
});
document.addEventListener('input', (ev) => {
  const row = ev.target.closest && ev.target.closest('[data-env-row]');
  if (!row) return;
  const entry = stackEd.env[parseInt(row.dataset.envRow, 10)];
  if (entry) entry[ev.target.dataset.envField] = ev.target.value;
});
