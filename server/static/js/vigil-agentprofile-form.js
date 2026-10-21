// vigil-agentprofile-form.js
// Owns: the agent settings form the Add Host wizard and the Agent profiles tab
//   share — mode, allowed actions, relays, live sessions, tags, intervals.
// HTML: mounted into any container by apfRender(); markup is generated here.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr)
// API: GET /api/v1/agent-profiles/actions/  GET /api/v1/relays/
//
// One field set, two hosts for it: a wizard that turns the values into an
// install command, and a profile editor that stores them. Both read the form
// with apfRead() and show server complaints with apfShowErrors(), so a field
// label or a name here is exactly the API's name in apps/agentprofiles.

const APF_MODES = ['monitor', 'managed', 'full_control'];

const _APF_MODE_HINTS = {
  monitor: 'Read-only metrics. Runs unprivileged and executes no tasks.',
  managed: 'Runs allowlisted tasks. The agent runs as root / SYSTEM.',
  full_control: 'Runs any signed task, including package installs. The agent runs as root / SYSTEM.',
};

const _APF_MODE_LABELS = { monitor: 'Monitor', managed: 'Managed', full_control: 'Full control' };

const APF_DEFAULTS = {
  mode: 'monitor',
  allowlist: [],
  primary_relay: '',
  fallback_relays: [],
  include_dashboard_fallback: true,
  allow_remote_shell: false,
  allow_remote_desktop: false,
  remote_shell_user: 'root',
  remote_desktop_port: 3389,
  allow_reprovision: false,
  tags: [],
  process_watch: [],
  checkin_interval: 60,
  docker_check_interval: 21600,
  software_interval: 21600,
};

/* ── context ───────────────────────────────────────────────────────────── */

let _apfCtx = null;

async function apfLoadContext() {
  if (_apfCtx) return _apfCtx;
  const [actions, relayBody] = await Promise.all([
    apiJson('/api/v1/agent-profiles/actions/'),
    apiJson('/api/v1/relays/'),
  ]);
  const relays = (Array.isArray(relayBody) ? relayBody : (relayBody.relays || []))
    .filter(r => r && r.kind === 'REMOTE' && r.enabled);
  _apfCtx = { actions: actions || [], relays };
  return _apfCtx;
}

/* ── values ────────────────────────────────────────────────────────────── */

function _apfList(value) {
  if (Array.isArray(value)) return value.map(v => String(v));
  return String(value ?? '').split(',').map(s => s.trim()).filter(Boolean);
}

function _apfValues(values) {
  const v = values || {};
  const pick = (key) => (v[key] === undefined || v[key] === null ? APF_DEFAULTS[key] : v[key]);
  return {
    mode: APF_MODES.includes(v.mode) ? v.mode : APF_DEFAULTS.mode,
    allowlist: _apfList(pick('allowlist')),
    primary_relay: String(pick('primary_relay') || ''),
    fallback_relays: _apfList(pick('fallback_relays')),
    include_dashboard_fallback: pick('include_dashboard_fallback') !== false,
    allow_remote_shell: !!pick('allow_remote_shell'),
    allow_remote_desktop: !!pick('allow_remote_desktop'),
    remote_shell_user: String(pick('remote_shell_user') || 'root'),
    remote_desktop_port: Number(pick('remote_desktop_port')) || 3389,
    allow_reprovision: !!pick('allow_reprovision'),
    tags: _apfList(pick('tags')),
    process_watch: String(pick('process_watch') || ''),
    checkin_interval: Number(pick('checkin_interval')) || 60,
    docker_check_interval: Number(pick('docker_check_interval')) || 21600,
    software_interval: Number(pick('software_interval')) || 21600,
  };
}

function _apfRelayName(ctx, id) {
  const relay = ctx.relays.find(r => String(r.id) === String(id));
  return relay ? relay.name : `relay ${String(id).slice(0, 8)}`;
}

/* ── render ────────────────────────────────────────────────────────────── */

function apfRender(container, values, ctx) {
  if (!container || !ctx) return;
  const v = _apfValues(values);
  container._apfCtx = ctx;
  container.classList.add('apf');

  const modeTabs = APF_MODES.map(m => `
    <button type="button" class="tab t-sky${m === v.mode ? ' active' : ''}"
            data-act="apfSetMode" data-a1="$el" data-a2="${m}">${_APF_MODE_LABELS[m]}</button>`).join('');

  const relayOpts = [{ id: '', name: 'This dashboard' }, ...ctx.relays]
    .map(r => `<option value="${escAttr(r.id)}"${r.id === v.primary_relay ? ' selected' : ''}>${escHtml(r.name)}</option>`)
    .join('');

  container.innerHTML = `
  <div class="apf-section">
    <div class="deploy-input-label">Agent mode</div>
    <div class="tab-bar apf-mode-tabs" style="margin:6px 0;">${modeTabs}</div>
    <div class="confirm-hint">${escHtml(_APF_MODE_HINTS[v.mode])}</div>
  </div>

<div class="apf-section"${v.mode === 'managed' ? '' : ' hidden'}>
  <div class="deploy-input-label">Allowed actions</div>
  <p class="confirm-hint" style="margin:4px 0 8px;">Only used in managed mode. Nothing here lets a task run
    arbitrary commands — <span class="mono">run_command</span> can never be allowlisted.</p>
  <div class="apf-action-tools">
    <input type="search" class="form-control apf-action-search" placeholder="Search actions…" aria-label="Search actions">
    <button type="button" class="btn btn-outline btn-sm" data-act="apfSelectReadOnly" data-a1="$el">Select read-only</button>
  </div>
  <div class="apf-actions">${_apfActionGroups(v.allowlist, ctx)}</div>
</div>

<div class="apf-section">
  <div class="deploy-input-label">Connection</div>
  <div class="apf-grid">
    <label class="apf-field"><span class="apf-field-label">Checks in with</span>
      <select class="form-control apf-primary-relay">${relayOpts}</select></label>
    <label class="apf-field"><span class="apf-field-label">Add fallback</span>
      <select class="form-control apf-add-fallback">${_apfFallbackOptions(v, ctx)}</select></label>
  </div>
  <div class="apf-field" style="margin-top:10px;"><span class="apf-field-label">Fallback relays, in order</span>
    <div class="apf-fallbacks">${_apfFallbackRows(v, ctx)}</div></div>
  <label class="apf-check" style="margin-top:8px;">
    <input type="checkbox" class="apf-dashboard-fallback"${v.include_dashboard_fallback ? ' checked' : ''}>
    <span>Fall back to this dashboard last</span></label>
</div>

<div class="apf-section">
  <div class="deploy-input-label">Live sessions</div>
  <div class="confirm-hint apf-sessions-note" style="margin:4px 0 8px;">${
    v.mode === 'monitor' ? 'Monitor mode never opens sessions.'
      : 'Off unless enabled here; every session still needs a two-factor code.'}</div>
  <label class="apf-check"><input type="checkbox" class="apf-shell"${v.allow_remote_shell ? ' checked' : ''}${v.mode === 'monitor' ? ' disabled' : ''}>
    <span>Allow web shell and container shells</span></label>
  <label class="apf-field apf-narrow"><span class="apf-field-label">Shell runs as</span>
    <input type="text" class="form-control apf-shell-user" maxlength="32" value="${escAttr(v.remote_shell_user)}"${v.mode === 'monitor' ? ' disabled' : ''}></label>
  <label class="apf-check"><input type="checkbox" class="apf-rdp"${v.allow_remote_desktop ? ' checked' : ''}${v.mode === 'monitor' ? ' disabled' : ''}>
    <span>Allow Web RDP</span></label>
  <label class="apf-field apf-narrow"><span class="apf-field-label">RDP port</span>
    <input type="number" class="form-control apf-rdp-port" min="1" max="65535" value="${escAttr(String(v.remote_desktop_port))}"${v.mode === 'monitor' ? ' disabled' : ''}></label>
</div>

<div class="apf-section">
  <div class="deploy-input-label">Tags</div>
  <input type="text" class="form-control apf-tags" placeholder="web, prod, rack-3" value="${escAttr(v.tags.join(', '))}">
  <div class="sa-chips apf-tag-chips">${_apfTagChips(v.tags)}</div>
</div>

<details class="apf-section apf-advanced">
  <summary class="deploy-input-label">Advanced</summary>
  <div class="apf-grid" style="margin-top:10px;">
    <label class="apf-field"><span class="apf-field-label">Check-in interval (s)</span>
      <input type="number" class="form-control apf-checkin" min="10" max="86400" value="${escAttr(String(v.checkin_interval))}"></label>
    <label class="apf-field"><span class="apf-field-label">Container update check (s)</span>
      <input type="number" class="form-control apf-docker-interval" min="60" max="2592000" value="${escAttr(String(v.docker_check_interval))}"></label>
    <label class="apf-field"><span class="apf-field-label">Software inventory (s)</span>
      <input type="number" class="form-control apf-software-interval" min="60" max="2592000" value="${escAttr(String(v.software_interval))}"></label>
  </div>
  <label class="apf-field" style="margin-top:10px;"><span class="apf-field-label">Watched processes</span>
    <input type="text" class="form-control apf-process-watch" placeholder="nginx, postgres" value="${escAttr(v.process_watch)}"></label>
  <label class="apf-check" style="margin-top:8px;"><input type="checkbox" class="apf-reprovision"${v.allow_reprovision ? ' checked' : ''}${v.mode === 'monitor' ? ' disabled' : ''}>
    <span>Allow remote rebuild of this machine</span></label>
</details>`;

  _apfBind(container);
}

function _apfActionGroups(allowlist, ctx) {
  const groups = new Map();
  for (const action of ctx.actions) {
    if (!action || !action.allowlistable) continue;
    const group = action.group || 'Other';
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(action);
  }
  if (!groups.size) return '<p class="confirm-hint">No actions are available to allowlist.</p>';
  const chosen = new Set(allowlist);
  let html = '';
  for (const [group, items] of groups) {
    const rows = items.map(a => `
      <label class="apf-action" data-name="${escAttr(a.name.toLowerCase())} ${escAttr((a.label || '').toLowerCase())}">
        <input type="checkbox" class="apf-action-check" value="${escAttr(a.name)}"${chosen.has(a.name) ? ' checked' : ''}>
        <span>${escHtml(a.label || a.name)}</span>
        <span class="mono apf-action-name">${escHtml(a.name)}</span>
        <span class="risk-badge risk-${escAttr(a.risk || 'standard')}">${escHtml(a.risk || 'standard')}</span>
      </label>`).join('');
    html += `<details open><summary>${escHtml(group)} <span class="muted">(${items.length})</span></summary>${rows}</details>`;
  }
  return html;
}

function _apfFallbackOptions(v, ctx) {
  const taken = new Set([v.primary_relay, ...v.fallback_relays]);
  const free = ctx.relays.filter(r => !taken.has(String(r.id)));
  if (!free.length) return '<option value="">No relays left</option>';
  return '<option value="">Choose a relay…</option>' + free.map(r =>
    `<option value="${escAttr(String(r.id))}">${escHtml(r.name)}</option>`).join('');
}

function _apfFallbackRows(v, ctx) {
  if (!v.fallback_relays.length) {
    return '<span class="confirm-hint">None — the agent retries its primary.</span>';
  }
  return v.fallback_relays.map((id, i) => `
    <div class="apf-fallback-row" data-id="${escAttr(id)}">
      <span class="apf-fallback-name">${escHtml(_apfRelayName(ctx, id))}</span>
      <button type="button" class="btn btn-outline btn-sm" data-act="apfMoveFallback" data-a1="$el" data-a2="up"
              ${i === 0 ? 'disabled' : ''} aria-label="Move up">&#8593;</button>
      <button type="button" class="btn btn-outline btn-sm" data-act="apfMoveFallback" data-a1="$el" data-a2="down"
              ${i === v.fallback_relays.length - 1 ? 'disabled' : ''} aria-label="Move down">&#8595;</button>
      <button type="button" class="btn btn-outline btn-sm" data-act="apfRemoveFallback" data-a1="$el">Remove</button>
    </div>`).join('');
}

function _apfTagChips(tags) {
  if (!tags.length) return '';
  return tags.map(t => `<span class="chip chip-muted">${escHtml(t)}</span>`).join('');
}

/* ── wiring ────────────────────────────────────────────────────────────── */

function _apfBind(container) {
  const search = container.querySelector('.apf-action-search');
  if (search) {
    search.addEventListener('input', () => {
      const term = search.value.trim().toLowerCase();
      for (const details of container.querySelectorAll('.apf-actions details')) {
        let any = false;
        for (const row of details.querySelectorAll('.apf-action')) {
          const hit = !term || (row.dataset.name || '').includes(term);
          row.style.display = hit ? '' : 'none';
          if (hit) any = true;
        }
        details.style.display = any ? '' : 'none';
        if (term) details.open = true;
      }
    });
  }

  // The fallback list and its picker are state the form holds in the DOM: the
  // checkboxes above it cannot carry a relay id, so the rows are the storage.
  const fallbacks = container.querySelector('.apf-fallbacks');
  if (fallbacks) {
    new MutationObserver(() => _apfRefreshFallbackPicker(container))
      .observe(fallbacks, { childList: true });
  }

  const tags = container.querySelector('.apf-tags');
  if (tags) {
    tags.addEventListener('input', () => {
      const chips = container.querySelector('.apf-tag-chips');
      if (chips) chips.innerHTML = _apfTagChips(_apfList(tags.value));
    });
  }

  for (const sel of ['apf-primary-relay', 'apf-add-fallback']) {
    const el = container.querySelector('.' + sel);
    if (el) el.addEventListener('change', () => _apfFallbackChange(container, sel));
  }
}

function _apfRefreshFallbackPicker(container) {
  const addSel = container.querySelector('.apf-add-fallback');
  if (!addSel || !container._apfCtx) return;
  // The "…" placeholder is what the picker shows when ready, so reading the
  // form's current ids has to skip it — otherwise it counts as a chosen relay.
  const taken = [...container.querySelectorAll('.apf-fallback-row')].map(r => r.dataset.id);
  const primary = container.querySelector('.apf-primary-relay');
  addSel.innerHTML = _apfFallbackOptions(
    { primary_relay: primary ? primary.value : '', fallback_relays: taken },
    container._apfCtx);
}

function _apfFallbackChange(container, which) {
  const listEl = container.querySelector('.apf-fallbacks');
  const addSel = container.querySelector('.apf-add-fallback');
  if (!listEl || !addSel) return;
  const picked = addSel.value;
  let ids = [...listEl.querySelectorAll('.apf-fallback-row')].map(r => r.dataset.id);
  const primary = container.querySelector('.apf-primary-relay');

  if (which === 'apf-primary-relay') {
    // A relay cannot also be its own fallback.
    ids = ids.filter(id => id !== (primary ? primary.value : ''));
  } else if (picked && !ids.includes(picked)) {
    ids.push(picked);
  }

  listEl.innerHTML = _apfFallbackRows(
    { primary_relay: primary ? primary.value : '', fallback_relays: ids }, container._apfCtx);
  addSel.value = '';
  _apfDispatchChange(container);
}

function apfSetMode(btn, mode) {
  const container = btn && btn.closest('.apf');
  if (!container || !APF_MODES.includes(mode)) return;
  for (const tab of container.querySelectorAll('[data-act="apfSetMode"]')) {
    tab.classList.toggle('active', tab.dataset.a2 === mode);
  }
  const hint = container.querySelector('.apf-mode-tabs + .confirm-hint');
  if (hint) hint.textContent = _APF_MODE_HINTS[mode];
  const actions = container.querySelectorAll('.apf-section')[1];
  if (actions) actions.hidden = mode !== 'managed';

  // Monitor never opens a session and is never rebuilt remotely: disabled, so
  // the command cannot promise a shell the agent would refuse.
  for (const sel of ['.apf-shell', '.apf-shell-user', '.apf-rdp', '.apf-rdp-port', '.apf-reprovision']) {
    for (const el of container.querySelectorAll(sel)) el.disabled = mode === 'monitor';
  }
  const note = container.querySelector('.apf-sessions-note');
  if (note) note.textContent = mode === 'monitor'
    ? 'Monitor mode never opens sessions.'
    : 'Off unless enabled here; every session still needs a two-factor code.';
  _apfDispatchChange(container);
}

function apfSelectReadOnly(btn) {
  const container = btn && btn.closest('.apf');
  const ctx = container && container._apfCtx;
  if (!ctx) return;
  const low = new Set(ctx.actions.filter(a => a.allowlistable && a.risk === 'low').map(a => a.name));
  for (const box of container.querySelectorAll('.apf-action-check')) {
    box.checked = low.has(box.value);
  }
  _apfDispatchChange(container);
}

function apfMoveFallback(btn, direction) {
  const row = btn.closest('.apf-fallback-row');
  const listEl = row && row.parentElement;
  if (!listEl) return;
  const ids = [...listEl.querySelectorAll('.apf-fallback-row')].map(r => r.dataset.id);
  const i = ids.indexOf(row.dataset.id);
  const j = direction === 'up' ? i - 1 : i + 1;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  _apfWriteFallbacks(listEl, ids);
}

function apfRemoveFallback(btn) {
  const row = btn.closest('.apf-fallback-row');
  const listEl = row && row.parentElement;
  if (!listEl) return;
  const ids = [...listEl.querySelectorAll('.apf-fallback-row')].map(r => r.dataset.id);
  _apfWriteFallbacks(listEl, ids.filter(id => id !== row.dataset.id));
}

function _apfWriteFallbacks(listEl, ids) {
  const container = listEl.closest('.apf');
  const primary = container && container.querySelector('.apf-primary-relay');
  // Rebuilt from data rather than moved in the DOM: the ↑/↓ disabled state and
  // the "Add fallback" choices both depend on position, and rewriting the rows
  // keeps those in step with one code path.
  listEl.innerHTML = _apfFallbackRows(
    { primary_relay: primary ? primary.value : '', fallback_relays: ids },
    container && container._apfCtx);
  if (container) _apfDispatchChange(container);
}

/* A form that has already produced an install command must say so when its
   settings move; each module decides what to do about it. */
function _apfDispatchChange(container) {
  container.dispatchEvent(new CustomEvent('apfchange', { bubbles: true }));
}

/* ── read / errors ─────────────────────────────────────────────────────── */

function apfRead(container) {
  const get = (sel) => container.querySelector(sel);
  const checked = (sel) => { const el = get(sel); return !!(el && el.checked); };
  const text = (sel, fallback) => { const el = get(sel); return el ? el.value.trim() : fallback; };
  const num = (sel, fallback) => {
    const el = get(sel);
    const n = el ? Number(el.value) : NaN;
    return Number.isFinite(n) && n > 0 ? Math.round(n) : fallback;
  };

  const primary = get('.apf-primary-relay');
  const listEl = get('.apf-fallbacks');
  return {
    mode: (container.querySelector('[data-act="apfSetMode"].active') || {}).dataset?.a2 || 'monitor',
    allowlist: [...container.querySelectorAll('.apf-action-check:checked')].map(el => el.value),
    primary_relay: primary ? primary.value : '',
    fallback_relays: listEl ? [...listEl.querySelectorAll('.apf-fallback-row')].map(r => r.dataset.id) : [],
    include_dashboard_fallback: checked('.apf-dashboard-fallback'),
    allow_remote_shell: checked('.apf-shell'),
    allow_remote_desktop: checked('.apf-rdp'),
    remote_shell_user: text('.apf-shell-user', 'root'),
    remote_desktop_port: num('.apf-rdp-port', 3389),
    allow_reprovision: checked('.apf-reprovision'),
    tags: _apfList(text('.apf-tags', '')),
    process_watch: text('.apf-process-watch', ''),
    checkin_interval: num('.apf-checkin', 60),
    docker_check_interval: num('.apf-docker-interval', 21600),
    software_interval: num('.apf-software-interval', 21600),
  };
}

const _APF_FIELDS = [
  ['mode', '.apf'],
  ['allowlist', '.apf-actions'],
  ['primary_relay', '.apf-primary-relay'],
  ['fallback_relays', '.apf-fallbacks'],
  ['include_dashboard_fallback', '.apf-dashboard-fallback'],
  ['allow_remote_shell', '.apf-shell'],
  ['remote_desktop_port', '.apf-rdp-port'],
  ['remote_shell_user', '.apf-shell-user'],
  ['allow_remote_desktop', '.apf-rdp'],
  ['tags', '.apf-tags'],
  ['process_watch', '.apf-process-watch'],
  ['checkin_interval', '.apf-checkin'],
  ['docker_check_interval', '.apf-docker-interval'],
  ['software_interval', '.apf-software-interval'],
  ['allow_reprovision', '.apf-reprovision'],
];

/* ── field errors ────────────────────────────────────────────────────────
   QA-62 refuses a bad setting with {errors: {field: message}}; apiJson hands
   that body over as err.body, and the callers pass its errors here. */

function apfShowErrors(container, errors) {
  for (const host of container.querySelectorAll('.apf-error')) host.remove();
  if (!errors || typeof errors !== 'object' || Array.isArray(errors)) return;
  for (const [field, message] of Object.entries(errors)) {
    const entry = _APF_FIELDS.find(([name]) => name === field);
    if (!entry) continue;
    const anchor = container.querySelector(entry[1]);
    if (!anchor) continue;
    const note = document.createElement('div');
    note.className = 'apf-error';
    note.textContent = String(message);
    anchor.insertAdjacentElement('afterend', note);
  }
}
