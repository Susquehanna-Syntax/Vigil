// vigil-stacks.js
// Owns: Vigil-managed compose stacks on the Containers page
//   (M11) — the list, and the full-screen stack editor page
//   (pages/_stack_editor.html): the compose file with a highlighted editor,
//   live validation, the masked .env, revisions, deploy and take-down.
//   Free, one host at a time.
// HTML: #managed-stacks in templates/pages/_containers.html;
//   #page-stack-editor in templates/pages/_stack_editor.html.
// Depends on: vigil-utils.js (escHtml, escAttr, apiJson, showToast, getCsrf,
//   confirmModal, totpPrompt, yamlToHtml, timeAgo, mountModal), vigil-nav.js
//   (navigateTo — wrapped below, and it needs every .page section to exist, so
//   this file loads after base.html's content block is parsed).
// API: /api/v1/stacks/ (CRUD), /validate/, /<id>/revisions/, /<id>/env/reveal/
//      (TOTP), /<id>/deploy/ (TOTP), /<id>/remove/ (TOTP), /<id>/pull/ (Git),
//      /git-credentials/ (list, add with TOTP, delete)

const stackEd = {
  hostId: null, stack: null, env: [], stacks: [], revisions: [], dirty: false,
  source: 'editor', gitCreds: null, gitSnapshot: null,
};
let _stackCheckSeq = 0;
let _stackCheckTimer = null;

const _NEW_COMPOSE = 'services:\n  app:\n    image: nginx:stable\n    restart: unless-stopped\n';
const DEPLOY_SAVE_HINT = 'Save first: deploy ships a saved revision';

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

function _renderStackEnv() {
  const wrap = document.getElementById('stack-env');
  // Values go in as .value after the markup is built: never interpolate one
  // into an attribute — a revealed secret must not become markup.
  wrap.innerHTML = stackEd.env.map((e, i) => `<div class="stack-env-row" data-env-row="${i}">
      <input class="form-control" data-env-field="key" value="${escAttr(e.key)}" placeholder="KEY" aria-label="Variable name" autocomplete="off">
      <input class="form-control" data-env-field="value" type="${e.revealed ? 'text' : 'password'}"
        placeholder="${e.stored ? '•••••• (unchanged)' : 'value'}" aria-label="Value of ${escAttr(e.key || 'variable')}" autocomplete="off">
      <button class="btn btn-ghost btn-xs" type="button" data-env-remove="${i}" aria-label="Remove">Remove</button>
    </div>`).join('') || '<div class="apps-empty-state">No variables.</div>';
  wrap.querySelectorAll('[data-env-row]').forEach((row) => {
    const entry = stackEd.env[parseInt(row.dataset.envRow, 10)];
    if (!entry) return;
    row.querySelector('[data-env-field="key"]').value = entry.key || '';
    row.querySelector('[data-env-field="value"]').value = entry.value || '';
  });
}

function _stedEl(id) { return document.getElementById(id); }

function _stedHostLabel(stack) {
  if (stack && stack.hostname) return stack.hostname;
  if (stackEd.hostId) {
    const opt = document.querySelector(`#containers-host option[value="${CSS.escape(stackEd.hostId)}"]`);
    if (opt) return opt.textContent.trim();
  }
  return '';
}

function _stedGutter(lines) {
  let out = '';
  for (let i = 1; i <= lines; i += 1) {
    out += `<div class="sted-ln${i === stackEd.errorLine ? ' is-error' : ''}">${i}</div>`;
  }
  return out;
}

function _syncStackEditor() {
  const ta = _stedEl('stack-compose');
  const hl = _stedEl('sted-hl');
  const gutter = _stedEl('sted-gutter');
  if (!ta || !hl || !gutter) return;
  hl.innerHTML = yamlToHtml(ta.value) + '\n';   // trailing NL keeps last line visible
  gutter.innerHTML = _stedGutter(ta.value.split('\n').length);
  _mirrorStackScroll();
}

function _mirrorStackScroll() {
  const ta = _stedEl('stack-compose');
  const hl = _stedEl('sted-hl');
  const gutter = _stedEl('sted-gutter');
  if (!ta || !hl || !gutter) return;
  hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft;
  gutter.scrollTop = ta.scrollTop;
}

function _stedButtons() {
  const editing = !!stackEd.stack;
  document.querySelector('.sted [data-stack-deploy]').hidden = !editing;
  document.querySelector('.sted [data-stack-remove]').hidden = !editing;
  _stedEl('stack-revisions-field').hidden = !editing;
  _stedEl('stack-name-field').hidden = editing;
  _updateDeployButton();
}

function _updateDeployButton() {
  const btn = document.querySelector('.sted [data-stack-deploy]');
  if (!btn || btn.hidden) return;   // a new stack has no Deploy to disable
  btn.disabled = stackEd.dirty;
  btn.title = stackEd.dirty ? DEPLOY_SAVE_HINT : '';
}

function _markStackDirty() {
  stackEd.dirty = true;
  _updateDeployButton();
}

function _clearStackDirty() {
  stackEd.dirty = false;
  _updateDeployButton();
}

async function openStackEditor(stackId) {
  const stack = stackId ? stackEd.stacks.find(s => s.id === stackId) : null;
  stackEd.stack = stack;
  stackEd.env = stack ? stack.env.map(e => ({ key: e.key, value: '', stored: true })) : [];
  stackEd.revisions = [];
  stackEd.gitSnapshot = null;
  stackEd.errorLine = null;
  stackEd.source = (stack && stack.git) ? 'git' : 'editor';
  _clearStackDirty();
  _stedEl('sted-host').textContent = _stedHostLabel(stack);
  _stedEl('sted-name-label').textContent = stack ? stack.name : 'New stack';
  const name = _stedEl('stack-name');
  name.value = stack ? stack.name : '';
  name.disabled = !!stack;
  _stedEl('stack-compose').value = stack ? stack.compose_yaml : _NEW_COMPOSE;
  _applyStackSource();
  if (stack && stack.git) _fillGitFields(stack.git);
  const err = _stedEl('stack-error');
  err.textContent = '';
  err.classList.remove('show');
  const report = (stack && stack.adopt_report) || {};
  const recreate = report.recreate || [];
  if (stack && stack.adopted) {
    err.textContent = recreate.length
      ? `Adopted from ${stack.working_dir}/${stack.compose_file}. Deploying this file would recreate: ${recreate.join(', ')} — their running config differs from the file.`
      : `Adopted from ${stack.working_dir}/${stack.compose_file}. Deploying it unchanged recreates nothing.`;
    err.classList.add('show');
  }
  _renderStackEnv();
  _stedButtons();
  _syncStackEditor();
  _setStackStatus(null);
  navigateTo('stack-editor');
  (stack ? _stedEl('stack-compose') : name).focus();
  if (stack) _loadStackRevisions();
  _loadGitCredentials();
  _validateStack();
}

async function closeStackEditor() {
  if (stackEd.dirty) {
    const discard = await confirmModal('Discard unsaved changes to this stack?',
      { danger: true, confirmText: 'Discard' });
    if (!discard) return;
  }
  stackEd.env = [];   // forget any revealed values
  _clearStackDirty();
  navigateTo('containers');
  if (stackEd.hostId) renderManagedStacks(stackEd.hostId);
}

function _stackError(message) {
  const el = _stedEl('stack-error');
  el.textContent = message;
  el.classList.add('show');
}

/* ── Live validation (QA-19: POST /api/v1/stacks/validate/) ─────────── */
function _scheduleStackCheck() {
  clearTimeout(_stackCheckTimer);
  _stackCheckTimer = setTimeout(_validateStack, 400);
}

function _setStackStatus(problems) {
  const pill = _stedEl('sted-status');
  if (!pill) return;
  if (problems === null) {
    pill.className = 'sted-status';
    pill.textContent = 'checking…';
    return;
  }
  pill.className = 'sted-status ' + (problems ? 'is-bad' : 'is-good');
  pill.textContent = problems ? `${problems} problem(s)` : 'valid';
}

function _renderStackCheck(r) {
  const wrap = _stedEl('sted-check');
  if (!wrap) return;
  const problems = (r.ok ? 0 : 1) + (r.missing_env || []).length;
  let html = '';
  if (r.ok) {
    const services = r.services || [];
    html += `<div class="sted-check-ok">✓ valid · ${services.length} service(s)</div>`;
    if (services.length) {
      html += `<div class="sted-services">${services.map(s => `<span class="chip chip-muted sted-service">${escHtml(s.name)}`
        + `<span class="sted-service-img">${escHtml(s.build ? 'build' : (s.image || ''))}</span></span>`).join('')}</div>`;
    }
  } else {
    html += `<div class="sted-check-bad">✕ ${escHtml(r.error || 'This file does not validate.')}`
      + (r.line ? `<span class="sted-check-line">line ${r.line}</span>` : '') + '</div>';
  }
  (r.missing_env || []).forEach((v) => {
    html += `<div class="sted-missing">⚠ ${escHtml(v)} is used but not set `
      + `<button class="btn btn-ghost btn-xs" type="button" data-sted-add-env="${escAttr(v)}" `
      + `aria-label="Add ${escAttr(v)} to the .env">Add ${escHtml(v)}</button></div>`;
  });
  wrap.innerHTML = html;
  stackEd.errorLine = r.ok ? null : (r.line || null);
  _syncStackEditor();
  _setStackStatus(problems);
}

async function _validateStack() {
  clearTimeout(_stackCheckTimer);
  const ta = _stedEl('stack-compose');
  if (!ta) return;
  if (ta.closest('.sted-editor').hidden) { _setStackStatus(null); return; }
  const seq = ++_stackCheckSeq;
  _setStackStatus(null);
  let r;
  try {
    r = await apiJson('/api/v1/stacks/validate/', {
      method: 'POST',
      body: JSON.stringify({ compose_yaml: ta.value, env_keys: stackEd.env.map(e => e.key.trim()).filter(Boolean) }),
    });
  } catch { return; }
  if (seq !== _stackCheckSeq) return;                       // a newer keystroke already answered
  if (!_stedEl('sted-check')) return;
  _renderStackCheck(r);
}

async function _loadStackRevisions() {
  const wrap = _stedEl('stack-revisions');
  if (!stackEd.stack) { wrap.innerHTML = ''; return; }   // saved-and-closed beats the in-flight fetch
  try {
    const revs = (await apiJson(`/api/v1/stacks/${stackEd.stack.id}/revisions/`)).results || [];
    const isGit = !!stackEd.stack.git;
    wrap.innerHTML = revs.map(r => `<div class="apps-detail-item">
        <span class="apps-name">r${r.number}${r.git_commit ? ` <code class="sted-git-chip">${escHtml(r.git_commit.slice(0, 12))}</code>` : ''}</span><span class="apps-detail-scope">${escHtml(r.note || '')} · ${escHtml(r.created_by || '')} · ${escHtml(timeAgo(r.created_at) || '')}</span>
        ${!isGit && r.number !== stackEd.stack.revision ? `<button class="btn btn-ghost btn-xs" type="button" data-stack-load-rev="${r.number}">Load this file</button>` : '<span class="chip chip-mint">current</span>'}
      </div>`).join('');
    stackEd.revisions = revs;
  } catch { wrap.innerHTML = ''; }
}

/* ── Source: written here, or a Git repository ───────────────────────── */

/* The five Git fields a save sends, read in the one place they are touched. */
function _gitFields() {
  const pick = (id) => _stedEl(id).value.trim();
  const credential = _stedEl('sted-git-cred').value;
  return {
    url: pick('sted-git-url'),
    branch: pick('sted-git-branch') || 'main',
    pin: pick('sted-git-pin'),
    path: pick('sted-git-path') || 'compose.yaml',
    credential_id: credential || null,
  };
}

/* A new Git stack POSTs this instead of compose_yaml: git: { url, branch,
   pin, path, credential_id }. The .env is still Vigil's own — only the compose
   file belongs to the repository. */
function _newGitStackBody(name, env) {
  return { host_id: stackEd.hostId, name, git: _gitFields(), env };
}

/* An existing Git stack PUTs nothing but its .env, unless the repository
   settings changed — then git: { … } takes effect by pulling it. */
function _gitSettingsBody() {
  return { git: _gitFields(), note: 'Git settings changed' };
}

function _applyStackSource() {
  const git = stackEd.source === 'git';
  const stack = stackEd.stack;
  document.querySelectorAll('[data-sted-source]').forEach(
    b => b.setAttribute('aria-checked', String(b.dataset.stedSource === stackEd.source)));
  _stedEl('sted-source').hidden = !!stack;      // an existing stack's source is fixed
  _stedEl('sted-git').hidden = !git;
  // A Git stack shows the repository's compose file so its folders can be
  // understood here; a new one has no file to show until the first fetch.
  const showEditor = !git || !!stack;
  const editor = _stedEl('stack-compose').closest('.sted-editor');
  editor.hidden = !showEditor;
  editor.previousElementSibling.hidden = !showEditor;
  _stedEl('sted-check').hidden = !showEditor;
  const ta = _stedEl('stack-compose');
  ta.readOnly = git;                            // the repository is the source of truth
  ta.classList.toggle('is-readonly', git);
  _renderGitState();
}

function _renderGitState() {
  const git = stackEd.stack && stackEd.stack.git;
  const state = _stedEl('sted-git-state');
  const pull = document.querySelector('.sted [data-sted-git-pull]');
  state.hidden = pull.hidden = !git;
  if (!git) return;
  // "Commit abc123def456 · tracking main" / "· pinned to v1.2". The commit is
  // the span's only <code>, so a repository string never becomes markup.
  state.querySelector('code').textContent = (git.commit || '').slice(0, 12);
  state.lastChild.textContent = git.pin ? ` · pinned to ${git.pin}` : ` · tracking ${git.branch || 'main'}`;
}

function _fillGitFields(git) {
  _stedEl('sted-git-url').value = git.url || '';
  _stedEl('sted-git-branch').value = git.branch || 'main';
  _stedEl('sted-git-pin').value = git.pin || '';
  _stedEl('sted-git-path').value = git.path || 'compose.yaml';
  stackEd.gitSnapshot = { url: git.url || '', branch: git.branch || 'main', pin: git.pin || '',
                          path: git.path || 'compose.yaml', credential_id: git.credential_id || '' };
}

function _gitSettingsChanged() {
  const was = stackEd.gitSnapshot, fresh = _gitFields();
  if (!was) return true;
  return ['url', 'branch', 'pin', 'path', 'credential_id'].some(k => (was[k] || '') !== (fresh[k] || ''));
}

function _setStackSource(source) {
  if (stackEd.stack || stackEd.source === source) return;   // fixed once the stack exists
  stackEd.source = source;
  _applyStackSource();
  _validateStack();
}

async function _loadGitCredentials() {
  if (stackEd.gitCreds === null) {
    stackEd.gitCreds = [];
    try {
      stackEd.gitCreds = (await apiJson('/api/v1/stacks/git-credentials/')).results || [];
    } catch (e) {
      _stackError(e.message);
    }
  }
  _renderGitCredentials();
}

function _renderGitCredentials() {
  const sel = _stedEl('sted-git-cred');
  const wanted = stackEd.gitSnapshot ? stackEd.gitSnapshot.credential_id : '';
  sel.innerHTML = '<option value="">None (public repo)</option>'
    + stackEd.gitCreds.map(c => `<option value="${escAttr(c.id)}">${escHtml(c.name)}</option>`).join('')
    + '<option value="__add__">Add a credential…</option>';
  // A credential that vanishes between the list and now (another tab deleted
  // it) leaves the field empty rather than throwing.
  sel.value = wanted && stackEd.gitCreds.some(c => c.id === wanted) ? wanted : '';
}

function _pickGitCredential() {
  const sel = _stedEl('sted-git-cred');
  if (sel.value !== '__add__') return;
  sel.value = stackEd.gitSnapshot ? stackEd.gitSnapshot.credential_id || '' : '';
  addGitCredential();
}

function addGitCredential() {
  const m = mountModal('git-cred', { variant: 'm-pop' });
  let kind = 'token';

  const close = () => {
    // The secret leaves the page on close, saved or not: it is only ever sent.
    const field = m.modal.querySelector('#git-cred-secret');
    if (field) field.value = '';      // the secret is only ever sent, never kept on screen
    m.close();
  };

  const save = async () => {
    const pick = (id) => { const el = m.modal.querySelector(id); return el ? el.value : ''; };
    const name = pick('#git-cred-name').trim();
    const username = pick('#git-cred-user').trim();
    const pass = pick('#git-cred-secret');
    const hosts = pick('#git-cred-hosts');
    if (!name) { showToast('Give the credential a name', 'warn'); return; }
    if (!pass.trim()) { showToast('The secret is empty', 'warn'); return; }
    if (kind === 'ssh_key' && !hosts.trim()) {
      showToast('An SSH deploy key needs the host key', 'warn');
      return;
    }
    const code = await totpPrompt('Adding a Git credential');
    if (!code) { close(); return; }
    try {
      const created = await apiJson('/api/v1/stacks/git-credentials/', {
        method: 'POST',
        body: JSON.stringify({ name, kind, username, secret: pass, known_hosts: hosts,
                               totp: code.trim() }),
      });
      stackEd.gitCreds.push(created);
      _renderGitCredentials();
      stackEd.gitSnapshot = { ...(stackEd.gitSnapshot || {}), credential_id: created.id };
      _pickCredentialOption(created.id);
      showToast(`Added ${created.name}`, 'success');
    } catch (e) {
      _stackError(e.message);
    }
    close();   // clears the secret whether the save landed or not
  };

  const render = () => {
    const ssh = kind === 'ssh_key';
    // The fields a kind needs, then the one that holds its key. Field ids in
    // markup strings; only the kind buttons interpolate.
    const extra = ssh ? `
      <div class="form-group">
        <label class="bl-label" for="git-cred-hosts">Known hosts</label>
        <textarea class="form-control" id="git-cred-hosts" rows="3" spellcheck="false"></textarea>
        <div class="bl-hint">The output of <code>ssh-keyscan github.com</code> — Vigil does not
          trust an unknown host key.</div>
      </div>` : `
      <div class="form-group">
        <label class="bl-label" for="git-cred-user">Username</label>
        <input class="form-control" id="git-cred-user" maxlength="255" placeholder="x-access-token"
          autocomplete="off" autocapitalize="off" spellcheck="false">
      </div>`;
    const holder = ssh
      ? '<textarea class="form-control" id="git-cred-secret" rows="6" spellcheck="false" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>'
      : '<input class="form-control" id="git-cred-secret" type="password" autocomplete="new-password">';
    const holderHint = ssh ? 'Paste the private key Vigil should authenticate with.'
                           : 'The token Vigil should authenticate with.';
    m.setBody(`
      <div class="modal-title">
        <span>A Git credential</span>
        <button class="modal-close" data-cred-x aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="sted-git-kinds" role="radiogroup" aria-label="Credential kind">
        <button class="btn btn-sm" type="button" data-cred-kind="token" aria-pressed="${!ssh}">HTTPS token</button>
        <button class="btn btn-sm" type="button" data-cred-kind="ssh_key" aria-pressed="${ssh}">SSH deploy key</button>
      </div>
      <div class="form-group">
        <label class="bl-label" for="git-cred-name">Name</label>
        <input class="form-control" id="git-cred-name" maxlength="120" placeholder="GitHub deploy token">
      </div>
      ${extra}
      <div class="form-group">
        <label class="bl-label" for="git-cred-secret">Secret</label>
        ${holder}
        <div class="bl-hint">${holderHint}</div>
      </div>
      <div class="confirm-actions">
        <button class="btn btn-outline btn-sm" data-cred-x>Cancel</button>
        <button class="btn btn-mint btn-sm" data-cred-save>Save</button>
      </div>`);
    m.overlay.onclick = close;
    m.modal.querySelectorAll('[data-cred-x]').forEach(b => { b.onclick = close; });
    m.modal.querySelectorAll('[data-cred-kind]').forEach(b => {
      b.onclick = () => { if (kind === b.dataset.credKind) return; kind = b.dataset.credKind; render(); };
    });
    m.modal.querySelector('[data-cred-save]').onclick = save;
  };

  render();
  requestAnimationFrame(() => { m.open(); m.modal.querySelector('#git-cred-name').focus(); });
}

async function pullStackFromGit() {
  const stack = stackEd.stack;
  if (!stack) return;
  try {
    const res = await apiJson(`/api/v1/stacks/${stack.id}/pull/`, { method: 'POST', body: '{}' });
    if (!res.changed) {
      showToast('Already at the latest commit', 'info');
      return;
    }
    const commit = ((res.stack.git || {}).commit || '').slice(0, 12);
    await renderManagedStacks(stackEd.hostId);   // the chip's r-number changed too
    showToast(`New commit ${commit} is revision ${res.stack.revision} — deploy it to apply`, 'info');
    openStackEditor(res.stack.id);
  } catch (e) {
    _stackError(e.message);
  }
}

function _gitFetchedToast(saved) {
  const git = saved.git || {};
  return `Fetched ${(git.commit || '').slice(0, 12)} from ${git.pin || git.branch || 'main'} — deploy it to run`;
}

async function saveStack() {
  const editing = stackEd.stack;
  const gitStack = !!(editing && editing.git);
  const gitChanged = gitStack && _gitSettingsChanged();
  const env = stackEd.env.filter(e => e.key.trim()).map(e => (e.stored && !e.value && !e.revealed)
    ? { key: e.key.trim(), keep: true } : { key: e.key.trim(), value: e.value || '' });
  let body;
  if (gitStack && gitChanged) body = _gitSettingsBody();
  else if (gitStack) body = { env, note: 'edited' };   // the compose file comes from Git, not from here
  else if (stackEd.source === 'git') body = _newGitStackBody(_stedEl('stack-name').value.trim(), env);
  else body = { compose_yaml: _stedEl('stack-compose').value, env, note: 'edited' };
  const wasNew = !editing;
  try {
    const saved = await apiJson(editing ? `/api/v1/stacks/${editing.id}/` : '/api/v1/stacks/',
      { method: editing ? 'PUT' : 'POST', body: JSON.stringify(body) });
    await renderManagedStacks(stackEd.hostId);
    if (wasNew && stackEd.source === 'git') showToast(_gitFetchedToast(saved), 'success');
    else showToast(gitChanged ? 'Repository settings updated — pulled what it holds now'
                              : `Saved ${saved.name} as revision ${saved.revision} — deploy it to apply`, 'success');
    // Reopen the saved row — but only if the editor is still on screen. An
    // async Ctrl+S can land after the operator has moved on, and dragging
    // them back to the page they left would be worse than a stale view.
    if (document.getElementById('page-stack-editor')?.classList.contains('active')) openStackEditor(saved.id);
  } catch (e) { _stackError(e.message); }
}

async function _stackTotpAction(path, payload) {
  const code = await totpPrompt('This change runs on the host.');
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
  const stack = stackEd.stack;
  const sure = await confirmModal(`Take down ${stack.name}?`,
    { title: 'Take down this stack?', danger: true, confirmText: 'Take down' });
  if (!sure) return;
  // An adopted stack's folder belongs to someone else — matching the server,
  // there is nothing to delete, so the editor never offers to.
  let wipe = false;
  if (!stack.adopted) {
    // Cancel means keep the files; the server refuses to delete an adopted
    // stack's folder anyway, so adopted stacks are never asked.
    wipe = await confirmModal(`Also delete ${stack.name}'s folder on ${stack.hostname}?`,
      { title: 'Delete its files too?', danger: true, confirmText: 'Delete the folder' });
  }
  const body = await _stackTotpAction(`/api/v1/stacks/${stack.id}/remove/`, { delete_files: wipe });
  if (!body) return;
  showToast(`Taking down ${stack.name}`, 'success');
  stackEd.env = [];
  _clearStackDirty();
  navigateTo('containers');
  if (stackEd.hostId) renderManagedStacks(stackEd.hostId);
}

/* ── Wiring ──────────────────────────────────────────────────────────── */
delegateClick('[data-stack-new]', () => openStackEditor(null));
delegateClick('[data-stack-edit]', (el) => openStackEditor(el.dataset.stackEdit));
delegateClick('[data-sted-back]', () => closeStackEditor());
delegateClick('[data-stack-save]', () => saveStack());
delegateClick('[data-stack-deploy]', () => deployStack());
delegateClick('[data-stack-remove]', () => removeStack());
delegateClick('[data-sted-source]', (el) => _setStackSource(el.dataset.stedSource));
delegateClick('[data-sted-git-pull]', () => pullStackFromGit());
delegateClick('[data-stack-env-reveal]', () => revealStackEnv());
delegateClick('[data-stack-env-add]', () => {
  stackEd.env.push({ key: '', value: '', stored: false });
  _renderStackEnv();
  _markStackDirty();
  _scheduleStackCheck();
});
delegateClick('[data-env-remove]', (el) => {
  stackEd.env.splice(parseInt(el.dataset.envRemove, 10), 1);
  _renderStackEnv();
  _markStackDirty();
  _scheduleStackCheck();
});
delegateClick('[data-sted-add-env]', (el) => {
  stackEd.env.push({ key: el.dataset.stedAddEnv, value: '', stored: false });
  _renderStackEnv();
  _markStackDirty();
  _scheduleStackCheck();
});
delegateClick('[data-stack-load-rev]', (el) => {
  const rev = (stackEd.revisions || []).find(r => r.number === parseInt(el.dataset.stackLoadRev, 10));
  if (!rev) return;
  _stedEl('stack-compose').value = rev.compose_yaml;
  _syncStackEditor();
  _markStackDirty();
  _scheduleStackCheck();
  showToast(`Loaded r${rev.number}'s compose file — Save makes it the next revision`, 'success');
});
document.addEventListener('input', (ev) => {
  const row = ev.target.closest && ev.target.closest('[data-env-row]');
  if (row) {
    const entry = stackEd.env[parseInt(row.dataset.envRow, 10)];
    if (!entry) return;
    entry[ev.target.dataset.envField] = ev.target.value;
    _markStackDirty();
    if (ev.target.dataset.envField === 'key') _scheduleStackCheck();
    return;
  }
  if (ev.target.id === 'stack-compose') {
    _syncStackEditor();
    _markStackDirty();
    _scheduleStackCheck();
  } else if (ev.target.id === 'stack-name') {
    _markStackDirty();
  }
});
document.addEventListener('scroll', (ev) => {
  if (ev.target && ev.target.id === 'stack-compose') _mirrorStackScroll();
}, true);
document.addEventListener('keydown', (ev) => {
  const ta = _stedEl('stack-compose');
  const inEditor = ev.target === ta;
  if ((ev.metaKey || ev.ctrlKey) && inEditor && ev.key.toLowerCase() === 's') {
    ev.preventDefault();
    saveStack();
    return;
  }
  if (inEditor && ev.key === 'Tab') {
    // Two-space soft tab, and Shift+Tab takes up to two back — the editor is
    // the page's only place to type YAML, so the Tab key must not leave it.
    ev.preventDefault();
    const start = ta.selectionStart, end = ta.selectionEnd;
    if (ev.shiftKey) {
      const lineStart = ta.value.lastIndexOf('\n', start - 1) + 1;
      let cut = 0;
      while (cut < 2 && ta.value[lineStart + cut] === ' ') cut += 1;
      if (cut) {
        ta.value = ta.value.slice(0, lineStart) + ta.value.slice(lineStart + cut);
        ta.selectionStart = ta.selectionEnd = Math.max(lineStart, start - cut);
      }
    } else {
      ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = start + 2;
    }
    _syncStackEditor();
    _markStackDirty();
    _scheduleStackCheck();
    return;
  }
  if (ev.key === 'Escape' && document.getElementById('page-stack-editor')?.classList.contains('active')) {
    // vigil-modal.js stops the event while a dialog is open, so Escape
    // reaching here means the editor is the topmost thing on screen.
    closeStackEditor();
  }
});

/* A <select> answers to 'change', not a click — the dispatcher's idiom is
   built for buttons, so this one wires itself. Typing in a focused <select>
   can select an option too, which is why the handler stops at the sentinel. */
(function () {
  const wire = () => {
    const sel = _stedEl('sted-git-cred');
    if (sel) sel.addEventListener('change', () => _pickGitCredential());
  };
  document.addEventListener('DOMContentLoaded', wire);
  if (document.readyState !== 'loading') wire();
})();

/* ── navigateTo wrapper: leaving the editor with unsaved edits asks first ─ */
(function () {
  const previous = window.navigateTo;
  if (typeof previous !== 'function') return;
  let navigating = false;
  window.navigateTo = function (pageName) {
    if (!navigating && pageName !== 'stack-editor'
        && document.getElementById('page-stack-editor')?.classList.contains('active')
        && stackEd.dirty) {
      navigating = true;
      confirmModal('Discard unsaved changes to this stack?', { danger: true, confirmText: 'Discard' })
        .then((go) => {
          navigating = false;
          if (!go) return;
          _clearStackDirty();
          stackEd.env = [];
          previous.apply(window, arguments);
        });
      return;
    }
    previous.apply(this, arguments);
  };
})();
