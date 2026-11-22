// @ts-check
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

/** One app rule as the API row carries it and as the editor edits it. */
/** @typedef {{app: string, source: string, state: string, version: string}} PolRule */
/** What /api/v1/policies/drift/?policy=<id> reports for one host that has
 *  drifted: policy_drift() always gives those rows a `changes` list. */
/** @typedef {{host_id: string, hostname: string, changes: object[]}} PolDriftHost */
/** One row of /api/v1/policies/ (apps/policies/views.py _row, described by
 *  PolicySerializer). `window_hours` and `deferral_days` are integers the editor
 *  assigns to a text input's `.value`; DOM assignment coerces them to text at run
 *  time, so those two statements carry a cast. */
/** @typedef {{id: string, name: string, enabled: boolean, target_tags: string[],
 *  site_id: string|null, cron_hour: string, cron_minute: string, cron_dow: string,
 *  window_hours: number, wave_group_tag: string, approval_mode: string,
 *  patch_enabled: boolean, windows_classifications: string[], deferral_days: number,
 *  reboot: string, linux_updates: string, allow_high_risk: boolean, high_risk: boolean,
 *  task_definition: string|null, app_rules: PolRule[]}} PolRow */

const POL_CLASSIFICATIONS = [
  'Critical Updates', 'Security Updates', 'Definition Updates', 'Update Rollups',
  'Updates', 'Feature Packs', 'Service Packs', 'Drivers', 'Tools', 'Upgrades',
];
const POL_DEFAULT_CLASSES = ['Critical Updates', 'Security Updates', 'Definition Updates'];
const POL_SOURCES = ['', 'dpkg', 'rpm', 'apk', 'pacman', 'flatpak', 'snap',
  'winget', 'chocolatey', 'scoop', 'registry'];
const POL_STATES = [['present', t('Present')], ['latest', t('Latest')], ['pinned', t('Pinned')], ['absent', t('Absent')]];
// i18n-ignore — a product name.
const POL_PLAN = 'SQSY Business';
const POL_COLUMNS = 6;

/** @type {{rows: PolRow[], changes: {id: string, policy_id: string, policy: string, hostname: string, changes: any[], state: string, created_at: string}[], selected: Set<string>, editing: string|null, rules: PolRule[], apps: {id: string, source: string, name?: string}[]|null, explainSet: boolean}} */
const polState = {
  rows: [], changes: [], selected: new Set(), editing: null, rules: [], apps: null, explainSet: false };

/* ── List ────────────────────────────────────────────────────────────── */
/** @param {{cron_dow: string, cron_hour: string, cron_minute: string, window_hours: number}} p @returns {string} */
function _polWindow(p) {
  // A guard against a count that needs no padding; RegExp.test coerces the
  // number to this same text before matching.
  const pad = (/** @type {string|number} */ v) => (/^\d+$/.test(String(v)) ? String(v).padStart(2, '0') : v);
  const days = p.cron_dow === '*' ? t('daily') : t('days {days}', { days: p.cron_dow });
  return `${pad(p.cron_hour)}:${pad(p.cron_minute)} ${days}, ${p.window_hours}h`;
}

/** @returns {Promise<void>} */
async function fetchPolicies() {
  const tbody = /** @type {HTMLElement|null} */ (document.getElementById('pol-table-body'));
  if (tbody) tbody.innerHTML = stateRowHtml(POL_COLUMNS, loadingHtml());
  try {
    polState.rows = (await apiJson('/api/v1/policies/')).results || [];
  } catch (e) {
    if (tbody) {
      tbody.innerHTML = stateRowHtml(POL_COLUMNS, '');
      showLoadFailure(/** @type {HTMLTableCellElement | null} */ (tbody.querySelector('td')), t('policies'), e, fetchPolicies);
    }
    return;
  }
  renderPolicies();
  const ex = /** @type {HTMLDetailsElement|null} */ (document.getElementById('pol-explain'));
  if (ex && !polState.explainSet) { ex.open = polState.rows.length === 0; polState.explainSet = true; }
}

/** @returns {void} */
function renderPolicies() {
  const tbody = /** @type {HTMLElement|null} */ (document.getElementById('pol-table-body'));
  if (!tbody) return;
  /** @type {Record<string, number>} */
  const pendingBy = {};
  for (const c of polState.changes) pendingBy[c.policy_id] = (pendingBy[c.policy_id] || 0) + 1;
  const html = polState.rows.map(p => {
    const tags = p.target_tags.length
      ? p.target_tags.map(tag => `<span class="chip chip-muted apps-src">${escHtml(tag)}</span>`).join('')
      : `<span class="apps-zero">${escHtml(t('every managed host'))}</span>`;
    const patching = p.patch_enabled
      ? `<span class="chip">${escHtml(t('on'))}</span>${p.reboot !== 'never' ? ` <span class="apps-zero">${escHtml(t('restart: {reboot}', { reboot: p.reboot.replace('_', ' ') }))}</span>` : ''}`
      : `<span class="apps-zero">${escHtml(t('off'))}</span>`;
    const pending = pendingBy[p.id] ? `<span class="chip apps-chip-warn">${escHtml(t('{n} waiting', { n: pendingBy[p.id] }))}</span>` : '';
    const mode = p.approval_mode === 'approve' ? `<span class="apps-zero">${escHtml(t('approve each'))}</span>` : `<span class="apps-zero">${escHtml(t('automatic'))}</span>`;
    return `<tr class="apps-row" data-pol-row="${escAttr(p.id)}">
      <td><span class="apps-name">${escHtml(p.name)}</span>${p.enabled ? '' : ` <span class="chip chip-muted">${escHtml(t('disabled'))}</span>`}</td>
      <td>${tags}</td>
      <td class="apps-mono">${escHtml(_polWindow(p))}</td>
      <td class="num">${p.app_rules.length}</td>
      <td>${patching}</td>
      <td>${pending || mode}</td>
    </tr>`;
  }).join('');
  tbody.innerHTML = html || `<tr><td colspan="${POL_COLUMNS}" class="apps-empty-state">${escHtml(t('No policies yet. A policy keeps a set of apps present, current, pinned or absent — and can install OS updates — on the hosts it names, in a maintenance window.'))}</td></tr>`;
}

/* ── Compliance (numbers Free; the report is Business) ───────────────── */
/** @returns {Promise<void>} */
async function fetchCompliance() {
  const wrap = /** @type {HTMLElement|null} */ (document.getElementById('pol-compliance'));
  if (!wrap) return;
  let c;
  try { c = await apiJson('/api/v1/policies/compliance/'); } catch { wrap.innerHTML = ''; return; /* state-ok: a chip row above the list, not a list */ }
  const late = (c.missing_by_age.critical['31-90'] || 0) + (c.missing_by_age.critical['90+'] || 0);
  wrap.innerHTML = `<span class="chip ${c.patched_pct >= 95 ? 'chip-mint' : 'apps-chip-warn'}">${escHtml(t('{pct}% of hosts patched within SLA', { pct: c.patched_pct }))}</span>
    <span class="chip chip-muted">${escHtml(t('{patched} of {total} hosts with nothing overdue', { patched: c.hosts_patched, total: c.hosts_total }))}</span>
    ${late ? `<span class="chip chip-rose">${escHtml(tn('{n} critical update missing over 30 days', '{n} critical updates missing over 30 days', late))}</span>` : ''}`;
}

/** @returns {Promise<void>} */
async function openComplianceReport() {
  const resp = await fetch('/api/v1/compliance/report/', { credentials: 'same-origin' });
  if (resp.status === 402) {
    showToast(t("The per-site compliance report, its export and branding need {plan}.", { plan: POL_PLAN }), 'error');
    return;
  }
  window.open('/api/v1/compliance/report.html', '_blank', 'noopener');
}

/* ── Waiting for approval ────────────────────────────────────────────── */
/** @param {{app?: string, title?: string, update?: string, action?: string, have?: string, want?: string}} c @returns {string} */
function _changeText(c) {
  if (c.update) return `${c.update}${c.title && c.title !== c.update ? ` — ${c.title}` : ''}`;
  const want = c.want ? ` → ${c.want}` : '';
  return `${c.action} ${c.app}${c.have ? ` (${c.have}${want})` : want}`;
}

/** @returns {Promise<void>} */
async function fetchPolicyChanges() {
  const wrap = /** @type {HTMLElement|null} */ (document.getElementById('pol-changes-list'));
  if (wrap) showLoading(wrap, t('Loading changes…'));
  try {
    polState.changes = (await apiJson('/api/v1/policies/changes/?state=pending')).results || [];
  } catch (e) {
    if (wrap) showLoadFailure(wrap, t('the changes waiting for approval'), e, fetchPolicyChanges);
    return;
  }
  const ids = new Set(polState.changes.map(c => c.id));
  for (const id of [...polState.selected]) if (!ids.has(id)) polState.selected.delete(id);
  renderPolicyChanges();
  renderPolicies();
}

/** @returns {void} */
function renderPolicyChanges() {
  const wrap = /** @type {HTMLElement|null} */ (document.getElementById('pol-changes-list'));
  if (!wrap) return;
  const actions = /** @type {HTMLElement|null} */ (document.getElementById('pol-changes-actions'));
  if (actions) actions.hidden = polState.selected.size === 0;
  if (!polState.changes.length) {
    wrap.innerHTML = `<div class="apps-empty-state">${escHtml(t('Nothing waiting. Policies set to approve each change list what they would do here.'))}</div>`;
    return;
  }
  wrap.innerHTML = polState.changes.map(c => `<label class="pol-change">
      <input type="checkbox" data-pol-change="${escAttr(c.id)}"${polState.selected.has(c.id) ? ' checked' : ''}>
      <span class="pol-change-head"><span class="apps-name">${escHtml(c.hostname)}</span>
        <span class="apps-zero">${escHtml(c.policy)} · ${escHtml(timeAgo(c.created_at) || '')}</span></span>
      <span class="pol-change-list">${c.changes.map(x => `<span class="chip chip-muted apps-src">${escHtml(_changeText(x))}</span>`).join('')}</span>
    </label>`).join('');
}

/** @param {'approve' | 'reject'} kind @returns {Promise<void>} */
async function decidePolicyChanges(kind) {
  const ids = [...polState.selected];
  if (!ids.length) return;
  const body = { ids };
  if (kind === 'approve') {
    body.totp = await totpPrompt(tn('Sending {n} approved change to their hosts.', 'Sending {n} approved changes to their hosts.', ids.length),
      { title: t('Approve changes'), confirmText: t('Approve and send') });
    if (!body.totp) return;
  }
  try {
    const result = await apiJson(`/api/v1/policies/changes/${kind}/`, { method: 'POST', body: JSON.stringify(body) });
    showToast(kind === 'approve' ? tn('Sent to {n} host', 'Sent to {n} hosts', result.dispatched)
      : t('Rejected {count}', { count: result.rejected }), 'success');
    polState.selected.clear();
    fetchPolicyChanges();
  } catch (e) { showToast(e.message, 'error'); }
}

/* ── Editor ──────────────────────────────────────────────────────────── */
/** @param {string} id @returns {HTMLElement|null} */
function _polEl(id) { return /** @type {HTMLElement|null} */ (document.getElementById(id)); }

/** @returns {Promise<void>} */
async function _loadAppPicker() {
  if (polState.apps) return;
  polState.apps = [];
  try {
    const body = await apiJson('/api/v1/software/apps/?limit=1000');
    for (const app of body.results || []) {
      for (const pkg of app.packages || []) polState.apps.push({ ...pkg, name: app.name });
    }
  } catch {
    // state-ok: the picker is a convenience; typing an id still works
  }
  const list = _polEl('pol-app-list');
  if (list) {
    list.innerHTML = polState.apps.map(a =>
      `<option value="${escAttr(a.id)}">${escHtml(a.name)} (${escHtml(a.source)})</option>`).join('');
  }
}

/** @param {string} name @returns {void} */
function setPolicyTab(name) {
  for (const t of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('[data-pol-tab]'))) t.classList.toggle('active', t.dataset.polTab === name);
  for (const p of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('.pol-panel'))) p.classList.toggle('active', p.id === `pol-panel-${name}`);
  if (name === 'preview') loadPolicyPreview();
}

/** @returns {void} */
function renderPolicyRules() {
  const wrap = _polEl('pol-rules');
  if (!wrap) return;
  wrap.innerHTML = polState.rules.map((r, i) => `<div class="pol-rule" data-pol-rule="${i}">
      <input class="form-control" list="pol-app-list" data-field="app" value="${escAttr(r.app)}" placeholder="${escAttr(t('App id, e.g. Mozilla.Firefox'))}" aria-label="${escAttr(t('App'))}">
      <select class="form-control" data-field="source" aria-label="${escAttr(t('Source'))}">${POL_SOURCES.map(s =>
        `<option value="${s}"${s === r.source ? ' selected' : ''}>${s || escHtml(t("host's own"))}</option>`).join('')}</select>
      <select class="form-control" data-field="state" aria-label="${escAttr(t('State'))}">${POL_STATES.map(([v, l]) =>
        `<option value="${v}"${v === r.state ? ' selected' : ''}>${l}</option>`).join('')}</select>
      <input class="form-control" data-field="version" value="${escAttr(r.version || '')}" placeholder="${escAttr(t('Version'))}" aria-label="${escAttr(t('Version'))}"${r.state === 'pinned' ? '' : ' hidden'}>
      <button class="btn btn-ghost btn-xs" type="button" data-pol-remove="${i}" aria-label="${escAttr(t('Remove'))}">${escHtml(t('Remove'))}</button>
    </div>`).join('') || `<div class="apps-empty-state">${escHtml(t('No app rules. Add one, or use only the Patching tab.'))}</div>`;
}

/** @returns {void} */
function addPolicyRule() {
  polState.rules.push({ app: '', source: '', state: 'present', version: '' });
  renderPolicyRules();
  const inputs = /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll('#pol-rules [data-field="app"]'));
  if (inputs.length) inputs[inputs.length - 1].focus();
}

async function openPolicyEditor(policyId) {
  const p = /** @type {PolRow|null} */ ((typeof policyId === 'string' && polState.rows.find(r => r.id === policyId)) || null);
  polState.editing = p ? p.id : null;
  polState.rules = p ? p.app_rules.map(r => ({ ...r })) : [];
  _polEl('pol-title').textContent = p ? p.name : t('New policy');
  /** @type {HTMLInputElement} */ (_polEl('pol-name')).value = p ? p.name : '';
  sitePickFill(/** @type {HTMLSelectElement} */ (_polEl('pol-site')), _polEl('pol-site-field'),
    p ? p.site_id : null, { editing: !!p });
  /** @type {HTMLInputElement} */ (_polEl('pol-tags')).value = p ? p.target_tags.join(', ') : '';
  /** @type {HTMLInputElement} */ (_polEl('pol-hour')).value = p ? p.cron_hour : '2';
  /** @type {HTMLInputElement} */ (_polEl('pol-minute')).value = p ? p.cron_minute : '0';
  /** @type {HTMLInputElement} */ (_polEl('pol-dow')).value = p ? p.cron_dow : '*';
  // The browser coerces this number to text on assignment; the cast states that.
  /** @type {HTMLInputElement} */ (_polEl('pol-hours')).value = p ? String(p.window_hours) : '4';
  /** @type {HTMLInputElement} */ (_polEl('pol-waves')).value = p ? p.wave_group_tag : '';
  /** @type {HTMLInputElement} */ (_polEl('pol-enabled')).checked = p ? p.enabled : true;
  /** @type {HTMLInputElement} */ (_polEl('pol-approve')).checked = p ? p.approval_mode === 'approve' : false;
  /** @type {HTMLInputElement} */ (_polEl('pol-patch')).checked = p ? p.patch_enabled : false;
  const classes = p ? p.windows_classifications : POL_DEFAULT_CLASSES;
  _polEl('pol-classes').innerHTML = POL_CLASSIFICATIONS.map(c => `<label class="setting-check">
      <input type="checkbox" value="${escAttr(c)}"${classes.includes(c) ? ' checked' : ''}> ${escHtml(c)}</label>`).join('');
  /** @type {HTMLInputElement} */ (_polEl('pol-defer')).value = p ? String(p.deferral_days) : '7';
  /** @type {HTMLSelectElement} */ (_polEl('pol-reboot')).value = p ? p.reboot : 'in_window';
  /** @type {HTMLSelectElement} */ (_polEl('pol-linux')).value = p ? p.linux_updates : 'security';
  /** @type {HTMLInputElement} */ (_polEl('pol-high-risk')).checked = p ? p.allow_high_risk : false;
  _polEl('pol-delete').hidden = !p;
  _polEl('pol-run').hidden = !p;
  _polEl('pol-error').classList.remove('show');
  renderPolicyRules();
  setPolicyTab('apps');
  _polEl('pol-overlay').classList.add('open');
  _polEl('pol-modal').classList.add('open');
  /** @type {HTMLInputElement} */ (_polEl('pol-name')).focus();
  _loadAppPicker();
}

/** From the Apps page: put an app's rules into a new policy or an existing
 *  one. Opens the editor with the rules added (state Latest); nothing is
 *  saved until Save.
 *
 *  `opts.windowsClassifications` is the same idea for OS updates: the Windows
 *  Update classifications an update actually arrived under, ticked into the
 *  Patching tab so the policy fetches that kind of update. */
/** @param {string} appName @param {{app: string, source: string}[]} pairs @param {string[]} classifications @returns {string} */
function _polNewMeta(appName, pairs, classifications) {
  if (!pairs.length && classifications.length) return t('patches {classifications} on Windows', { classifications: classifications.join(', ') });
  if (pairs.length && classifications.length) {
    return t('keeps the packages up to date and patches {classifications} on Windows', { classifications: classifications.join(', ') });
  }
  return t('keeps {appName} installed and up to date', { appName });
}

/** @param {string} appName @param {{app: string, source: string}[]} pairs @param {{windowsClassifications?: string[]}=} opts @returns {Promise<void>} */
async function policyFromApp(appName, pairs, opts = {}) {
  try {
    polState.rows = (await apiJson('/api/v1/policies/')).results || [];
  } catch (e) {
    showToast(t('Update policies need an admin account: {message}', { message: e.message }), 'error');
    return;
  }
  const want = (opts.windowsClassifications || []).filter((/** @type {string} */ c) => POL_CLASSIFICATIONS.includes(c));
  const items = [
    { key: '', name: t('New policy'), meta: _polNewMeta(appName, pairs, want) },
    ...polState.rows.map(p => ({ key: p.id, name: p.name,
      meta: tn('{n} app rule', '{n} app rules', p.app_rules.length) + (p.enabled ? '' : ' · ' + t('disabled')) })),
  ];
  openPicker({
    type: 'policy', title: t('Add {appName} to a policy', { appName }), allowAdd: false, items,
    onSelect: async (item) => {
      navigateTo('policies');
      await openPolicyEditor(item.key || null);
      if (!item.key) /** @type {HTMLInputElement} */ (_polEl('pol-name')).value = t('{appName} updates', { appName });
      let added = 0;
      for (const pr of pairs) {
        if (polState.rules.some(r => r.app === pr.app && r.source === pr.source)) continue;
        polState.rules.push({ app: pr.app, source: pr.source, state: 'latest', version: '' });
        added += 1;
      }
      renderPolicyRules();
      let patched = 0;
      if (want.length) {
        /** @type {HTMLInputElement} */ (_polEl('pol-patch')).checked = true;
        for (const box of /** @type {NodeListOf<HTMLInputElement>} */ (_polEl('pol-classes').querySelectorAll('input'))) {
          if (!want.includes(box.value)) continue;
          box.checked = true;
          patched += 1;
        }
      }
      const rules = added ? tn('{n} app rule', '{n} app rules', added) : '';
      const types = patched ? tn('{n} update type', '{n} update types', patched) : '';
      const what = rules && types ? t('{rules} and {types}', { rules, types }) : rules || types;
      showToast(what ? t('Added {appName}: {what}. Choose the hosts, then Save.', { appName, what })
        : t('{appName} is already in this policy', { appName }),
        what ? 'success' : 'info');
    },
  });
}

function closePolicyEditor() {
  _polEl('pol-overlay').classList.remove('open');
  _polEl('pol-modal').classList.remove('open');
}

/** @returns {Record<string, any>} */
function _policyPayload() {
  const tags = /** @type {HTMLInputElement} */ (_polEl('pol-tags')).value.split(',').map(t => t.trim()).filter(Boolean);
  const rules = polState.rules.filter(r => r.app.trim()).map(r => ({
    app: r.app.trim(), source: r.source, state: r.state,
    ...(r.state === 'pinned' ? { version: (r.version || '').trim() } : {}),
  }));
  return {
    name: /** @type {HTMLInputElement} */ (_polEl('pol-name')).value.trim(),
    target_tags: tags,
    cron_hour: /** @type {HTMLInputElement} */ (_polEl('pol-hour')).value.trim(),
    cron_minute: /** @type {HTMLInputElement} */ (_polEl('pol-minute')).value.trim(),
    cron_dow: /** @type {HTMLInputElement} */ (_polEl('pol-dow')).value.trim() || '*',
    window_hours: parseInt(/** @type {HTMLInputElement} */ (_polEl('pol-hours')).value, 10),
    wave_group_tag: /** @type {HTMLInputElement} */ (_polEl('pol-waves')).value.trim(),
    enabled: /** @type {HTMLInputElement} */ (_polEl('pol-enabled')).checked,
    approval_mode: /** @type {HTMLInputElement} */ (_polEl('pol-approve')).checked ? 'approve' : 'automatic',
    patch_enabled: /** @type {HTMLInputElement} */ (_polEl('pol-patch')).checked,
    windows_classifications: [...document.querySelectorAll('#pol-classes input:checked')].map((/** @type {HTMLInputElement} */ i) => i.value),
    deferral_days: parseInt(/** @type {HTMLInputElement} */ (_polEl('pol-defer')).value, 10),
    reboot: /** @type {HTMLSelectElement} */ (_polEl('pol-reboot')).value,
    linux_updates: /** @type {HTMLSelectElement} */ (_polEl('pol-linux')).value,
    app_rules: rules,
  };
}

/** @returns {Promise<void>} */
async function savePolicy() {
  const body = _policyPayload();
  const siteId = sitePickValue(/** @type {HTMLSelectElement} */ (_polEl('pol-site')), _polEl('pol-site-field'));
  if (siteId !== undefined) body.site_id = siteId;
  const current = polState.rows.find(r => r.id === polState.editing);
  const wantHighRisk = /** @type {HTMLInputElement} */ (_polEl('pol-high-risk')).checked;
  if (wantHighRisk !== (current ? current.allow_high_risk : false)) {
    body.allow_high_risk = wantHighRisk;
    if (wantHighRisk) {
      const code = await totpPrompt(t('Allow unattended restarts from this policy.'));
      if (!code) return;
      body.totp = code.trim();
    }
  }
  const url = polState.editing ? `/api/v1/policies/${polState.editing}/` : '/api/v1/policies/';
  try {
    const saved = await apiJson(url, { method: polState.editing ? 'PUT' : 'POST', body: JSON.stringify(body) });
    showToast(t('Saved {name}', { name: saved.name }), 'success');
    await fetchPolicies();
    closePolicyEditor();
  } catch (e) {
    const err = _polEl('pol-error');
    err.textContent = e.message;
    err.classList.add('show');
  }
}

/** @returns {Promise<void>} */
async function deletePolicy() {
  if (!polState.editing) return;
  const p = polState.rows.find(r => r.id === polState.editing);
  if (!window.confirm(t('Delete the policy "{name}"? Its generated task is archived, not deleted.', { name: p ? p.name : '' }))) return;
  try {
    await fetch(`/api/v1/policies/${polState.editing}/`, {
      method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRFToken': getCsrf() },
    });
    closePolicyEditor();
    fetchPolicies();
  } catch (e) { showToast(e.message, 'error'); }
}

/** @returns {Promise<void>} */
async function loadPolicyPreview() {
  const wrap = _polEl('pol-preview');
  if (!wrap || !polState.editing) return;
  wrap.innerHTML = loadingHtml(t('Working out what would change…'));
  /** @type {{hosts: PolDriftHost[], compliant: number, unknown: {host_id: string, hostname: string}[]}} */
  let drift;
  try { drift = await apiJson(`/api/v1/policies/${polState.editing}/drift/`); } catch (e) {
    showLoadFailure(wrap, t('the preview of what would change'), e, loadPolicyPreview);
    return;
  }
  const p = polState.rows.find(r => r.id === polState.editing);
  const unknownNames = drift.unknown.map(u => u.hostname).join(', ');
  const summary = `<div class="pol-summary">
      <span class="chip ${drift.hosts.length ? 'apps-chip-warn' : 'chip-mint'}">${escHtml(t('{n} to change', { n: drift.hosts.length }))}</span>
      <span class="chip chip-mint">${escHtml(t('{compliant} already in line', { compliant: drift.compliant }))}</span>
      ${drift.unknown.length ? `<span class="chip chip-muted" title="${escAttr(unknownNames)}">${escHtml(t('{n} not reported yet', { n: drift.unknown.length }))}</span>` : ''}
      ${p && p.high_risk && !p.allow_high_risk ? `<span class="chip chip-rose">${escHtml(t('restarts not allowed yet — runs are refused'))}</span>` : ''}
    </div>`;
  const rows = drift.hosts.map(h => `<div class="apps-detail-item">
      <span class="apps-name">${escHtml(h.hostname)}</span>
      ${h.changes.map(c => `<span class="chip chip-muted apps-src">${escHtml(_changeText(c))}</span>`).join('')}
    </div>`).join('');
  wrap.innerHTML = summary + (rows ? `<div class="apps-detail pol-drift">${rows}</div>` : '');
  const link = _polEl('pol-view-task');
  if (link) link.hidden = !(p && p.task_definition);
}

/** @returns {Promise<void>} */
async function runPolicyNow() {
  const totp = await totpPrompt(t('Running this policy now applies it to every host it targets.'),
    { title: t('Run policy now'), confirmText: t('Run now') });
  if (!totp) return;
  try {
    const result = await apiJson(`/api/v1/policies/${polState.editing}/run/`, { method: 'POST', body: JSON.stringify({ totp }) });
    if (result.error) { showToast(result.error, 'error'); return; }
    const text = result.mode === 'approval' ? tn('{n} change waiting for approval', '{n} changes waiting for approval', result.queued)
      : result.mode === 'rollout' ? tn('Rollout started for {n} host', 'Rollout started for {n} hosts', result.dispatched)
        : tn('Sent to {n} host', 'Sent to {n} hosts', result.dispatched);
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
window.navigateTo = function(pageName) {
  _origNavForPolicies(pageName);
  if (pageName === 'policies') {
    fetchPolicies();
    fetchPolicyChanges();
    fetchCompliance();
  }
};
