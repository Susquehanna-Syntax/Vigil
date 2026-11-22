// @ts-check
// vigil-apps.js
// Owns: the Apps page (installed software by app / by host) and the Software
//   section on the Monitor page.
// HTML: templates/pages/_apps.html, Software section in templates/pages/_monitor.html
// Depends on: vigil-utils.js (escHtml, escAttr, timeAgo, delegateClick),
//   vigil-nav.js (navigateTo — wrapped below), vigil-monitor.js
//   (selectMonitorHost, reached from the "Open this host's monitor" link).
// API: GET /api/v1/software/apps/, /api/v1/software/apps/<name_key>/,
//      /api/v1/software/hosts/, /api/v1/software/hosts/<id>/,
//      /api/v1/software/updates/, /api/v1/software/updates/hosts/;
//      POST /api/v1/software/updates/decide/ (admin)

const APPS_PAGE_SIZE = 100;
const APPS_DEBOUNCE_MS = 250;
const APPS_COLUMNS = 7;
const APPS_EMPTY_TEXT = t('No software reported yet — agents send their list within a few minutes of starting, or run the "Refresh software inventory" task.');

/** One installed app of /api/v1/software/apps/, or one host row of
 *  /api/v1/software/hosts/, or one installed item of a host's software list. */
/** @typedef {{name?: string, name_key?: string, key?: string, hostname?: string, hosts?: number,
 *  item_count?: number, versions?: Record<string, number>, outdated?: any, unmanaged?: number,
 *  managed?: any, sources?: string[], errors?: Record<string, string>, scope?: string,
 *  user?: string|null, package_id?: string, source?: string, version?: string, host_id?: string,
 *  latest_version?: string, received_at?: string|null, [k: string]: any}} AppRow */

/** The per-host snapshot of /api/v1/software/hosts/<id>/ (`null` when the host
 *  never reported), shown as one line above its software table. */
/** @typedef {{digest?: string, collected_at?: string|null, received_at?: string|null,
 *  item_count?: number, errors?: Record<string, string>}} MonitorSnapshot */

/** @type {{rows: AppRow[], count: number, hosts: AppRow[], seq: number, hostSeq: number,
 *  details: Record<string, AppRow[]|null>, expanded: Record<string, boolean>,
 *  hostDetails: Record<string, AppRow[]|null>, hostExpanded: Record<string, boolean>,
 *  hostShowAll: Record<string, boolean>, monitorItems: AppRow[],
 *  monitorSnapshot: MonitorSnapshot|null, monitorSeq: number,
 *  failed: Record<string, Error>, hostFailed: Record<string, Error>}} */
const appsState = {
  rows: [],
  count: 0,
  hosts: [],
  // A response from a superseded query must not land: the operator can type
  // faster than the fleet answers.
  seq: 0,
  hostSeq: 0,
  details: {},        // name_key → host rows, fetched once per app; null = in flight
  expanded: {},       // name_key → true while its detail row is open
  hostDetails: {},    // host_id → software items, fetched once per host; null = in flight
  hostExpanded: {},   // host_id → true while its detail row is open
  hostShowAll: {},    // host_id → true once "Show all" was clicked
  monitorItems: [],
  monitorSnapshot: null,
  monitorSeq: 0,
  failed: {},         // name_key → the error its host list came back with; the row's Try again clears it
  hostFailed: {},     // host_id → the error its software list came back with
};

/* ── Shared fragments ────────────────────────────────────────────────── */
function _loadingRow() {
  return stateRowHtml(APPS_COLUMNS, loadingHtml());
}

function _emptyRow(text) {
  return `<tr><td colspan="${APPS_COLUMNS}" class="apps-empty-state">${escHtml(text)}</td></tr>`;
}

/** @param {any} value @param {string} cls @returns {string} */
function _countChip(value, cls) {
  return value ? `<span class="chip ${cls}">${value}</span>` : '<span class="apps-zero">0</span>';
}

/** @param {string[]|null|undefined} sources @returns {string} */
function _sourceChips(sources) {
  if (!sources || !sources.length) return '<span class="apps-zero">—</span>';
  return sources.map(s => `<span class="chip chip-muted apps-src">${escHtml(s)}</span>`).join('');
}

// "user: alice" for a user-scoped package, the scope name otherwise.
function _scopeCell(row) {
  return row.scope === 'user'
    ? t('user: {name}', { name: escHtml(row.user || t('unknown')) }) : escHtml(row.scope || '');
}

/** @param {Record<string, any>|null|undefined} errors @returns {string} */
function _errorChips(errors) {
  const entries = Object.entries(errors || {});
  if (!entries.length) return '';
  return entries.map(([source, message]) =>
    `<span class="chip chip-rose apps-err" title="${escAttr(String(message))}">${escHtml(source)}</span>`)
    .join(' ');
}

function _versionChips(versions) {
  const entries = Object.entries(versions || {})
    .sort((a, b) => (b[1] - a[1]) || String(a[0]).localeCompare(String(b[0])));
  if (!entries.length) return '<span class="apps-zero">—</span>';
  const shown = entries.slice(0, 3)
    .map(([version, hosts]) => `<span class="chip chip-muted apps-ver">${escHtml(version)} ×${hosts}</span>`)
    .join('');
  const rest = entries.length - 3;
  return shown + (rest > 0 ? `<span class="apps-more-ver">+${rest} more</span>` : '');
}

/* ── By app ──────────────────────────────────────────────────────────── */
/** @param {number} offset @returns {URLSearchParams} */
function _appsParams(offset) {
  const params = new URLSearchParams();
  const q = /** @type {HTMLInputElement} */ (document.getElementById('apps-q'));
  if (q && q.value.trim()) params.set('q', q.value.trim());
  if (/** @type {HTMLInputElement} */ (document.getElementById('apps-outdated'))?.checked) params.set('outdated', '1');
  if (/** @type {HTMLInputElement} */ (document.getElementById('apps-unmanaged'))?.checked) params.set('unmanaged', '1');
  const source = /** @type {HTMLSelectElement} */ (document.getElementById('apps-source'));
  if (source && source.value) params.set('source', source.value);
  params.set('limit', String(APPS_PAGE_SIZE));
  params.set('offset', String(offset));
  return params;
}

async function fetchApps(reset) {
  const seq = ++appsState.seq;
  // offset is read before the reset so both pages of a rapid "Load more" get
  // their own slice rather than re-reading the first one.
  const params = _appsParams(reset ? 0 : appsState.rows.length);
  if (reset) {
    appsState.rows = [];
    appsState.count = 0;
    appsState.expanded = {};
  }
  const tbody = document.getElementById('apps-table-body');
  if (tbody) tbody.innerHTML = _loadingRow();
  let body;
  try {
    body = await apiJson(`/api/v1/software/apps/?${params}`);
  } catch (e) {
    if (seq !== appsState.seq) return;
    showLoadFailure(tbody, t('the app list'), e, () => fetchApps(true));
    return;
  }
  if (seq !== appsState.seq) return;
  appsState.count = body.count || 0;
  appsState.rows = appsState.rows.concat(body.results || []);
  renderAppsTable();
}

function renderAppsTable() {
  const tbody = document.getElementById('apps-table-body');
  const more = document.getElementById('apps-more');
  if (!tbody) return;
  const hasAnyHostSoftware = appsState.count > 0 || appsState.hosts.some(h => h.item_count > 0);

  let html = '';
  for (const row of appsState.rows) {
    const key = row.name_key;
    const keyNote = key !== row.name
      ? `<div class="apps-key">${escHtml(key)}</div>` : '';
    html += `<tr class="apps-row" data-app-row="${escAttr(key)}">
      <td><span class="apps-name">${escHtml(row.name)}</span>${keyNote}</td>
      <td class="num">${row.hosts}</td>
      <td>${_versionChips(row.versions)}</td>
      <td class="num">${_countChip(row.outdated, 'chip-rose')}</td>
      <td class="num">${_countChip(row.unmanaged, 'apps-chip-warn')}</td>
      <td>${_sourceChips(row.sources)}</td>
      <td class="apps-act"><button class="btn btn-xs btn-outline" type="button" data-app-policy="${escAttr(key)}"
        title="${escAttr(t('Keep {name} installed and up to date with an update policy', { name: row.name }))}">${escHtml(t('Policy…'))}</button></td>
    </tr>`;
    if (appsState.expanded[key]) html += _appDetailHtml(key);
  }
  if (!html) {
    html = _emptyRow(hasAnyHostSoftware ? t('No apps match these filters.') : APPS_EMPTY_TEXT);
  }
  tbody.innerHTML = html;
  for (const key of Object.keys(appsState.failed)) {
    const cell = tbody.querySelector(`[data-app-detail="${escAttr(key)}"] .apps-detail`);
    if (cell) showLoadFailure(/** @type {HTMLElement} */ (cell), t('this app’s hosts'), appsState.failed[key], () => retryAppHosts(key));
  }
  for (const id of Object.keys(appsState.hostFailed)) {
    const cell = tbody.querySelector(`[data-app-host-detail="${escAttr(id)}"] .apps-detail`);
    if (cell) showLoadFailure(/** @type {HTMLElement} */ (cell), t('this host’s software'), appsState.hostFailed[id], () => retryHostSoftware(id));
  }
  if (more) more.hidden = !(appsState.rows.length > 0 && appsState.rows.length < appsState.count);
}

function _appDetailHtml(key) {
  const rows = appsState.details[key];
  if (!rows) {
    return `<tr class="apps-detail-row" data-app-detail="${escAttr(key)}"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail">${loadingHtml(t('Loading hosts…'))}</div></td></tr>`;
  }
  if (!rows.length) {
    return `<tr class="apps-detail-row" data-app-detail="${escAttr(key)}"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail"><div class="apps-detail-loading">${escHtml(t('No hosts report this app in your scope.'))}</div></div></td></tr>`;
  }
  const items = rows.map(row => {
    const latest = row.outdated
      ? `<span class="chip chip-rose" title="${escAttr(t('Latest version'))}">${escHtml(row.latest_version)}</span>` : '';
    const unmanaged = row.managed ? '' : `<span class="chip apps-chip-warn">${escHtml(t('unmanaged'))}</span>`;
    return `<div class="apps-detail-item">
      <a class="apps-host-link" href="#" data-app-host="${escAttr(row.host_id)}">${escHtml(row.hostname)}</a>
      <span class="apps-detail-ver">${escHtml(row.version)}</span>
      ${latest}
      <span class="chip chip-muted apps-src">${escHtml(row.source)}</span>
      <span class="apps-detail-scope">${_scopeCell(row)}</span>
      <span class="apps-detail-pkg">${escHtml(row.package_id)}</span>
      ${unmanaged}
    </div>`;
  }).join('');
  return `<tr class="apps-detail-row" data-app-detail="${escAttr(key)}"><td colspan="${APPS_COLUMNS}">
    <div class="apps-detail">${items}</div></td></tr>`;
}

async function retryAppHosts(key) {
  // The failed host list is drawn inside the app's own expanded row, so its
  // retry clears that cache entry and refetches without collapsing anything.
  delete appsState.details[key];
  delete appsState.failed[key];
  renderAppsTable();
  await loadAppHosts(key);
}

async function loadAppHosts(key) {
  try {
    const body = await apiJson(`/api/v1/software/apps/${encodeURIComponent(key)}/`);
    delete appsState.failed[key];
    appsState.details[key] = body.rows || [];
  } catch (e) {
    if (!appsState.expanded[key]) return;
    appsState.failed[key] = /** @type {Error} */ (e);
  }
  if (appsState.expanded[key]) renderAppsTable();
}

async function toggleAppDetail(key) {
  if (appsState.expanded[key]) {
    delete appsState.expanded[key];
    renderAppsTable();
    return;
  }
  appsState.expanded[key] = true;
  renderAppsTable();
  // The cached rows or the in-flight null stand; a failed row was cleared by its retry.
  if (appsState.details[key] !== undefined) return;
  appsState.details[key] = null;
  await loadAppHosts(key);
}

/* ── By host ─────────────────────────────────────────────────────────── */
async function fetchAppHosts() {
  const seq = ++appsState.hostSeq;
  const tbody = document.getElementById('apps-hosts-body');
  if (tbody) tbody.innerHTML = _loadingRow();
  try {
    const body = await apiJson('/api/v1/software/hosts/');
    if (seq !== appsState.hostSeq) return;
    appsState.hosts = body.results || [];
  } catch (e) {
    if (seq !== appsState.hostSeq) return;
    showLoadFailure(tbody, t('the host list'), e, fetchAppHosts);
    return;
  }
  renderAppsHostsTable();
}

function renderAppsHostsTable() {
  const tbody = document.getElementById('apps-hosts-body');
  if (!tbody) return;
  const html = appsState.hosts.map(row => {
    const open = appsState.hostExpanded[row.host_id] ? ' open' : '';
    let out = `<tr class="apps-row${open}" data-app-host-row="${escAttr(row.host_id)}">
      <td><span class="apps-chev">▸</span> <span class="apps-name">${escHtml(row.hostname)}</span></td>
      <td class="num">${row.item_count}</td>
      <td class="num">${_countChip(row.outdated, 'chip-rose')}</td>
      <td class="num">${_countChip(row.unmanaged, 'apps-chip-warn')}</td>
      <td>${escHtml(timeAgo(row.received_at) || t('never'))}</td>
      <td>${_errorChips(row.errors) || '<span class="apps-zero">—</span>'}</td>
    </tr>`;
    if (appsState.hostExpanded[row.host_id]) out += _hostDetailHtml(row.host_id);
    return out;
  }).join('');
  tbody.innerHTML = html || _emptyRow(t('No hosts in your scope.'));
  renderAppsTable();
}

async function retryHostSoftware(id) {
  // A failed software list is drawn inside the host's own expanded row, so
  // its retry clears just that cache entry and redraws.
  delete appsState.hostDetails[id];
  delete appsState.hostFailed[id];
  renderAppsHostsTable();
  await loadHostSoftware(id);
}

async function loadHostSoftware(id) {
  try {
    const body = await apiJson(`/api/v1/software/hosts/${encodeURIComponent(id)}/`);
    delete appsState.hostFailed[id];
    appsState.hostDetails[id] = body.items || [];
  } catch (e) {
    if (!appsState.hostExpanded[id]) return;
    appsState.hostFailed[id] = /** @type {Error} */ (e);
  }
  if (appsState.hostExpanded[id]) renderAppsHostsTable();
}

function _hostSoftwareSorted(items) {
  // The Monitor page's Software list orders this way; By host's expanded rows match it.
  return [...items].sort((a, b) => {
    if (!!b.outdated !== !!a.outdated) return b.outdated ? 1 : -1;
    if (!!b.managed !== !!a.managed) return b.managed ? 1 : -1;
    return String(a.name || '').localeCompare(String(b.name || ''));
  });
}

function _hostDetailHtml(id) {
  const items = appsState.hostDetails[id];
  if (items === null || items === undefined) {
    return `<tr class="apps-detail-row" data-app-host-detail="${escAttr(id)}"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail">${loadingHtml(t('Loading software…'))}</div></td></tr>`;
  }
  if (!items.length) {
    return `<tr class="apps-detail-row" data-app-host-detail="${escAttr(id)}"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail"><div class="apps-detail-loading">${escHtml(t('No software reported for this host yet.'))}</div></div></td></tr>`;
  }
  const sorted = _hostSoftwareSorted(items);
  const showAll = !!appsState.hostShowAll[id];
  const shown = showAll ? sorted : sorted.slice(0, APPS_HOST_DETAIL_LIMIT);
  const rows = shown.map(it => {
    const unmanaged = it.managed ? '' : ` <span class="chip apps-chip-warn">${escHtml(t('unmanaged'))}</span>`;
    return `<tr${it.outdated ? ' class="upd-outdated"' : ''}>
      <td>${escHtml(it.name)}${unmanaged}</td>
      <td class="apps-mono">${escHtml(it.version)}</td>
      <td>${it.outdated ? `<span class="chip chip-rose">${escHtml(it.latest_version)}</span>` : '<span class="apps-zero">—</span>'}</td>
      <td><span class="chip chip-muted apps-src">${escHtml(it.source)}</span></td>
      <td>${_scopeCell(it)}</td>
    </tr>`;
  }).join('');
  const more = (!showAll && sorted.length > APPS_HOST_DETAIL_LIMIT)
    ? `<div class="apps-show-all">
         <button class="btn btn-outline btn-xs" type="button" data-host-show-all="${escAttr(id)}">${escHtml(t('Show all {n}', { n: sorted.length }))}</button>
       </div>` : '';
  return `<tr class="apps-detail-row" data-app-host-detail="${escAttr(id)}"><td colspan="${APPS_COLUMNS}">
    <div class="apps-detail">
    <table class="hunt-table apps-table apps-host-detail">
      <thead><tr><th>${escHtml(t('Name'))}</th><th>${escHtml(t('Version'))}</th><th>${escHtml(t('Latest'))}</th><th>${escHtml(t('Source'))}</th><th>${escHtml(t('Scope'))}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${more}
    <a href="#" class="apps-host-open" data-app-host="${escAttr(id)}">${escHtml(t("Open this host's monitor →"))}</a>
    </div>
  </td></tr>`;
}

async function toggleHostDetail(id) {
  if (appsState.hostExpanded[id]) {
    delete appsState.hostExpanded[id];
    renderAppsHostsTable();
    return;
  }
  appsState.hostExpanded[id] = true;
  renderAppsHostsTable();
  // The cached rows or the in-flight null stand; a failed row was cleared by its retry.
  if (appsState.hostDetails[id] !== undefined) return;
  appsState.hostDetails[id] = null;
  await loadHostSoftware(id);
}

function refreshApps() {
  appsState.details = {};
  appsState.expanded = {};
  appsState.failed = {};
  appsState.hostDetails = {};
  appsState.hostExpanded = {};
  appsState.hostShowAll = {};
  appsState.hostFailed = {};
  fetchApps(true);
  fetchAppHosts();
  fetchUpdates();
}

function loadMoreApps() { fetchApps(false); }

/* ── By update (M8) ──────────────────────────────────────────────────── */
const UPDATE_COLUMNS = 7;
const updState = { rows: [], seq: 0, selected: new Set(), details: {}, expanded: {}, failed: {} };

function _updKey(row) { return `${row.kind}|${row.key}`; }

function _severityChip(row) {
  const sev = row.severity || (row.kind === 'linux' ? '' : 'unrated');
  if (!sev) return '<span class="apps-zero">—</span>';
  const cls = sev === 'critical' ? 'chip-rose' : sev === 'important' ? 'apps-chip-warn' : 'chip-muted';
  return `<span class="chip ${cls}">${escHtml(sev)}</span>`;
}

function _decisionChip(decision) {
  if (decision === 'approved') return `<span class="chip chip-mint">${escHtml(t('approved'))}</span>`;
  if (decision === 'declined') return `<span class="chip chip-rose">${escHtml(t('declined'))}</span>`;
  return `<span class="apps-zero">${escHtml(t('undecided'))}</span>`;
}

function _ageCell(days) {
  const text = tn('{n} day', '{n} days', days);
  return days > 30 ? `<span class="chip apps-chip-warn">${text}</span>` : text;
}

async function fetchUpdates() {
  const seq = ++updState.seq;
  const tbody = document.getElementById('apps-updates-body');
  if (tbody) tbody.innerHTML = stateRowHtml(UPDATE_COLUMNS, loadingHtml());
  const params = new URLSearchParams();
  for (const [id, name] of [['upd-q', 'q'], ['upd-kind', 'kind'], ['upd-decision', 'decision']]) {
    const value = (/** @type {HTMLInputElement} */ (document.getElementById(id))?.value || '').trim();
    if (value) params.set(name, value);
  }
  try {
    const body = await apiJson(`/api/v1/software/updates/?${params}`);
    if (seq !== updState.seq) return;
    updState.rows = body.results || [];
    const keys = new Set(updState.rows.map(_updKey));
    for (const key of [...updState.selected]) if (!keys.has(key)) updState.selected.delete(key);
  } catch (e) {
    if (seq !== updState.seq) return;
    if (tbody) {
      tbody.innerHTML = stateRowHtml(UPDATE_COLUMNS, '');
      showLoadFailure(/** @type {HTMLTableCellElement | null} */ (tbody.querySelector('td')), t('pending updates'), e, fetchUpdates);
    }
    return;
  }
  renderUpdatesTable();
}

function renderUpdatesTable() {
  const tbody = document.getElementById('apps-updates-body');
  if (!tbody) return;
  const html = updState.rows.map(row => {
    const key = _updKey(row);
    const checked = updState.selected.has(key) ? ' checked' : '';
    let out = `<tr class="apps-row" data-upd-row="${escAttr(key)}">
      <td class="upd-check"><input type="checkbox" data-upd-pick="${escAttr(key)}"${checked} aria-label="${escAttr(t('Select {key}', { key: row.key }))}"></td>
      <td><div class="apps-name">${escHtml(row.key)}</div>
        <div class="upd-title">${escHtml(row.title || '')}${row.classification ? ' · ' + escHtml(row.classification) : ''}</div></td>
      <td>${_severityChip(row)}</td>
      <td class="num">${row.hosts}</td>
      <td class="num">${_ageCell(row.age_days)}</td>
      <td>${_decisionChip(row.decision)}</td>
      <td><button class="btn btn-xs btn-outline" type="button" data-upd-policy="${escAttr(key)}">${escHtml(t('Policy…'))}</button></td>
    </tr>`;
    if (updState.expanded[key]) {
      const hosts = updState.details[key];
      const inner = hosts == null
        ? loadingHtml()
        : hosts.map(h => `<div class="apps-detail-item">
            <a class="apps-host-link" href="#" data-app-host="${escAttr(h.host_id)}">${escHtml(h.hostname)}</a>
            <span class="apps-detail-scope">${escHtml(tn('missing for {n} day', 'missing for {n} days', h.age_days))}</span>
            ${h.version ? `<span class="apps-detail-ver">→ ${escHtml(h.version)}</span>` : ''}
          </div>`).join('') || `<div class="apps-detail-loading">${escHtml(t('No hosts in your scope.'))}</div>`;
      out += `<tr class="apps-detail-row" data-upd-detail="${escAttr(key)}"><td colspan="${UPDATE_COLUMNS}"><div class="apps-detail">${inner}</div></td></tr>`;
    }
    return out;
  }).join('');
  tbody.innerHTML = html || `<tr><td colspan="${UPDATE_COLUMNS}" class="apps-empty-state">${escHtml(
    t('No pending updates reported. Windows hosts send their list after each update scan; Linux hosts after each software inventory.'))}</td></tr>`;
  for (const key of Object.keys(updState.failed)) {
    const cell = tbody.querySelector(`[data-upd-detail="${escAttr(key)}"] .apps-detail`);
    if (cell) showLoadFailure(/** @type {HTMLElement} */ (cell), t('the hosts missing this update'), updState.failed[key], () => retryUpdateHosts(key));
  }
  _renderUpdateBulk();
}

function _renderUpdateBulk() {
  const bar = document.getElementById('upd-bulk');
  const count = updState.selected.size;
  if (bar) bar.hidden = count === 0;
  const label = document.getElementById('upd-bulk-count');
  if (label) label.textContent = t('{n} selected', { n: count });
  const all = /** @type {HTMLInputElement|null} */ (document.getElementById('upd-all'));
  if (all) all.checked = count > 0 && count === updState.rows.length;
}

async function toggleUpdateHosts(key) {
  updState.expanded[key] = !updState.expanded[key];
  if (updState.expanded[key] && updState.details[key] === undefined) await loadUpdateHosts(key);
  renderUpdatesTable();
}

async function loadUpdateHosts(key) {
  updState.details[key] = null;
  const [kind, ...rest] = key.split('|');
  const params = new URLSearchParams({ kind, key: rest.join('|') });
  try {
    const body = await apiJson(`/api/v1/software/updates/hosts/?${params}`);
    delete updState.failed[key];
    updState.details[key] = body.results || [];
  } catch (e) {
    if (!updState.expanded[key]) return;
    updState.failed[key] = /** @type {Error} */ (e);
  }
}

async function retryUpdateHosts(key) {
  // The failed host list sits inside the update's own expanded row, so its
  // retry clears that entry and refetches without collapsing anything.
  delete updState.details[key];
  delete updState.failed[key];
  renderUpdatesTable();
  await loadUpdateHosts(key);
  renderUpdatesTable();
}

async function decideUpdates(decision) {
  const byKind = {};
  for (const key of updState.selected) {
    const [kind, ...rest] = key.split('|');
    (byKind[kind] = byKind[kind] || []).push(rest.join('|'));
  }
  try {
    for (const [kind, keys] of Object.entries(byKind)) {
      await apiJson('/api/v1/software/updates/decide/', {
        method: 'POST', body: JSON.stringify({ kind, keys, decision }),
      });
    }
    showToast(decision === 'clear' ? t('Decision cleared') : t('Marked {decision} fleet-wide', { decision }), 'success');
    updState.selected.clear();
    fetchUpdates();
  } catch (e) {
    showToast(e.message || t('Could not save the decision'), 'error');
  }
}

/* ── Monitor page: Software section ──────────────────────────────────── */
async function renderHostSoftware(hostId) {
  const wrap = document.getElementById('software-list');
  if (!wrap) return;
  const seq = ++appsState.monitorSeq;
  wrap.innerHTML = loadingHtml();
  try {
    const body = await apiJson(`/api/v1/software/hosts/${encodeURIComponent(hostId)}/`);
    if (seq !== appsState.monitorSeq) return;
    appsState.monitorItems = body.items || [];
    _softwareShowAll = false;   // a newly selected host starts collapsed
    appsState.monitorSnapshot = body.snapshot || null;
  } catch (e) {
    if (seq !== appsState.monitorSeq) return;
    showLoadFailure(wrap, t('this host’s software'), e, () => renderHostSoftware(hostId));
    return;
  }
  renderHostSoftwareList();
}

function renderHostSoftwareList() {
  const wrap = document.getElementById('software-list');
  const countEl = document.getElementById('software-count');
  if (!wrap) return;

  const snapshot = appsState.monitorSnapshot;
  if (!snapshot) {
    if (countEl) countEl.textContent = '';
    wrap.innerHTML = `<div class="apps-empty-state">${escHtml(t('No software reported for this host yet.'))}</div>`;
    return;
  }

  const items = appsState.monitorItems;
  const outdated = items.filter(i => i.outdated).length;
  const unmanaged = items.filter(i => !i.managed).length;
  if (countEl) {
    countEl.textContent = t('{items} items · {outdated} outdated · {unmanaged} unmanaged', { items: snapshot.item_count, outdated, unmanaged })
      + ' · ' + t('reported {when}', { when: timeAgo(snapshot.received_at) || t('unknown') });
  }

  const filter = (/** @type {HTMLInputElement} */ (document.getElementById('software-q'))?.value || '').trim().toLowerCase();
  const matched = (filter
    ? items.filter(i => (i.name || '').toLowerCase().includes(filter)
        || (i.package_id || '').toLowerCase().includes(filter))
    : items.slice())
    // What needs attention first: outdated, then unmanaged, then the rest by name.
    .sort((a, b) => (b.outdated - a.outdated) || (a.managed - b.managed)
      || (a.name || '').localeCompare(b.name || ''));
  // A Linux host lists ~1500 packages: show the first 100 unless asked.
  const shown = (_softwareShowAll || filter) ? matched : matched.slice(0, SOFTWARE_ROWS);
  const more = matched.length - shown.length;

  const rows = shown.map(item => `<tr>
    <td>${escHtml(item.name)}${item.managed ? '' : ` <span class="chip apps-chip-warn">${escHtml(t('unmanaged'))}</span>`}</td>
    <td class="apps-mono">${escHtml(item.version)}</td>
    <td>${item.outdated ? `<span class="chip chip-rose">${escHtml(item.latest_version)}</span>` : '<span class="apps-zero">—</span>'}</td>
    <td><span class="chip chip-muted apps-src">${escHtml(item.source)}</span></td>
    <td>${_scopeCell(item)}</td>
    <td>${escHtml(item.publisher || '')}</td>
  </tr>`).join('');

  wrap.innerHTML = `<div class="apps-snapshot-line">${_errorChips(snapshot.errors)}</div>
    <div class="table-wrap">
      <table class="hunt-table apps-table">
        <thead><tr>
          <th>${escHtml(t('Name'))}</th><th>${escHtml(t('Version'))}</th><th>${escHtml(t('Latest'))}</th><th>${escHtml(t('Source'))}</th><th>${escHtml(t('Scope'))}</th><th>${escHtml(t('Publisher'))}</th>
        </tr></thead>
        <tbody>${rows || _emptyRow(t('No software matches this filter.'))}</tbody>
      </table>
    </div>
    ${more > 0 ? `<button type="button" class="btn btn-ghost btn-xs apps-show-all" id="software-show-all">${escHtml(t('Show all {n}', { n: matched.length }))}</button>` : ''}`;
  const showAll = document.getElementById('software-show-all');
  if (showAll) showAll.addEventListener('click', () => { _softwareShowAll = true; renderHostSoftwareList(); });
}

const SOFTWARE_ROWS = 100;
let _softwareShowAll = false;

// An expanded "By host" row shows this many packages; a Linux host reports ~1500.
const APPS_HOST_DETAIL_LIMIT = 50;

/* ── Wiring ──────────────────────────────────────────────────────────── */
let _appsSearchTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  const q = document.getElementById('apps-q');
  if (q) {
    q.addEventListener('input', () => {
      clearTimeout(_appsSearchTimer);
      _appsSearchTimer = setTimeout(() => fetchApps(true), APPS_DEBOUNCE_MS);
    });
  }
  for (const id of ['apps-outdated', 'apps-unmanaged', 'apps-source']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => fetchApps(true));
  }
  const updQ = document.getElementById('upd-q');
  if (updQ) {
    updQ.addEventListener('input', () => {
      clearTimeout(_appsSearchTimer);
      _appsSearchTimer = setTimeout(fetchUpdates, APPS_DEBOUNCE_MS);
    });
  }
  for (const id of ['upd-kind', 'upd-decision']) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', fetchUpdates);
  }
  const updAll = /** @type {HTMLInputElement} */ (document.getElementById('upd-all'));
  if (updAll) {
    updAll.addEventListener('change', () => {
      updState.selected = new Set(updAll.checked ? updState.rows.map(_updKey) : []);
      renderUpdatesTable();
    });
  }
  const softwareQ = document.getElementById('software-q');
  if (softwareQ) softwareQ.addEventListener('input', renderHostSoftwareList);
});

delegateClick('[data-app-row]', (el, ev) => {
  if (ev.target.closest('a, button')) return;
  toggleAppDetail(el.dataset.appRow);
});

/** The package ids a policy rule can name for one app, from its per-host rows:
 *  one (package id, source) per package manager it comes from. Homebrew,
 *  Windows Update and unmanaged installs cannot be a policy rule. */
async function _appPolicyPairs(key) {
  let rows = appsState.details[key];
  if (!rows) {
    rows = (await apiJson(`/api/v1/software/apps/${encodeURIComponent(key)}/`)).rows || [];
    appsState.details[key] = rows;
  }
  const seen = new Set();
  const pairs = [];
  for (const r of rows) {
    if (!r.managed || !POLICY_APP_SOURCES.includes(r.source)) continue;
    const id = `${r.source}\u0000${r.package_id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    pairs.push({ app: r.package_id, source: r.source });
  }
  return { pairs, sources: [...new Set(rows.map(r => r.source))] };
}

//: apps/tasks/spec.py _APP_SOURCE_CHOICES — what an app rule may name.
const POLICY_APP_SOURCES = ['dpkg', 'rpm', 'apk', 'pacman', 'flatpak', 'snap',
  'winget', 'chocolatey', 'scoop', 'registry'];

delegateClick('[data-app-policy]', async (el) => {
  const key = el.dataset.appPolicy;
  const row = appsState.rows.find(r => r.name_key === key);
  const name = row ? row.name : key;
  let found;
  try { found = await _appPolicyPairs(key); } catch (e) { showToast(e.message, 'error'); return; }
  if (!found.pairs.length) {
    showToast(t('A policy cannot manage {name}: it comes from {sources}', { name, sources: found.sources.join(', ') || t('no package manager') }), 'error');
    return;
  }
  policyFromApp(name, found.pairs);
});

delegateClick('[data-upd-pick]', (el) => {
  if (el.checked) updState.selected.add(el.dataset.updPick);
  else updState.selected.delete(el.dataset.updPick);
  _renderUpdateBulk();
});

delegateClick('[data-upd-row]', (el, ev) => {
  if (ev.target.closest('a, input, button')) return;
  toggleUpdateHosts(el.dataset.updRow);
});

delegateClick('[data-upd-decide]', (el) => decideUpdates(el.dataset.updDecide));

/** "Policy…" from an update row: a Linux package becomes an app rule for its
 *  package manager; a Windows update is not an app, so what it teaches the
 *  policy is which Windows Update classification to fetch. */
delegateClick('[data-upd-policy]', (el) => {
  const row = updState.rows.find(r => _updKey(r) === el.dataset.updPolicy);
  if (row) _updPolicy([row]);
});

delegateClick('[data-upd-policy-selected]', () => {
  const rows = updState.rows.filter(r => updState.selected.has(_updKey(r)));
  if (!rows.length) return;
  _updPolicy(rows);
});

function _updPolicy(rows) {
  const pairs = [];
  const classifications = new Set();
  for (const row of rows) {
    if (row.kind === 'linux' && POLICY_APP_SOURCES.includes(row.classification)) {
      pairs.push({ app: row.key, source: row.classification });
    } else if (row.kind === 'windows' && row.classification) {
      classifications.add(row.classification);
    }
  }
  const label = rows.length === 1 ? rows[0].key : tn('{n} update', '{n} updates', rows.length);
  if (!pairs.length && !classifications.size) {
    const first = rows[0];
    showToast(t('A policy cannot manage {name}: it comes from {source}', { name: first.key, source: first.classification || t('no package manager') }), 'error');
    return;
  }
  policyFromApp(label, pairs, { windowsClassifications: [...classifications] });
}

delegateClick('[data-app-host-row]', (el, ev) => {
  if (ev.target.closest('a, button')) return;
  toggleHostDetail(el.dataset.appHostRow);
});

delegateClick('[data-host-show-all]', (el) => {
  appsState.hostShowAll[el.dataset.hostShowAll] = true;
  renderAppsHostsTable();
});

delegateClick('[data-app-host]', (el, ev) => {
  ev.preventDefault();
  navigateTo('monitor');
  if (typeof selectMonitorHost === 'function') selectMonitorHost(el.dataset.appHost);
});

// The Apps page loads its data the first time it is shown, so an unvisited
// page costs no queries. "By host" answers even when nothing is installed —
// that is the empty state an operator needs to see — so a rows-only guard
// would leave the page blank forever.
const _origNavForApps = navigateTo;
window.navigateTo = function(pageName) {
  _origNavForApps(pageName);
  if (pageName === 'apps') {
    fetchApps(true);
    fetchAppHosts();
    fetchUpdates();
  }
};
