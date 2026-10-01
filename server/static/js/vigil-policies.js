// vigil-policies.js
// Owns: the Policies page — app and patch policies, the editor (Apps /
//   Patching / Preview), run now, and the changes waiting for approval.
// HTML: templates/pages/_policies.html
// Depends on: vigil-utils.js (escHtml, escAttr, apiJson, showToast,
//   delegateClick), vigil-nav.js (navigateTo — wrapped below),
//   vigil-tasks.js (openDefinitionEditor, for "View the generated task").
// API: /api/v1/policies/ (CRUD), /<id>/drift/, /<id>/run/,
//      /changes/?state=pending, /changes/approve/, /changes/reject/,
//      /compliance/; /api/v1/compliance/report{/,.html} (Business);
//      GET /api/v1/software/apps/ (the app picker)

const POL_CLASSIFICATIONS = [
  'Critical Updates', 'Security Updates', 'Definition Updates', 'Update Rollups',
  'Updates', 'Feature Packs', 'Service Packs', 'Drivers', 'Tools', 'Upgrades',
];
const POL_DEFAULT_CLASSES = ['Critical Updates', 'Security Updates', 'Definition Updates'];
const POL_SOURCES = ['', 'dpkg', 'rpm', 'apk', 'pacman', 'flatpak', 'snap',
  'winget', 'chocolatey', 'scoop', 'registry'];
const POL_STATES = [['present', 'Present'], ['latest', 'Latest'], ['pinned', 'Pinned'], ['absent', 'Absent']];
const POL_COLUMNS = 6;

const polState = { rows: [], changes: [], selected: new Set(), editing: null, rules: [], apps: null };

/* ── List ────────────────────────────────────────────────────────────── */
function _polWindow(p) {
  const pad = (v) => (/^\d+$/.test(v) ? String(v).padStart(2, '0') : v);
  const days = p.cron_dow === '*' ? 'daily' : `days ${p.cron_dow}`;
  return `${pad(p.cron_hour)}:${pad(p.cron_minute)} ${days}, ${p.window_hours}h`;
}

async function fetchPolicies() {
  const tbody = document.getElementById('pol-table-body');
  if (tbody) tbody.innerHTML = `<tr><td colspan="${POL_COLUMNS}" class="apps-detail-loading">Loading…</td></tr>`;
  try {
    polState.rows = (await apiJson('/api/v1/policies/')).results || [];
  } catch { polState.rows = []; }
  renderPolicies();
}

function renderPolicies() {
  const tbody = document.getElementById('pol-table-body');
  if (!tbody) return;
  const pendingBy = {};
  for (const c of polState.changes) pendingBy[c.policy_id] = (pendingBy[c.policy_id] || 0) + 1;
  const html = polState.rows.map(p => {
    const tags = p.target_tags.length
      ? p.target_tags.map(t => `<span class="chip chip-muted apps-src">${escHtml(t)}</span>`).join('')
      : '<span class="apps-zero">every managed host</span>';
    const patching = p.patch_enabled
      ? `<span class="chip">on</span>${p.reboot !== 'never' ? ` <span class="apps-zero">restart: ${escHtml(p.reboot.replace('_', ' '))}</span>` : ''}`
      : '<span class="apps-zero">off</span>';
    const pending = pendingBy[p.id] ? `<span class="chip apps-chip-warn">${pendingBy[p.id]} waiting</span>` : '';
    const mode = p.approval_mode === 'approve' ? '<span class="apps-zero">approve each</span>' : '<span class="apps-zero">automatic</span>';
    return `<tr class="apps-row" data-pol-row="${escAttr(p.id)}">
      <td><span class="apps-name">${escHtml(p.name)}</span>${p.enabled ? '' : ' <span class="chip chip-muted">disabled</span>'}</td>
      <td>${tags}</td>
      <td class="apps-mono">${escHtml(_polWindow(p))}</td>
      <td class="num">${p.app_rules.length}</td>
      <td>${patching}</td>
      <td>${pending || mode}</td>
    </tr>`;
  }).join('');
  tbody.innerHTML = html || `<tr><td colspan="${POL_COLUMNS}" class="apps-empty-state">No policies yet. A policy keeps a set of apps present, current, pinned or absent — and can install OS updates — on the hosts it names, in a maintenance window.</td></tr>`;
}

/* ── Compliance (numbers Free; the report is Business) ───────────────── */
async function fetchCompliance() {
  const wrap = document.getElementById('pol-compliance');
  if (!wrap) return;
  let c;
  try { c = await apiJson('/api/v1/policies/compliance/'); } catch { wrap.innerHTML = ''; return; }
  const late = (c.missing_by_age.critical['31-90'] || 0) + (c.missing_by_age.critical['90+'] || 0);
  wrap.innerHTML = `<span class="chip ${c.patched_pct >= 95 ? 'chip-mint' : 'apps-chip-warn'}">${c.patched_pct}% of hosts patched within SLA</span>
    <span class="chip chip-muted">${c.hosts_patched} of ${c.hosts_total} hosts with nothing overdue</span>
    ${late ? `<span class="chip chip-rose">${late} critical update${late === 1 ? '' : 's'} missing over 30 days</span>` : ''}`;
}

async function openComplianceReport() {
  const resp = await fetch('/api/v1/compliance/report/', { credentials: 'same-origin' });
  if (resp.status === 402) {
    showToast('The per-site compliance report, its export and branding need Vigil Business.', 'error');
    return;
  }
  window.open('/api/v1/compliance/report.html', '_blank', 'noopener');
}

/* ── Waiting for approval ────────────────────────────────────────────── */
function _changeText(c) {
  if (c.update) return `${c.update}${c.title && c.title !== c.update ? ` — ${c.title}` : ''}`;
  const want = c.want ? ` → ${c.want}` : '';
  return `${c.action} ${c.app}${c.have ? ` (${c.have}${want})` : want}`;
}

async function fetchPolicyChanges() {
  try {
    polState.changes = (await apiJson('/api/v1/policies/changes/?state=pending')).results || [];
  } catch { polState.changes = []; }
  const ids = new Set(polState.changes.map(c => c.id));
  for (const id of [...polState.selected]) if (!ids.has(id)) polState.selected.delete(id);
  renderPolicyChanges();
  renderPolicies();
}

function renderPolicyChanges() {
  const wrap = document.getElementById('pol-changes-list');
  if (!wrap) return;
  const actions = document.getElementById('pol-changes-actions');
  if (actions) actions.hidden = polState.selected.size === 0;
  if (!polState.changes.length) {
    wrap.innerHTML = '<div class="apps-empty-state">Nothing waiting. Policies set to approve each change list what they would do here.</div>';
    return;
  }
  wrap.innerHTML = polState.changes.map(c => `<label class="pol-change">
      <input type="checkbox" data-pol-change="${escAttr(c.id)}"${polState.selected.has(c.id) ? ' checked' : ''}>
      <span class="pol-change-head"><span class="apps-name">${escHtml(c.hostname)}</span>
        <span class="apps-zero">${escHtml(c.policy)} · ${escHtml(timeAgo(c.created_at) || '')}</span></span>
      <span class="pol-change-list">${c.changes.map(x => `<span class="chip chip-muted apps-src">${escHtml(_changeText(x))}</span>`).join('')}</span>
    </label>`).join('');
}

async function decidePolicyChanges(kind) {
  const ids = [...polState.selected];
  if (!ids.length) return;
  const body = { ids };
  if (kind === 'approve') {
    body.totp = (document.getElementById('pol-changes-totp')?.value || '').trim();
    if (!body.totp) { showToast('Enter your TOTP code', 'error'); return; }
  }
  try {
    const result = await apiJson(`/api/v1/policies/changes/${kind}/`, { method: 'POST', body: JSON.stringify(body) });
    showToast(kind === 'approve' ? `Sent to ${result.dispatched} host${result.dispatched === 1 ? '' : 's'}`
      : `Rejected ${result.rejected}`, 'success');
    polState.selected.clear();
    const totp = document.getElementById('pol-changes-totp');
    if (totp) totp.value = '';
    fetchPolicyChanges();
  } catch (e) { showToast(e.message, 'error'); }
}

/* ── Editor ──────────────────────────────────────────────────────────── */
function _polEl(id) { return document.getElementById(id); }

async function _loadAppPicker() {
  if (polState.apps) return;
  polState.apps = [];
  try {
    const body = await apiJson('/api/v1/software/apps/?limit=1000');
    for (const app of body.results || []) {
      for (const pkg of app.packages || []) polState.apps.push({ ...pkg, name: app.name });
    }
  } catch { /* the picker is a convenience; typing an id still works */ }
  const list = _polEl('pol-app-list');
  if (list) {
    list.innerHTML = polState.apps.map(a =>
      `<option value="${escAttr(a.id)}">${escHtml(a.name)} (${escHtml(a.source)})</option>`).join('');
  }
}

function setPolicyTab(name) {
  document.querySelectorAll('[data-pol-tab]').forEach(t => t.classList.toggle('active', t.dataset.polTab === name));
  document.querySelectorAll('.pol-panel').forEach(p => p.classList.toggle('active', p.id === `pol-panel-${name}`));
  if (name === 'preview') loadPolicyPreview();
}

function renderPolicyRules() {
  const wrap = _polEl('pol-rules');
  if (!wrap) return;
  wrap.innerHTML = polState.rules.map((r, i) => `<div class="pol-rule" data-pol-rule="${i}">
      <input class="form-control" list="pol-app-list" data-field="app" value="${escAttr(r.app)}" placeholder="App id, e.g. Mozilla.Firefox" aria-label="App">
      <select class="form-control" data-field="source" aria-label="Source">${POL_SOURCES.map(s =>
        `<option value="${s}"${s === r.source ? ' selected' : ''}>${s || 'host\'s own'}</option>`).join('')}</select>
      <select class="form-control" data-field="state" aria-label="State">${POL_STATES.map(([v, l]) =>
        `<option value="${v}"${v === r.state ? ' selected' : ''}>${l}</option>`).join('')}</select>
      <input class="form-control" data-field="version" value="${escAttr(r.version || '')}" placeholder="Version" aria-label="Version"${r.state === 'pinned' ? '' : ' hidden'}>
      <button class="btn btn-ghost btn-xs" type="button" data-pol-remove="${i}" aria-label="Remove">Remove</button>
    </div>`).join('') || '<div class="apps-empty-state">No app rules. Add one, or use only the Patching tab.</div>';
}

function addPolicyRule() {
  polState.rules.push({ app: '', source: '', state: 'present', version: '' });
  renderPolicyRules();
  const inputs = document.querySelectorAll('#pol-rules [data-field="app"]');
  if (inputs.length) inputs[inputs.length - 1].focus();
}

async function openPolicyEditor(policyId) {
  const p = (typeof policyId === 'string' && polState.rows.find(r => r.id === policyId)) || null;
  polState.editing = p ? p.id : null;
  polState.rules = p ? p.app_rules.map(r => ({ ...r })) : [];
  _polEl('pol-title').textContent = p ? p.name : 'New policy';
  _polEl('pol-name').value = p ? p.name : '';
  _polEl('pol-tags').value = p ? p.target_tags.join(', ') : '';
  _polEl('pol-hour').value = p ? p.cron_hour : '2';
  _polEl('pol-minute').value = p ? p.cron_minute : '0';
  _polEl('pol-dow').value = p ? p.cron_dow : '*';
  _polEl('pol-hours').value = p ? p.window_hours : 4;
  _polEl('pol-waves').value = p ? p.wave_group_tag : '';
  _polEl('pol-enabled').checked = p ? p.enabled : true;
  _polEl('pol-approve').checked = p ? p.approval_mode === 'approve' : false;
  _polEl('pol-patch').checked = p ? p.patch_enabled : false;
  const classes = p ? p.windows_classifications : POL_DEFAULT_CLASSES;
  _polEl('pol-classes').innerHTML = POL_CLASSIFICATIONS.map(c => `<label class="setting-check">
      <input type="checkbox" value="${escAttr(c)}"${classes.includes(c) ? ' checked' : ''}> ${escHtml(c)}</label>`).join('');
  _polEl('pol-defer').value = p ? p.deferral_days : 7;
  _polEl('pol-reboot').value = p ? p.reboot : 'in_window';
  _polEl('pol-linux').value = p ? p.linux_updates : 'security';
  _polEl('pol-high-risk').checked = p ? p.allow_high_risk : false;
  _polEl('pol-delete').hidden = !p;
  _polEl('pol-run').hidden = !p;
  _polEl('pol-error').classList.remove('show');
  renderPolicyRules();
  setPolicyTab('apps');
  _polEl('pol-overlay').classList.add('open');
  _polEl('pol-modal').classList.add('open');
  _polEl('pol-name').focus();
  _loadAppPicker();
}

function closePolicyEditor() {
  _polEl('pol-overlay').classList.remove('open');
  _polEl('pol-modal').classList.remove('open');
}

function _policyPayload() {
  const tags = _polEl('pol-tags').value.split(',').map(t => t.trim()).filter(Boolean);
  const rules = polState.rules.filter(r => r.app.trim()).map(r => ({
    app: r.app.trim(), source: r.source, state: r.state,
    ...(r.state === 'pinned' ? { version: (r.version || '').trim() } : {}),
  }));
  return {
    name: _polEl('pol-name').value.trim(),
    target_tags: tags,
    cron_hour: _polEl('pol-hour').value.trim(),
    cron_minute: _polEl('pol-minute').value.trim(),
    cron_dow: _polEl('pol-dow').value.trim() || '*',
    window_hours: parseInt(_polEl('pol-hours').value, 10),
    wave_group_tag: _polEl('pol-waves').value.trim(),
    enabled: _polEl('pol-enabled').checked,
    approval_mode: _polEl('pol-approve').checked ? 'approve' : 'automatic',
    patch_enabled: _polEl('pol-patch').checked,
    windows_classifications: [...document.querySelectorAll('#pol-classes input:checked')].map(i => i.value),
    deferral_days: parseInt(_polEl('pol-defer').value, 10),
    reboot: _polEl('pol-reboot').value,
    linux_updates: _polEl('pol-linux').value,
    app_rules: rules,
  };
}

async function savePolicy() {
  const body = _policyPayload();
  const current = polState.rows.find(r => r.id === polState.editing);
  const wantHighRisk = _polEl('pol-high-risk').checked;
  if (wantHighRisk !== (current ? current.allow_high_risk : false)) {
    body.allow_high_risk = wantHighRisk;
    if (wantHighRisk) {
      const code = window.prompt('Allowing unattended restarts needs your TOTP code:');
      if (!code) return;
      body.totp = code.trim();
    }
  }
  const url = polState.editing ? `/api/v1/policies/${polState.editing}/` : '/api/v1/policies/';
  try {
    const saved = await apiJson(url, { method: polState.editing ? 'PUT' : 'POST', body: JSON.stringify(body) });
    showToast(`Saved ${saved.name}`, 'success');
    await fetchPolicies();
    polState.editing = saved.id;
    _polEl('pol-title').textContent = saved.name;
    _polEl('pol-delete').hidden = false;
    _polEl('pol-run').hidden = false;
    _polEl('pol-error').classList.remove('show');
    setPolicyTab('preview');
  } catch (e) {
    const err = _polEl('pol-error');
    err.textContent = e.message;
    err.classList.add('show');
  }
}

async function deletePolicy() {
  if (!polState.editing) return;
  const p = polState.rows.find(r => r.id === polState.editing);
  if (!window.confirm(`Delete the policy "${p ? p.name : ''}"? Its generated task is archived, not deleted.`)) return;
  try {
    await fetch(`/api/v1/policies/${polState.editing}/`, {
      method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRFToken': getCsrf() },
    });
    closePolicyEditor();
    fetchPolicies();
  } catch (e) { showToast(e.message, 'error'); }
}

async function loadPolicyPreview() {
  const wrap = _polEl('pol-preview');
  if (!wrap || !polState.editing) return;
  wrap.innerHTML = '<div class="apps-detail-loading">Working out what would change…</div>';
  let drift;
  try { drift = await apiJson(`/api/v1/policies/${polState.editing}/drift/`); } catch (e) {
    wrap.innerHTML = `<div class="apps-empty-state">${escHtml(e.message)}</div>`;
    return;
  }
  const p = polState.rows.find(r => r.id === polState.editing);
  const summary = `<div class="pol-summary">
      <span class="chip ${drift.hosts.length ? 'apps-chip-warn' : 'chip-mint'}">${drift.hosts.length} to change</span>
      <span class="chip chip-mint">${drift.compliant} already in line</span>
      ${drift.unknown.length ? `<span class="chip chip-muted" title="${escAttr(drift.unknown.map(u => u.hostname).join(', '))}">${drift.unknown.length} not reported yet</span>` : ''}
      ${p && p.high_risk && !p.allow_high_risk ? '<span class="chip chip-rose">restarts not allowed yet — runs are refused</span>' : ''}
    </div>`;
  const rows = drift.hosts.map(h => `<div class="apps-detail-item">
      <span class="apps-name">${escHtml(h.hostname)}</span>
      ${h.changes.map(c => `<span class="chip chip-muted apps-src">${escHtml(_changeText(c))}</span>`).join('')}
    </div>`).join('');
  wrap.innerHTML = summary + (rows ? `<div class="apps-detail pol-drift">${rows}</div>` : '');
  const link = _polEl('pol-view-task');
  if (link) link.hidden = !(p && p.task_definition);
}

async function runPolicyNow() {
  const totp = (_polEl('pol-run-totp')?.value || '').trim();
  if (!totp) { showToast('Enter your TOTP code', 'error'); return; }
  try {
    const result = await apiJson(`/api/v1/policies/${polState.editing}/run/`, { method: 'POST', body: JSON.stringify({ totp }) });
    _polEl('pol-run-totp').value = '';
    if (result.error) { showToast(result.error, 'error'); return; }
    const text = result.mode === 'approval' ? `${result.queued} change${result.queued === 1 ? '' : 's'} waiting for approval`
      : result.mode === 'rollout' ? `Rollout started for ${result.dispatched} host${result.dispatched === 1 ? '' : 's'}`
        : `Sent to ${result.dispatched} host${result.dispatched === 1 ? '' : 's'}`;
    showToast(text, 'success');
    fetchPolicyChanges();
  } catch (e) { showToast(e.message, 'error'); }
}

/* ── Wiring ──────────────────────────────────────────────────────────── */
delegateClick('[data-pol-row]', (el) => openPolicyEditor(el.dataset.polRow));
delegateClick('[data-pol-tab]', (el) => setPolicyTab(el.dataset.polTab));
delegateClick('[data-pol-remove]', (el) => {
  polState.rules.splice(parseInt(el.dataset.polRemove, 10), 1);
  renderPolicyRules();
});
delegateClick('[data-pol-change]', (el) => {
  if (el.checked) polState.selected.add(el.dataset.polChange);
  else polState.selected.delete(el.dataset.polChange);
  const actions = document.getElementById('pol-changes-actions');
  if (actions) actions.hidden = polState.selected.size === 0;
});
delegateClick('[data-pol-decide]', (el) => decidePolicyChanges(el.dataset.polDecide));
delegateClick('#pol-view-task', (el, ev) => {
  ev.preventDefault();
  const p = polState.rows.find(r => r.id === polState.editing);
  if (!p || !p.task_definition) return;
  closePolicyEditor();
  if (typeof openDefinitionEditor === 'function') openDefinitionEditor(p.task_definition);
});

document.addEventListener('DOMContentLoaded', () => {
  const rules = document.getElementById('pol-rules');
  if (rules) {
    const sync = (ev) => {
      const row = ev.target.closest('[data-pol-rule]');
      const field = ev.target.dataset.field;
      if (!row || !field) return;
      const rule = polState.rules[parseInt(row.dataset.polRule, 10)];
      rule[field] = ev.target.value;
      if (field === 'state') renderPolicyRules();
    };
    rules.addEventListener('input', sync);
    rules.addEventListener('change', sync);
  }
  const overlay = document.getElementById('pol-overlay');
  if (overlay) overlay.addEventListener('click', closePolicyEditor);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && document.getElementById('pol-modal')?.classList.contains('open')) closePolicyEditor();
  });
});

const _origNavForPolicies = navigateTo;
navigateTo = function(pageName) {
  _origNavForPolicies(pageName);
  if (pageName === 'policies') {
    fetchPolicies();
    fetchPolicyChanges();
    fetchCompliance();
  }
};
