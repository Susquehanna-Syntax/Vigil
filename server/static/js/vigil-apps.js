// vigil-apps.js
// Owns: the Apps page (installed software by app / by host) and the Software
//   section on the Monitor page.
// HTML: templates/pages/_apps.html, Software section in templates/pages/_monitor.html
// Depends on: vigil-utils.js (escHtml, escAttr, timeAgo, delegateClick),
//   vigil-nav.js (navigateTo — wrapped below), vigil-monitor.js
//   (selectMonitorHost, reached from a hostname link).
// API: GET /api/v1/software/apps/, /api/v1/software/apps/<name_key>/,
//      /api/v1/software/hosts/, /api/v1/software/hosts/<id>/,
//      /api/v1/software/updates/, /api/v1/software/updates/hosts/;
//      POST /api/v1/software/updates/decide/ (admin)

const APPS_PAGE_SIZE = 100;
const APPS_DEBOUNCE_MS = 250;
const APPS_COLUMNS = 6;
const APPS_EMPTY_TEXT = 'No software reported yet — agents send their list within a few '
  + 'minutes of starting, or run the "Refresh software inventory" task.';

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
  monitorItems: [],
  monitorSnapshot: null,
  monitorSeq: 0,
};

/* ── Shared fragments ────────────────────────────────────────────────── */
function _loadingRow() {
  return `<tr><td colspan="${APPS_COLUMNS}" class="apps-detail-loading">Loading…</td></tr>`;
}

function _emptyRow(text) {
  return `<tr><td colspan="${APPS_COLUMNS}" class="apps-empty-state">${escHtml(text)}</td></tr>`;
}

function _countChip(value, cls) {
  return value ? `<span class="chip ${cls}">${value}</span>` : '<span class="apps-zero">0</span>';
}

function _sourceChips(sources) {
  if (!sources || !sources.length) return '<span class="apps-zero">—</span>';
  return sources.map(s => `<span class="chip chip-muted apps-src">${escHtml(s)}</span>`).join('');
}

// "user: alice" for a user-scoped package, the scope name otherwise.
function _scopeCell(row) {
  return row.scope === 'user'
    ? `user: ${escHtml(row.user || 'unknown')}` : escHtml(row.scope || '');
}

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
function _appsParams(offset) {
  const params = new URLSearchParams();
  const q = document.getElementById('apps-q');
  if (q && q.value.trim()) params.set('q', q.value.trim());
  if (document.getElementById('apps-outdated')?.checked) params.set('outdated', '1');
  if (document.getElementById('apps-unmanaged')?.checked) params.set('unmanaged', '1');
  const source = document.getElementById('apps-source');
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
  let body = { count: 0, results: [] };
  try {
    const resp = await fetch(`/api/v1/software/apps/?${params}`, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    body = await resp.json();
  } catch {
    if (seq === appsState.seq) body = { count: 0, results: [] };
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
    </tr>`;
    if (appsState.expanded[key]) html += _appDetailHtml(key);
  }
  if (!html) {
    html = _emptyRow(hasAnyHostSoftware ? 'No apps match these filters.' : APPS_EMPTY_TEXT);
  }
  tbody.innerHTML = html;
  if (more) more.hidden = !(appsState.rows.length > 0 && appsState.rows.length < appsState.count);
}

function _appDetailHtml(key) {
  const rows = appsState.details[key];
  if (!rows) {
    return `<tr class="apps-detail-row"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail-loading">Loading hosts…</div></td></tr>`;
  }
  if (!rows.length) {
    return `<tr class="apps-detail-row"><td colspan="${APPS_COLUMNS}">
      <div class="apps-detail-loading">No hosts report this app in your scope.</div></td></tr>`;
  }
  const items = rows.map(row => {
    const latest = row.outdated
      ? `<span class="chip chip-rose" title="Latest version">${escHtml(row.latest_version)}</span>` : '';
    const unmanaged = row.managed ? '' : '<span class="chip apps-chip-warn">unmanaged</span>';
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
  return `<tr class="apps-detail-row"><td colspan="${APPS_COLUMNS}">
    <div class="apps-detail">${items}</div></td></tr>`;
}

async function toggleAppDetail(key) {
  if (appsState.expanded[key]) {
    delete appsState.expanded[key];
    renderAppsTable();
    return;
  }
  appsState.expanded[key] = true;
  renderAppsTable();
  if (appsState.details[key] !== undefined) return;
  appsState.details[key] = null;
  let rows = [];
  try {
    const resp = await fetch(`/api/v1/software/apps/${encodeURIComponent(key)}/`, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    rows = (await resp.json()).rows || [];
  } catch { rows = []; }
  appsState.details[key] = rows;
  if (appsState.expanded[key]) renderAppsTable();
}

/* ── By host ─────────────────────────────────────────────────────────── */
async function fetchAppHosts() {
  const seq = ++appsState.hostSeq;
  const tbody = document.getElementById('apps-hosts-body');
  if (tbody) tbody.innerHTML = _loadingRow();
  let body = { results: [] };
  try {
    const resp = await fetch('/api/v1/software/hosts/', { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    body = await resp.json();
  } catch { body = { results: [] }; }
  if (seq !== appsState.hostSeq) return;
  appsState.hosts = body.results || [];
  renderAppsHostsTable();
}

function renderAppsHostsTable() {
  const tbody = document.getElementById('apps-hosts-body');
  if (!tbody) return;
  const html = appsState.hosts.map(row => `<tr>
    <td><a class="apps-host-link" href="#" data-app-host="${escAttr(row.host_id)}">${escHtml(row.hostname)}</a></td>
    <td class="num">${row.item_count}</td>
    <td class="num">${_countChip(row.outdated, 'chip-rose')}</td>
    <td class="num">${_countChip(row.unmanaged, 'apps-chip-warn')}</td>
    <td>${escHtml(timeAgo(row.received_at) || 'never')}</td>
    <td>${_errorChips(row.errors) || '<span class="apps-zero">—</span>'}</td>
  </tr>`).join('');
  tbody.innerHTML = html || _emptyRow('No hosts in your scope.');
  renderAppsTable();
}

function refreshApps() {
  fetchApps(true);
  fetchAppHosts();
  fetchUpdates();
}

function loadMoreApps() { fetchApps(false); }

/* ── By update (M8) ──────────────────────────────────────────────────── */
const UPDATE_COLUMNS = 6;
const updState = { rows: [], seq: 0, selected: new Set(), details: {}, expanded: {} };

function _updKey(row) { return `${row.kind}|${row.key}`; }

function _severityChip(row) {
  const sev = row.severity || (row.kind === 'linux' ? '' : 'unrated');
  if (!sev) return '<span class="apps-zero">—</span>';
  const cls = sev === 'critical' ? 'chip-rose' : sev === 'important' ? 'apps-chip-warn' : 'chip-muted';
  return `<span class="chip ${cls}">${escHtml(sev)}</span>`;
}

function _decisionChip(decision) {
  if (decision === 'approved') return '<span class="chip chip-mint">approved</span>';
  if (decision === 'declined') return '<span class="chip chip-rose">declined</span>';
  return '<span class="apps-zero">undecided</span>';
}

function _ageCell(days) {
  const text = days === 1 ? '1 day' : `${days} days`;
  return days > 30 ? `<span class="chip apps-chip-warn">${text}</span>` : text;
}

async function fetchUpdates() {
  const seq = ++updState.seq;
  const tbody = document.getElementById('apps-updates-body');
  if (tbody) tbody.innerHTML = `<tr><td colspan="${UPDATE_COLUMNS}" class="apps-detail-loading">Loading…</td></tr>`;
  const params = new URLSearchParams();
  for (const [id, name] of [['upd-q', 'q'], ['upd-kind', 'kind'], ['upd-decision', 'decision']]) {
    const value = (document.getElementById(id)?.value || '').trim();
    if (value) params.set(name, value);
  }
  let body = { results: [] };
  try {
    const resp = await fetch(`/api/v1/software/updates/?${params}`, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    body = await resp.json();
  } catch { body = { results: [] }; }
  if (seq !== updState.seq) return;
  updState.rows = body.results || [];
  const keys = new Set(updState.rows.map(_updKey));
  for (const key of [...updState.selected]) if (!keys.has(key)) updState.selected.delete(key);
  renderUpdatesTable();
}

function renderUpdatesTable() {
  const tbody = document.getElementById('apps-updates-body');
  if (!tbody) return;
  const html = updState.rows.map(row => {
    const key = _updKey(row);
    const checked = updState.selected.has(key) ? ' checked' : '';
    let out = `<tr class="apps-row" data-upd-row="${escAttr(key)}">
      <td class="upd-check"><input type="checkbox" data-upd-pick="${escAttr(key)}"${checked} aria-label="Select ${escAttr(row.key)}"></td>
      <td><div class="apps-name">${escHtml(row.key)}</div>
        <div class="upd-title">${escHtml(row.title || '')}${row.classification ? ' · ' + escHtml(row.classification) : ''}</div></td>
      <td>${_severityChip(row)}</td>
      <td class="num">${row.hosts}</td>
      <td class="num">${_ageCell(row.age_days)}</td>
      <td>${_decisionChip(row.decision)}</td>
    </tr>`;
    if (updState.expanded[key]) {
      const hosts = updState.details[key];
      const inner = hosts == null
        ? '<div class="apps-detail-loading">Loading…</div>'
        : hosts.map(h => `<div class="apps-detail-item">
            <a class="apps-host-link" href="#" data-app-host="${escAttr(h.host_id)}">${escHtml(h.hostname)}</a>
            <span class="apps-detail-scope">missing for ${h.age_days} day${h.age_days === 1 ? '' : 's'}</span>
            ${h.version ? `<span class="apps-detail-ver">→ ${escHtml(h.version)}</span>` : ''}
          </div>`).join('') || '<div class="apps-detail-loading">No hosts in your scope.</div>';
      out += `<tr class="apps-detail-row"><td colspan="${UPDATE_COLUMNS}"><div class="apps-detail">${inner}</div></td></tr>`;
    }
    return out;
  }).join('');
  tbody.innerHTML = html || `<tr><td colspan="${UPDATE_COLUMNS}" class="apps-empty-state">${escHtml(
    'No pending updates reported. Windows hosts send their list after each update scan; Linux hosts after each software inventory.')}</td></tr>`;
  _renderUpdateBulk();
}

function _renderUpdateBulk() {
  const bar = document.getElementById('upd-bulk');
  const count = updState.selected.size;
  if (bar) bar.hidden = count === 0;
  const label = document.getElementById('upd-bulk-count');
  if (label) label.textContent = `${count} selected`;
  const all = document.getElementById('upd-all');
  if (all) all.checked = count > 0 && count === updState.rows.length;
}

async function toggleUpdateHosts(key) {
  updState.expanded[key] = !updState.expanded[key];
  if (updState.expanded[key] && updState.details[key] === undefined) {
    updState.details[key] = null;
    renderUpdatesTable();
    const [kind, ...rest] = key.split('|');
    const params = new URLSearchParams({ kind, key: rest.join('|') });
    try {
      const body = await apiJson(`/api/v1/software/updates/hosts/?${params}`);
      updState.details[key] = body.results || [];
    } catch { updState.details[key] = []; }
  }
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
    showToast(decision === 'clear' ? 'Decision cleared' : `Marked ${decision} fleet-wide`, 'success');
    updState.selected.clear();
    fetchUpdates();
  } catch (e) {
    showToast(e.message || 'Could not save the decision', 'error');
  }
}

/* ── Monitor page: Software section ──────────────────────────────────── */
async function renderHostSoftware(hostId) {
  const wrap = document.getElementById('software-list');
  if (!wrap) return;
  const seq = ++appsState.monitorSeq;
  wrap.innerHTML = '<div class="apps-empty-state">Loading…</div>';
  let body = { snapshot: null, items: [] };
  try {
    const resp = await fetch(`/api/v1/software/hosts/${encodeURIComponent(hostId)}/`, { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    body = await resp.json();
  } catch { body = { snapshot: null, items: [] }; }
  if (seq !== appsState.monitorSeq) return;
  appsState.monitorItems = body.items || [];
  _softwareShowAll = false;   // a newly selected host starts collapsed
  appsState.monitorSnapshot = body.snapshot || null;
  renderHostSoftwareList();
}

function renderHostSoftwareList() {
  const wrap = document.getElementById('software-list');
  const countEl = document.getElementById('software-count');
  if (!wrap) return;

  const snapshot = appsState.monitorSnapshot;
  if (!snapshot) {
    if (countEl) countEl.textContent = '';
    wrap.innerHTML = '<div class="apps-empty-state">No software reported for this host yet.</div>';
    return;
  }

  const items = appsState.monitorItems;
  const outdated = items.filter(i => i.outdated).length;
  const unmanaged = items.filter(i => !i.managed).length;
  if (countEl) {
    countEl.textContent = `${snapshot.item_count} items · ${outdated} outdated · ${unmanaged} unmanaged`
      + ` · reported ${timeAgo(snapshot.received_at) || 'unknown'}`;
  }

  const filter = (document.getElementById('software-q')?.value || '').trim().toLowerCase();
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
    <td>${escHtml(item.name)}${item.managed ? '' : ' <span class="chip apps-chip-warn">unmanaged</span>'}</td>
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
          <th>Name</th><th>Version</th><th>Latest</th><th>Source</th><th>Scope</th><th>Publisher</th>
        </tr></thead>
        <tbody>${rows || _emptyRow('No software matches this filter.')}</tbody>
      </table>
    </div>
    ${more > 0 ? `<button type="button" class="btn btn-ghost btn-xs apps-show-all" id="software-show-all">Show all ${matched.length}</button>` : ''}`;
  const showAll = document.getElementById('software-show-all');
  if (showAll) showAll.addEventListener('click', () => { _softwareShowAll = true; renderHostSoftwareList(); });
}

const SOFTWARE_ROWS = 100;
let _softwareShowAll = false;

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
  const updAll = document.getElementById('upd-all');
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
  if (ev.target.closest('a')) return;
  toggleAppDetail(el.dataset.appRow);
});

delegateClick('[data-upd-pick]', (el) => {
  if (el.checked) updState.selected.add(el.dataset.updPick);
  else updState.selected.delete(el.dataset.updPick);
  _renderUpdateBulk();
});

delegateClick('[data-upd-row]', (el, ev) => {
  if (ev.target.closest('a, input')) return;
  toggleUpdateHosts(el.dataset.updRow);
});

delegateClick('[data-upd-decide]', (el) => decideUpdates(el.dataset.updDecide));

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
navigateTo = function(pageName) {
  _origNavForApps(pageName);
  if (pageName === 'apps') {
    fetchApps(true);
    fetchAppHosts();
    fetchUpdates();
  }
};
