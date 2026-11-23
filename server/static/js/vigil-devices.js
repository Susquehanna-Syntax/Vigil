// vigil-devices.js — Unmanaged device inventory (manual registry on the Inventory page).
// Depends on: vigil-utils.js (apiJson, showToast, escHtml, escAttr, timeAgo,
//   confirmModal, delegateClick, tok), vigil-nav.js (navigateTo),
//   Chart.js for a check's latency.
// API: GET/POST /api/v1/hosts/devices/, GET/PATCH/DELETE .../devices/<id>/,
//      GET/POST /api/v1/hosts/devices/<id>/checks/, PATCH/DELETE .../checks/<check_id>/,
//      GET .../checks/<check_id>/results/?hours=24

let _devices = [];
// Which devices this user may write checks for, from the list's can_edit_checks
// — admin role AND the device in their write scope, which is exactly what the
// writes refuse. The editor offers only what is in here.
let _editableDevices = new Set();

function _canEditChecks(deviceId) {
  return deviceId !== null && deviceId !== undefined && _editableDevices.has(String(deviceId));
}

async function loadDevices() {
  let rows;
  try {
    rows = await apiJson('/api/v1/hosts/devices/');
  } catch (e) {
    rows = [];
  }
  _devices = Array.isArray(rows) ? rows : [];
  _editableDevices = new Set(_devices.filter((d) => d && d.can_edit_checks).map((d) => String(d.id)));
  renderDevices();
}

function renderDevices() {
  const wrap = document.getElementById('dev-content');
  const countEl = document.getElementById('dev-count');
  if (!wrap) return;

  if (!_devices.length) {
    if (countEl) countEl.textContent = '';
    wrap.innerHTML = '<div class="dev-empty">No unmanaged devices yet. Add routers, switches, '
      + 'printers and other gear to keep a full picture of the network.</div>';
    return;
  }
  if (countEl) countEl.textContent = _devices.length === 1 ? '1 device' : `${_devices.length} devices`;

  let html = `<div class="dev-card"><table class="ctr-table dev-table">
    <thead><tr>
      <th>Name</th><th>Type</th><th>IP Address</th><th>MAC</th><th>Serial</th>
      <th>Vendor</th><th>Location</th><th>Owner</th><th class="num"></th>
    </tr></thead><tbody>`;
  for (const d of _devices) {
    // The dot comes from check_state, which the list query computes for every
    // device at once — one request per device would be a dozen for a page.
    const dot = d.check_state === 'up' ? '<span class="dev-state-dot dev-state-dot--up"></span>'
      : d.check_state === 'down' ? '<span class="dev-state-dot dev-state-dot--down"></span>' : '';
    html += `<tr>
      <td class="ctr-name">${escHtml(d.name || '')}${dot}</td>
      <td>${escHtml(d.device_type_label || '')}</td>
      <td class="dev-mono">${escHtml(d.ip_address || '—')}</td>
      <td class="dev-mono">${escHtml(d.mac_address || '—')}</td>
      <td class="dev-mono">${escHtml(d.serial || '—')}</td>
      <td>${escHtml(d.vendor || '—')}</td>
      <td>${escHtml(d.location || '—')}</td>
      <td>${escHtml(d.owner || '—')}</td>
      <td class="num"><button class="btn btn-ghost btn-sm" data-device-edit="${escAttr(d.id)}">Edit</button></td>
    </tr>`;
  }
  html += `</tbody></table></div>`;
  wrap.innerHTML = html;
}

function openDeviceModal(id) {
  const d = id ? _devices.find(x => x.id === id) : null;
  document.getElementById('device-modal-title').textContent = d ? 'Edit Device' : 'Add Device';
  document.getElementById('device-id').value = d ? d.id : '';
  document.getElementById('device-name').value = d ? (d.name || '') : '';
  sitePickFill(document.getElementById('device-site'), document.getElementById('device-site-field'),
    d ? d.site_id : null, { editing: !!d });
  document.getElementById('device-type').value = d ? (d.device_type || 'other') : 'other';
  document.getElementById('device-ip').value = d ? (d.ip_address || '') : '';
  document.getElementById('device-mac').value = d ? (d.mac_address || '') : '';
  document.getElementById('device-vendor').value = d ? (d.vendor || '') : '';
  document.getElementById('device-location').value = d ? (d.location || '') : '';
  document.getElementById('device-serial').value = d ? (d.serial || '') : '';
  document.getElementById('device-warranty').value = d ? (d.warranty_end || '') : '';
  document.getElementById('device-owner').value = d ? (d.owner || '') : '';
  document.getElementById('device-extra').value = d ? _extraToLines(d.extra) : '';
  document.getElementById('device-notes').value = d ? (d.notes || '') : '';
  document.getElementById('device-delete-btn').style.display = d ? 'inline-flex' : 'none';
  document.getElementById('device-overlay').classList.add('open');
  document.getElementById('device-modal').classList.add('open');
  openDeviceChecks(d);
}

function closeDeviceModal() {
  collapseDeviceCheckChart();
  document.getElementById('device-overlay').classList.remove('open');
  document.getElementById('device-modal').classList.remove('open');
}

/* ── Checks: what a poller reports about this device (S9) ─────────────────── */

const DEVICE_CHECK_KINDS = { icmp: 'Ping', tcp: 'TCP', http: 'HTTP', dns: 'DNS', snmp: 'SNMP', ssh: 'SSH backup' };
const NO_POLLER_HINT = 'No results yet: is a poller assigned? See Settings > Agents for hosts with poller: true';

let _checkDeviceId = null;
// One expanded row at a time, and one Chart.js instance to go with it.
let _openCheckId = null;
let _checkChart = null;
// The element the rows and their chart hosts live under — #device-checks-list.
let _chartHost = null;
// The checks the last loadDeviceChecks drew, by id: an expanded row needs its
// check to know its kind and its latest result's values.
const _loadedChecks = new Map();
let _checkValues = null;
// An SNMP check that walks IF-MIB shows its interface table under the values,
// and one interface's traffic under that table. `_checkInterfaces` is the one
// element holding both (or the note said instead), so a collapse takes it whole.
let _checkInterfaces = null;
let _interfaceChart = null;

function _deviceEl(id) {
  return document.getElementById(id);
}

function _checkPath(checkId) {
  return `/api/v1/hosts/devices/${_checkDeviceId}/checks/${checkId}/`;
}

/** The Checks section follows the device the modal is showing: hidden for a
 *  new one, which has no id to hang a check on, filled in for an existing one. */
function openDeviceChecks(device) {
  const section = _deviceEl('device-checks');
  if (!section) return;
  collapseDeviceCheckChart();
  _checkDeviceId = device ? device.id : null;
  section.hidden = !device;
  const add = _deviceEl('device-check-add');
  if (add) add.hidden = !(device && _canEditChecks(device.id));
  if (device) {
    setDeviceCheckKind(_deviceEl('device-check-kind'));
    loadDeviceChecks(device.id);
    _applyConfigBackupLicense();
  } else if (_deviceEl('device-checks-list')) {
    _deviceEl('device-checks-list').replaceChildren();
  }
}

/** A check's target as an operator wrote it, so the row says what is polled. */
function _checkLabel(check) {
  const p = check.params || {};
  let target = '';
  // SSH is labelled by the user we log in as plus the host, not the bare
  // address: an operator installing the key wants to see both.
  if (check.kind === 'ssh') {
    const port = (p.port !== undefined && p.port !== null && p.port !== '' && Number(p.port) !== 22)
      ? `:${p.port}` : '';
    return `${DEVICE_CHECK_KINDS.ssh} ${String(p.username || '').trim()}@${String(p.host || '').trim()}${port}`;
  }
  if (check.kind === 'tcp') target = [p.host, p.port].filter(x => x !== undefined && x !== null && x !== '').join(':');
  else if (check.kind === 'http') target = p.url || '';
  else if (check.kind === 'dns') target = p.name || '';
  // SNMP defaults to 161 and the server stores no port for it, so a stored port
  // is the exception worth naming.
  else if (check.kind === 'snmp') target = [p.host, p.port].filter(x => x !== undefined && x !== null && x !== '').join(':');
  else target = p.host || '';
  // A v3 check says so, and whether it is encrypted: that is what an operator
  // checks first when a device refuses it.
  const kind = check.kind === 'snmp' && p.version === '3'
    ? `SNMPv3${p.priv === false ? ' (not encrypted)' : ''}`
    : (DEVICE_CHECK_KINDS[check.kind] || check.kind);
  return `${kind}${target ? ` ${target}` : ''}`;
}

function _ms(value) {
  return (value === null || value === undefined) ? '-' : `${Number(value).toFixed(1)} ms`;
}

function _checkRow(check) {
  const row = document.createElement('div');
  row.className = 'device-check-row';
  row.dataset.deviceCheckToggle = String(check.id);
  row.title = 'Show the last 24 hours';

  const dot = document.createElement('span');
  dot.className = 'device-check-dot' + (check.latest
    ? (check.latest.ok ? ' device-check-dot--ok' : ' device-check-dot--bad')
    : ' device-check-dot--none');
  row.appendChild(dot);

  // Every piece of it — target, detail, hostname, URL — is device- or
  // operator-supplied, so textContent only: a detail of "<img src=x onerror=…>"
  // from a poller must land as the text it is.
  const label = document.createElement('span');
  label.className = 'device-check-label';
  label.textContent = _checkLabel(check) + (check.enabled ? '' : ' (off)');
  row.appendChild(label);

  const latency = document.createElement('span');
  latency.className = 'device-check-latency';
  latency.textContent = _ms(check.latest ? check.latest.latency_ms : null);
  row.appendChild(latency);

  const detail = document.createElement('span');
  detail.className = 'device-check-detail';
  detail.textContent = (check.latest && check.latest.detail) || '';
  row.appendChild(detail);

  const age = document.createElement('span');
  age.className = 'device-check-age';
  age.textContent = check.latest ? timeAgo(check.latest.time) : '';
  row.appendChild(age);

  if (_canEditChecks(_checkDeviceId)) {
    const actions = document.createElement('span');
    actions.className = 'device-check-actions';
    // The shared switch, like the settings panes: aria-checked is the state and
    // the label says what a click will do.
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'toggle device-check-toggle';
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', String(!!check.enabled));
    toggle.setAttribute('aria-label', check.enabled ? 'Disable check' : 'Enable check');
    toggle.title = check.enabled ? 'Enabled — click to stop polling it' : 'Disabled — click to poll it again';
    toggle.dataset.act = 'toggleDeviceCheck';
    toggle.dataset.a1 = String(check.id);
    toggle.dataset.a2 = check.enabled ? '$false' : '$true';
    actions.appendChild(toggle);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn-ghost btn-sm device-check-delete';
    del.textContent = 'Delete';
    del.dataset.act = 'deleteDeviceCheck';
    del.dataset.a1 = String(check.id);
    actions.appendChild(del);
    row.appendChild(actions);
  }

  const chart = document.createElement('div');
  chart.className = 'device-check-chart';
  chart.hidden = true;
  return { row, chart };
}

/** Line-oriented "label = OID" editor for an SNMP check. The server validates
 *  labels and OIDs and answers with one sentence of its own, so this only splits. */
function parseSnmpOids(text) {
  const oids = [];
  const lines = String(text || '').split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || line.startsWith('#')) continue;
    const at = line.indexOf('=');
    // Only the first '=' splits; anything after it belongs to the OID.
    if (at < 0) return { oids: [], error: `Line ${i + 1}: write label = OID` };
    oids.push({ label: line.slice(0, at).trim(), oid: line.slice(at + 1).trim() });
  }
  return { oids, error: null };
}

function _snmpUptime(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return '-';
  const parts = [Math.floor(s / 86400), Math.floor(s / 3600) % 24, Math.floor(s / 60) % 60];
  const units = ['d', 'h', 'm'];
  const firstKept = parts.findIndex((n) => n > 0);
  if (firstKept < 0) return '0m';
  // From the largest non-zero unit down: 3d 4h 5m, 4h 5m, 5m; never 0d 0h 5m.
  const kept = parts.slice(firstKept);
  return kept.map((n, i) => `${n}${units[firstKept + i]}`).join(' ');
}

/** One <dt>/<dd> pair per value of an SNMP result, Name and Uptime first. Only
 *  ever with textContent: every one of these values comes back from a device. */
function _snmpValuesList(extra) {
  const ORDER = ['sys_name', 'uptime_s'];
  const NAMES = { sys_name: 'Name', uptime_s: 'Uptime' };
  // 'kind' and 'target' repeat what the row itself already says.
  const keys = Object.keys(extra || {}).filter((k) => k !== 'kind' && k !== 'target');
  const ordered = ORDER.filter((k) => keys.includes(k)).concat(keys.filter((k) => !ORDER.includes(k)));
  const dl = document.createElement('dl');
  dl.className = 'device-check-values';
  for (const key of ordered) {
    const value = extra[key];
    const dt = document.createElement('dt');
    dt.textContent = NAMES[key] || key;
    const dd = document.createElement('dd');
    dd.textContent = key === 'uptime_s' ? _snmpUptime(value)
      : (value === null || value === undefined ? '-' : String(value));
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  return dl;
}

async function loadDeviceChecks(deviceId) {
  const list = _deviceEl('device-checks-list');
  if (!list) return;
  _checkDeviceId = deviceId;
  _openCheckId = null;
  _chartHost = list;
  list.replaceChildren();
  let checks = [];
  try {
    checks = await apiJson(`/api/v1/hosts/devices/${deviceId}/checks/`);
  } catch (e) {
    const note = document.createElement('div');
    note.className = 'device-check-note';
    note.textContent = e.message || 'Could not load checks';
    list.appendChild(note);
    return;
  }
  if (!Array.isArray(checks) || !checks.length) {
    const note = document.createElement('div');
    note.className = 'device-check-note';
    note.textContent = 'No checks yet. A check is run by a poller at this device\'s site.';
    list.appendChild(note);
    return;
  }
  for (const check of checks) {
    const built = _checkRow(check);
    list.appendChild(built.row);
    list.appendChild(built.chart);
  }
  // The checks this answer drew, by id: an expanded row reads its own back out
  // to know its kind and the values of its newest result.
  _loadedChecks.clear();
  for (const check of checks) _loadedChecks.set(String(check.id), check);
}

function collapseDeviceCheckChart() {
  if (_checkChart) { _checkChart.destroy(); _checkChart = null; }
  // An SNMP row's values are drawn under the chart, in the same container, so
  // they go whenever the chart goes.
  if (_checkValues) { _checkValues.remove(); _checkValues = null; }
  // The interface table, and any traffic chart under it, go the same way. The
  // chart is destroyed before its canvas leaves the page.
  if (_interfaceChart) { _interfaceChart.destroy(); _interfaceChart = null; }
  if (_checkInterfaces) { _checkInterfaces.remove(); _checkInterfaces = null; }
  // An SSH row's key and backup panel goes with it too, and the check it was
  // drawn for is forgotten: an answer still on its way must not paint a panel
  // that no longer has a place on the page.
  _collapseSshPanel();
  // The host we last rendered into, tracked here rather than queried: the rows
  // are replaced on every reload and there is exactly one list in the modal.
  if (_chartHost) {
    // Emptied on the way out: one expansion at a time, and its canvas is not
    // ours to keep — Chart.js would meet a busy canvas on the next one.
    for (const holder of _chartHost.querySelectorAll('.device-check-chart')) {
      holder.replaceChildren();
      holder.hidden = true;
    }
  }
  _openCheckId = null;
}

/** The 24-hour latency chart under the row that was clicked. A failed result
 *  has no useful latency, so it is a gap in the line rather than a zero — a
 *  zero would read as "answered in no time" on the same axis as the good ones. */
async function toggleDeviceCheckChart(row) {
  const checkId = row.dataset.deviceCheckToggle;
  if (!checkId) return;
  const holder = row.nextElementSibling;
  if (!holder || !holder.classList.contains('device-check-chart')) return;

  if (_openCheckId === checkId) {
    collapseDeviceCheckChart();
    return;
  }
  collapseDeviceCheckChart();
  _openCheckId = checkId;
  holder.hidden = false;

  let results = [];
  try {
    results = await apiJson(`${_checkPath(checkId)}results/?hours=24`);
  } catch (e) {
    const note = document.createElement('div');
    note.className = 'device-check-note';
    note.textContent = e.message || 'Could not load results';
    holder.appendChild(note);
    return;
  }
  if (!Array.isArray(results) || !results.length) {
    const note = document.createElement('div');
    note.className = 'device-check-note';
    note.textContent = NO_POLLER_HINT;
    holder.appendChild(note);
    return;
  }
  const canvas = document.createElement('canvas');
  holder.appendChild(canvas);
  // An SNMP check has values to show beside the line: its device's name and
  // uptime and whatever OIDs the operator asked for, from the newest result.
  const check = _loadedChecks.get(String(checkId));
  const extra = check && check.latest ? check.latest.extra : null;
  let anchor = holder;
  if (check && check.kind === 'snmp' && extra && Object.keys(extra).length) {
    // Straight after the chart host: the host is a fixed-height box Chart.js
    // sizes itself to, and the rows share one list, so the values follow it.
    _checkValues = _snmpValuesList(extra);
    holder.after(_checkValues);
    anchor = _checkValues;
  }
  // An SSH check adds its panel straight after the chart host — before the
  // interface walk, whose answer would otherwise jump the queue and land above
  // it: which key to install on the device, what host key we trust, and every
  // kept config.
  if (check && check.kind === 'ssh') {
    holder.after(_configBackupPanel(check, _canEditChecks(_checkDeviceId)));
    void _loadConfigBackups(check.id);
  }
  // A stale answer is dropped by _loadCheckInterfaces' own guard, so it runs
  // unawaited: awaiting would hold the chart up behind a second request.
  if (check && check.kind === 'snmp' && check.params && check.params.interfaces === true) {
    void _loadCheckInterfaces(checkId, anchor);
  }
  _checkChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [{
        label: 'Latency',
        data: results.map(r => ({ x: new Date(r.time).getTime(), y: r.ok ? r.latency_ms : null })),
        borderColor: tok('--mint'), borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 3, fill: false, spanGaps: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: { type: 'time', grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: { min: 0, grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 }, callback: v => v + ' ms' } },
      },
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: tok('--s1'), borderColor: tok('--border'), borderWidth: 1, callbacks: { label: c => _ms(c.parsed.y) } },
      },
    },
  });
}

/** Bits per second, at the largest unit that still leaves a number ≥ 1. A null
 *  rate — no walk yet, or an interface added between two of them — is a dash. */
function _fmtBps(value) {
  if (value === null || value === undefined) return '-';
  const units = ['Gb/s', 'Mb/s', 'kb/s', 'b/s'];
  for (let i = 0; i < units.length; i++) {
    const scale = Math.pow(1000, units.length - 1 - i);
    if (Math.abs(value) >= scale || i === units.length - 1) {
      return `${+((value / scale).toFixed(1))} ${units[i]}`;
    }
  }
  return '-';
}

/** An interface's link speed. The walk reports megabits per second, so a
 *  thousand of them is the round Gb/s reading an operator expects. */
function _fmtSpeed(mbps) {
  if (mbps === null || mbps === undefined) return '-';
  if (mbps < 1000) return `${mbps} Mb/s`;
  return `${+(mbps / 1000).toFixed(1)} Gb/s`;
}

/** A note in place of the table or a chart: no data yet, or a failed request. */
function _interfaceNote(message) {
  const note = document.createElement('div');
  note.className = 'device-check-note';
  note.textContent = message;
  return note;
}

function _interfaceTable(rows) {
  const table = document.createElement('table');
  table.className = 'device-check-ifaces';
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const title of ['Interface', 'State', 'Speed', 'In', 'Out']) {
    const cell = document.createElement('th');
    cell.textContent = title;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    // The interface's own number, as the detail endpoint wants it back.
    tr.dataset.ifIndex = String(row.index);
    const state = row.up === true ? 'Up' : row.up === false ? 'Down' : '-';
    // The state's colour class hangs on a span so the td stays plain. Names
    // come back off the device, so every cell here is a text node.
    tr.appendChild(_ifaceCell('', row.name || `if${row.index}`));
    tr.appendChild(_ifaceCell(`device-check-if-state${row.up === true ? ' up' : row.up === false ? ' down' : ''}`, state));
    tr.appendChild(_ifaceCell('', _fmtSpeed(row.speed_mbps)));
    tr.appendChild(_ifaceCell('', _fmtBps(row.in_bps)));
    tr.appendChild(_ifaceCell('', _fmtBps(row.out_bps)));
    body.appendChild(tr);
  }
  table.appendChild(body);
  return table;
}

function _ifaceCell(className, text) {
  const td = document.createElement('td');
  if (!className) {
    td.textContent = text;
    return td;
  }
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  td.appendChild(span);
  return td;
}

/** The interface walk's answer, placed right after `anchor` (the values list,
 *  or the chart host when there is none). An answer that lands after the row
 *  closed, or after another row opened, is dropped. */
async function _loadCheckInterfaces(checkId, anchor) {
  let rows;
  let failed = '';
  try {
    const answer = await apiJson(`${_checkPath(checkId)}interfaces/`);
    rows = Array.isArray(answer) ? answer : [];
  } catch (err) {
    failed = err.message;
  }
  if (_openCheckId !== checkId || !anchor.parentElement) return;
  const box = document.createElement('div');
  box.className = 'device-check-if-box';
  box.appendChild(rows && rows.length ? _interfaceTable(rows) : _interfaceNote(failed || 'No interface data yet'));
  _checkInterfaces = box;
  anchor.after(box);
}

/** In/out bits per second for one interface over the last day, drawn under the
 *  table. A second click on the selected row closes it; a null rate is a gap. */
async function _selectInterface(table, tr) {
  const checkId = _openCheckId;
  const index = tr.dataset.ifIndex;
  const box = table.parentElement;
  if (!checkId || !index || !box) return;
  const wasSelected = tr.classList.contains('selected');
  for (const row of table.querySelectorAll('tr[data-if-index]')) row.classList.remove('selected');
  if (_interfaceChart) { _interfaceChart.destroy(); _interfaceChart = null; }
  // Whatever sits under the table (a chart or a note) belongs to the last pick.
  for (const el of [...box.children]) if (el !== table) el.remove();
  if (wasSelected) return;
  tr.classList.add('selected');

  let answer;
  try {
    answer = await apiJson(`${_checkPath(checkId)}interfaces/${encodeURIComponent(index)}/?hours=24`);
  } catch (err) {
    if (_openCheckId === checkId && tr.classList.contains('selected')) box.appendChild(_interfaceNote(err.message));
    return;
  }
  // Collapsed, another row opened, or another interface picked meanwhile.
  if (_openCheckId !== checkId || !tr.classList.contains('selected')) return;
  const series = answer && Array.isArray(answer.series) ? answer.series : [];
  const host = document.createElement('div');
  host.className = 'device-check-chart device-check-if-chart';
  // The canvas goes in with the host already placed: Chart.js sizes itself to
  // a box it can measure.
  host.appendChild(document.createElement('canvas'));
  box.appendChild(host);
  _interfaceChart = new Chart(host.children[0].getContext('2d'), {
    type: 'line',
    data: {
      datasets: [
        { label: 'In', data: series.map((p) => ({ x: new Date(p.time).getTime(), y: p.in_bps ?? null })), borderColor: tok('--sky'), backgroundColor: 'transparent', tension: 0.2, borderWidth: 1.6, pointRadius: 0, spanGaps: false },
        { label: 'Out', data: series.map((p) => ({ x: new Date(p.time).getTime(), y: p.out_bps ?? null })), borderColor: tok('--lavender'), backgroundColor: 'transparent', tension: 0.2, borderWidth: 1.6, pointRadius: 0, spanGaps: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, labels: { boxWidth: 10, font: { size: 11 } } },
        tooltip: { backgroundColor: tok('--s1'), borderColor: tok('--border'), borderWidth: 1, callbacks: { label: (item) => ` ${item.dataset.label}: ${_fmtBps(item.parsed.y)}` } },
      },
      scales: {
        x: { type: 'time', grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: {
          min: 0,
          grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') },
          // The same units as the table's In and Out columns, not raw bits.
          ticks: { maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 }, callback: (v) => _fmtBps(v) },
        },
      },
    },
  });
}

/* ── Config backups: an SSH check's keys and every kept configuration ───────

`apps_business.configbackup` keeps one text per successful poll (`/api/v1/
config-backups/?check=<id>`). The panel under an expanded SSH row shows what to
install on the device, what we already trust of it, and what we have of it. */

// The expansion's check id and its panel: a delegated handler gets an element,
// not the row that drew it, and both are gone by the time a request answers.
let _panelOwner = '';
let _openPanel = null;
// What the table was drawn from, so the Compare line can name what is ticked.
let _backupRows = [];
// The View / Compare box under the table, whenever either put one there.
let _backupViewer = null;

/** Take down the expanded SSH row's panel and forget whose it was, so a list
 *  still on its way finds nothing to paint. */
function _collapseSshPanel() {
  if (_openPanel) _openPanel.remove();
  _openPanel = null;
  _panelOwner = '';
  _backupRows = [];
  _backupViewer = null;
}

function _configBackupsPath(checkId) {
  return `/api/v1/config-backups/?check=${encodeURIComponent(String(checkId))}`;
}

function _configBackupDetailPath(id) {
  return `/api/v1/config-backups/${encodeURIComponent(String(id))}/`;
}

/** The SSH panel a clicked control sits in, or null. */
function _panelFor(el) {
  return (el && el.closest) ? el.closest('.device-check-ssh-panel') : null;
}

/** The part of the open panel the backup list draws into (under the key lines). */
function _backupList() {
  return _openPanel ? _openPanel.querySelector('.device-check-backup-list') : null;
}

/** "Public key: …", the key, and the button that takes it to the clipboard. */
function _sshKeyLine(publicKey) {
  const wrap = document.createElement('div');
  const lead = document.createElement('div');
  lead.className = 'device-check-ssh-line';
  lead.textContent = "Public key: add it to the device's authorized keys";
  wrap.appendChild(lead);

  // The key on a line of its own, wide as the panel and broken anywhere it has
  // to be: an ed25519 line outruns this modal twice over.
  const row = document.createElement('div');
  row.className = 'device-check-ssh-keyline';
  const code = document.createElement('code');
  code.className = 'device-check-ssh-key';
  code.textContent = publicKey;
  row.appendChild(code);
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'btn btn-sky btn-sm device-check-ssh-copy';
  copy.dataset.sshCopy = publicKey;
  copy.textContent = 'Copy';
  row.appendChild(copy);
  wrap.appendChild(row);
  return wrap;
}

/** "Host key:" with the pinned one, or what an unpinned check is waiting for. */
function _sshHostKeyLine(hostKey, canEdit) {
  const line = document.createElement('div');
  line.className = 'device-check-ssh-line';
  const label = document.createElement('span');
  label.textContent = 'Host key:';
  line.appendChild(label);
  if (!hostKey) {
    // Nothing to forget, so the sentence says why the panel offers no such button.
    const pending = document.createElement('span');
    pending.className = 'device-check-ssh-unpinned';
    pending.textContent = 'not pinned yet: the first run learns it';
    line.appendChild(pending);
    return line;
  }
  const code = document.createElement('code');
  code.className = 'device-check-ssh-key';
  code.textContent = hostKey;
  line.appendChild(code);
  if (canEdit) {
    const forget = document.createElement('button');
    forget.type = 'button';
    forget.className = 'btn btn-ghost btn-sm device-check-ssh-forget';
    forget.dataset.sshUnpin = '';
    forget.textContent = 'Forget host key';
    line.appendChild(forget);
  }
  return line;
}

function _configBackupPanel(check, canEdit) {
  const params = check.params || {};
  _panelOwner = String(check.id);
  _openPanel = null;
  _backupRows = [];
  _backupViewer = null;

  const panel = document.createElement('div');
  panel.className = 'device-check-ssh-panel';
  if (params.public_key) panel.appendChild(_sshKeyLine(String(params.public_key)));
  panel.appendChild(_sshHostKeyLine(params.host_key, canEdit));
  if (canEdit) {
    const rotate = document.createElement('button');
    rotate.type = 'button';
    rotate.className = 'btn btn-ghost btn-sm device-check-ssh-rotate';
    rotate.dataset.sshRotate = '';
    rotate.textContent = 'New key pair';
    panel.appendChild(rotate);
  }
  // Left empty for the list to fill, so a slow answer cannot leave its table
  // beside the note that already answered for it, and never touches the key
  // lines above it.
  const list = document.createElement('div');
  list.className = 'device-check-backup-list';
  panel.appendChild(list);
  _openPanel = panel;
  return panel;
}

function _backupHeading(text) {
  const head = document.createElement('div');
  head.className = 'device-check-ssh-line';
  head.textContent = text;
  return head;
}

/** One kept configuration: its tick, when, how big, and the two ways out. */
function _backupRow(backup) {
  const tr = document.createElement('tr');

  const pick = document.createElement('td');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.dataset.backupPick = String(backup.id);
  box.setAttribute('aria-label', 'Select this configuration to compare with another');
  pick.appendChild(box);
  tr.appendChild(pick);

  const when = document.createElement('td');
  when.textContent = new Date(backup.taken_at).toLocaleString();
  // The answer as the server wrote it, beside what the locale turned local.
  when.title = String(backup.taken_at);
  tr.appendChild(when);

  const size = document.createElement('td');
  size.className = 'device-check-backup-size';
  size.textContent = `${(Number(backup.size || 0) / 1024).toFixed(1)} KB`;
  tr.appendChild(size);

  const view = document.createElement('td');
  const viewBtn = document.createElement('button');
  viewBtn.type = 'button';
  viewBtn.className = 'btn btn-sky btn-sm device-check-backup-view';
  viewBtn.dataset.backupView = String(backup.id);
  viewBtn.textContent = 'View';
  view.appendChild(viewBtn);
  tr.appendChild(view);

  // A link rather than a button that fetches and saves: the browser can keep
  // the name the server gives it, and the file never has to be a JS string.
  const saved = document.createElement('td');
  const link = document.createElement('a');
  link.className = 'btn btn-ghost btn-sm device-check-backup-download';
  link.href = `${_configBackupDetailPath(backup.id)}download/`;
  link.download = '';
  link.textContent = 'Download';
  saved.appendChild(link);
  tr.appendChild(saved);
  return tr;
}

function _backupsTable(rows) {
  const table = document.createElement('table');
  table.className = 'device-check-backups';
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  for (const title of ['Compare', 'Taken', 'Size', '', '']) {
    const cell = document.createElement('th');
    cell.textContent = title;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const body = document.createElement('tbody');
  for (const backup of rows) body.appendChild(_backupRow(backup));
  table.appendChild(body);
  return table;
}

/** The Compare button, out of reach until exactly two rows are ticked. */
function _compareButton() {
  const compare = document.createElement('button');
  compare.type = 'button';
  compare.className = 'btn btn-ghost btn-sm device-check-backup-compare';
  compare.dataset.backupCompare = '';
  compare.disabled = true;
  compare.textContent = 'Compare';
  return compare;
}

function _backupPicks() {
  return _openPanel ? Array.from(_openPanel.querySelectorAll('[data-backup-pick]')) : [];
}

function _pickedIds() {
  const ids = [];
  for (const box of _backupPicks()) if (box.checked) ids.push(String(box.dataset.backupPick));
  // Newest first down the table, so with two ticks the second is the older:
  // the diff reads forward in time, additions as lines the device gained.
  return ids.length === 2 ? [ids[1], ids[0]] : ids;
}

/** The rows the ticks cover, in the order they are kept: newest first. */
function _pickedRows() {
  const ids = new Set(_backupPicks().filter((b) => b.checked).map((b) => String(b.dataset.backupPick)));
  return _backupRows.filter((b) => ids.has(String(b.id)));
}

/** The Compare line: the button once two rows are ticked, and until then the
 *  one sentence that says what is still wanted — naming what already is. */
function _compareLine() {
  const picked = _pickedRows();
  if (picked.length === 2) return _compareButton();
  const line = _backupHeading('Tick two configurations to compare them.');
  if (picked.length === 1) {
    // Its own time cell, so the sentence cannot name a row that is not there.
    const only = document.createElement('span');
    only.textContent = ` One so far: ${new Date(picked[0].taken_at).toLocaleString()}.`;
    line.appendChild(only);
  }
  return line;
}

/** Redraw the Compare line against the ticks as they stand. */
function _syncCompareButton(panel) {
  const line = panel ? panel.querySelector('.device-check-backup-compare-line') : null;
  if (line) line.replaceChildren(_compareLine());
}

/** Whatever the last View or Compare left under the table. */
function _dropBackupViewer() {
  if (_backupViewer) { _backupViewer.remove(); _backupViewer = null; }
}

/** A configuration, or a diff, under the table: one box per line of text the
 *  device said, so a `<` there stays a character and colour still reaches it. */
function _showBackupText(text) {
  if (!_openPanel) return;
  _dropBackupViewer();
  const pre = document.createElement('pre');
  pre.className = 'device-check-config';
  for (const line of String(text === null || text === undefined ? '' : text).split('\n')) {
    const span = document.createElement('span');
    if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'diff-del';
    else if (line.startsWith('@@')) span.className = 'diff-hunk';
    // A context line carries no class: the box behind it is already its colour.
    span.textContent = line;
    pre.appendChild(span);
  }
  _openPanel.appendChild(pre);
  _backupViewer = pre;
}

/** The list, replacing anything a lapsed license or an empty answer said. */
async function _loadConfigBackups(checkId) {
  let answer;
  try {
    answer = await apiJson(_configBackupsPath(checkId));
  } catch (e) {
    // 402 is the license and worth a sentence. 404 means this install has no
    // configbackup at all, or the check is not an SSH one: say nothing.
    const list = _panelOwner === String(checkId) ? _backupList() : null;
    if (!list) return;
    list.replaceChildren();
    if (e.status === 402) {
      list.appendChild(_backupHeading('Config backups need a Business license.'));
    }
    return;
  }
  // Collapsed, or another row opened, while that was out.
  const list = _panelOwner === String(checkId) ? _backupList() : null;
  if (!list) return;
  const rows = Array.isArray(answer) ? answer : [];
  _backupRows = rows;
  _dropBackupViewer();
  list.replaceChildren();
  if (!rows.length) {
    list.appendChild(_backupHeading('No configuration kept yet: the first successful run keeps one.'));
    return;
  }
  list.appendChild(_backupHeading('Configurations kept'));
  list.appendChild(_backupsTable(rows));
  const compareLine = document.createElement('div');
  compareLine.className = 'device-check-backup-compare-line';
  list.appendChild(compareLine);
  _syncCompareButton(_openPanel);
}

async function _showBackup(el) {
  const panel = _panelFor(el);
  if (!panel) return;
  const owner = _panelOwner;
  try {
    const answer = await apiJson(_configBackupDetailPath(el.dataset.backupView));
    // Another row may have opened, or this one closed, behind the request.
    if (_panelOwner !== owner || _openPanel !== panel) return;
    _openPanel = panel;
    _showBackupText(answer && answer.content);
  } catch (e) {
    showToast(e.message || 'Could not read that configuration', 'error');
  }
}

async function _compareBackups(el) {
  const panel = _panelFor(el);
  if (!panel) return;
  const ids = _pickedIds();
  if (ids.length !== 2) return;
  const owner = _panelOwner;
  try {
    const answer = await apiJson(
      `/api/v1/config-backups/diff/?from=${encodeURIComponent(ids[0])}&to=${encodeURIComponent(ids[1])}`);
    if (_panelOwner !== owner || _openPanel !== panel) return;
    _openPanel = panel;
    _showBackupText(answer && answer.diff);
  } catch (e) {
    showToast(e.message || 'Could not compare those configurations', 'error');
  }
}

/** Copy, forget or rotate: the three things a key pair needs of a page. */
async function _copySshKey(el) {
  await copyText(String(el.dataset.sshCopy || ''));
  showToast('Copied', 'success');
}

/** The check as it stands, less the public half the server writes alone, plus
 *  an empty host key: what a PATCH must send to change one key of a check. */
function _sshParamsPatch(check) {
  const params = { ...(check.params || {}) };
  delete params.public_key;
  params.host_key = '';
  return JSON.stringify({ params });
}

async function _rotateSshKey() {
  if (!confirm('Make a new key pair? The device refuses the poller until the new public key is installed on it.')) return;
  const check = _loadedChecks.get(_panelOwner);
  if (!check) return;
  try {
    await apiJson(_checkPath(check.id), { method: 'PATCH', body: JSON.stringify({ rotate_key: true }) });
    showToast('New key pair made. Install the public key on the device.', 'success');
    await loadDeviceChecks(_checkDeviceId);   // the new key is in the list's params
  } catch (e) {
    showToast((e.body && e.body.error) || e.message || 'Could not make a new key pair', 'error');
  }
}

async function _unpinSshHostKey() {
  if (!confirm('Forget the pinned host key? The next run trusts whatever key the device presents.')) return;
  const check = _loadedChecks.get(_panelOwner);
  if (!check) return;
  try {
    await apiJson(_checkPath(check.id), { method: 'PATCH', body: _sshParamsPatch(check) });
    showToast('Host key forgotten. The next run learns it again.', 'success');
    await loadDeviceChecks(_checkDeviceId);
  } catch (e) {
    showToast((e.body && e.body.error) || e.message || 'Could not forget the host key', 'error');
  }
}

/** A tick: the Compare line under the table answers it. */
function _pickBackup(box) {
  const panel = _panelFor(box);
  if (!panel) return;
  _openPanel = panel;
  _syncCompareButton(panel);
}

/** The add row is one flex line, so the SSH boxes take a line to themselves. */
function setDeviceCheckKind(select) {
  const kind = select && 'value' in select ? select.value : (select || 'icmp');
  const port = _deviceEl('device-check-port');
  if (port) {
    // SSH reaches the device on a real TCP port, and it is nearly always 22 —
    // the placeholder says so instead of leaving the box blank and required.
    port.hidden = kind !== 'tcp' && kind !== 'snmp' && kind !== 'ssh';
    port.placeholder = kind === 'snmp' ? 'Port (161)' : (kind === 'ssh' ? 'Port (22)' : 'Port');
  }
  const version = _deviceEl('device-check-snmp-version');
  if (version) version.hidden = kind !== 'snmp';
  _showSnmpSecretFields(kind === 'snmp' ? (version ? version.value : '2c') : null);
  const oids = _deviceEl('device-check-oids');
  if (oids) oids.hidden = kind !== 'snmp';
  const walk = _deviceEl('device-check-walk');
  if (walk) walk.hidden = kind !== 'snmp';
  const ssh = _deviceEl('device-check-ssh');
  if (ssh) ssh.hidden = kind !== 'ssh';
  // A config is worth keeping, not polling every minute, and the server would
  // refuse anything under fifteen: nudge the pick instead of the operator.
  const interval = _deviceEl('device-check-interval');
  if (kind === 'ssh' && interval && parseInt(interval.value, 10) < 900) interval.value = '86400';
}

/* ── License: SSH config backup is a Business feature (S9 phase 08d) ─────── */

// One answer per page load: the license does not change while the modal is open.
let _configBackupLicense = null;

async function _configBackupActive() {
  if (_configBackupLicense !== null) return _configBackupLicense;
  try {
    const lic = await apiJson('/api/v1/license/');
    const f = (lic.features || []).find((x) => x.name === 'config_backup');
    _configBackupLicense = !!(f && f.active);
  } catch (e) {
    _configBackupLicense = false;
  }
  return _configBackupLicense;
}

/** Unlicensed, the SSH kind stays in the list to be seen but cannot be picked. */
async function _applyConfigBackupLicense() {
  const option = document.getElementById('device-check-kind-ssh');
  if (!option || await _configBackupActive()) return;
  option.disabled = true;
  option.textContent = 'SSH config backup (Business)';
}

/** The community box for v2c, the user/protocol/password row for v3, neither otherwise. */
function _showSnmpSecretFields(version) {
  const community = _deviceEl('device-check-community');
  if (community) community.hidden = version !== '2c';
  const v3 = _deviceEl('device-check-v3');
  if (v3) v3.hidden = version !== '3';
}

function setDeviceCheckSnmpVersion(select) {
  _showSnmpSecretFields(select && select.value === '3' ? '3' : '2c');
}

async function addDeviceCheck() {
  if (!_checkDeviceId) return;
  const kind = _deviceEl('device-check-kind').value;
  const target = _deviceEl('device-check-target').value.trim();
  const portRaw = _deviceEl('device-check-port').value.trim();
  const communityBox = _deviceEl('device-check-community');
  const oidsBox = _deviceEl('device-check-oids');
  const walkBox = _deviceEl('device-check-interfaces');
  const interval = parseInt(_deviceEl('device-check-interval').value, 10);

  let params;
  if (kind === 'http') params = { url: target };
  else if (kind === 'dns') params = { name: target };
  else if (kind === 'tcp') params = { host: target, port: parseInt(portRaw, 10) };
  else if (kind === 'ssh') params = { host: target, username: _deviceEl('device-check-ssh-user').value.trim(), command: _deviceEl('device-check-ssh-command').value.trim() };
  else params = { host: target };

  const body = { kind, params, interval_seconds: interval };
  if (kind === 'ssh' && portRaw) params.port = parseInt(portRaw, 10);
  if (kind === 'snmp') {
    if (portRaw) params.port = parseInt(portRaw, 10);
    const parsed = parseSnmpOids(oidsBox.value);
    // A malformed line is ours to catch: nothing goes out until it reads.
    if (parsed.error) { showToast(parsed.error, 'error'); return; }
    if (parsed.oids.length) params.oids = parsed.oids;
    if (walkBox && walkBox.checked) params.interfaces = true;
    const version = _deviceEl('device-check-snmp-version');
    if (version && version.value === '3') {
      // Passwords untrimmed, like the community: their spaces are the operator's.
      const privPassword = _deviceEl('device-check-v3-priv-password').value;
      params.version = '3';
      params.user = _deviceEl('device-check-v3-user').value.trim();
      params.auth = _deviceEl('device-check-v3-auth').value;
      params.priv = privPassword !== '';
      body.auth_password = _deviceEl('device-check-v3-auth-password').value;
      if (privPassword) body.priv_password = privPassword;
    } else if (communityBox.value) {
      // Untrimmed: a community's spaces are the operator's, not ours to drop.
      body.community = communityBox.value;
    }
  }

  try {
    await apiJson(`/api/v1/hosts/devices/${_checkDeviceId}/checks/`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    showToast('Check added', 'success');
    _deviceEl('device-check-target').value = '';
    _deviceEl('device-check-port').value = '';
    communityBox.value = '';
    for (const id of ['device-check-v3-user', 'device-check-v3-auth-password', 'device-check-v3-priv-password']) {
      const box = _deviceEl(id);
      if (box) box.value = '';
    }
    oidsBox.value = '';
    if (walkBox) walkBox.checked = false;
    for (const id of ['device-check-ssh-user', 'device-check-ssh-command']) {
      const box = _deviceEl(id);
      if (box) box.value = '';
    }
    loadDeviceChecks(_checkDeviceId);
  } catch (e) {
    // The server refuses a bad target with one sentence in `error`.
    showToast((e.body && e.body.error) || e.message || 'Could not add check', 'error');
  }
}

async function toggleDeviceCheck(checkId, enabled) {
  try {
    await apiJson(_checkPath(checkId), { method: 'PATCH', body: JSON.stringify({ enabled }) });
    showToast(enabled ? 'Check enabled' : 'Check disabled', 'success');
    if (_checkDeviceId) loadDeviceChecks(_checkDeviceId);
  } catch (e) {
    showToast(e.message || 'Could not change the check', 'error');
  }
}

async function deleteDeviceCheck(checkId) {
  if (!await confirmModal('Delete this check and its results?',
    { title: 'Delete check', confirmText: 'Delete', danger: true })) return;
  try {
    await apiJson(_checkPath(checkId), { method: 'DELETE' });
    showToast('Check deleted', 'success');
    if (_checkDeviceId) loadDeviceChecks(_checkDeviceId);
  } catch (e) {
    showToast(e.message || 'Could not delete the check', 'error');
  }
}

/* ── The rest of the device form ─────────────────────────────────────────── */

function _deviceFormBody() {
  const ip = document.getElementById('device-ip').value.trim();
  return {
    name: document.getElementById('device-name').value.trim(),
    device_type: document.getElementById('device-type').value,
    ip_address: ip || null,
    mac_address: document.getElementById('device-mac').value.trim(),
    vendor: document.getElementById('device-vendor').value.trim(),
    location: document.getElementById('device-location').value.trim(),
    serial: document.getElementById('device-serial').value.trim(),
    warranty_end: document.getElementById('device-warranty').value || null,
    owner: document.getElementById('device-owner').value.trim(),
    extra: _extraFromBody(document.getElementById('device-extra').value),
    notes: document.getElementById('device-notes').value.trim(),
  };
}

/** The extra-facts object as the textarea shows it: "Key: value" per line. */
function _extraToLines(extra) {
  return Object.entries(extra || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
}

/** And back: split on the first colon so a value may carry its own colons. */
function _extraFromBody(text) {
  const extra = {};
  for (const line of (text || '').split('\n')) {
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    if (!key) continue;
    extra[key] = line.slice(colon + 1).trim();
  }
  return extra;
}

async function saveDevice() {
  const id = document.getElementById('device-id').value;
  const body = _deviceFormBody();
  if (!body.name) { showToast('Name is required', 'error'); return; }
  const siteId = sitePickValue(document.getElementById('device-site'), document.getElementById('device-site-field'));
  if (siteId !== undefined) body.site_id = siteId;
  try {
    if (id) {
      await apiJson(`/api/v1/hosts/devices/${id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast('Device updated', 'success');
    } else {
      await apiJson('/api/v1/hosts/devices/', { method: 'POST', body: JSON.stringify(body) });
      showToast('Device added', 'success');
    }
    closeDeviceModal();
    loadDevices();
  } catch (e) {
    showToast(e.message || 'Could not save device — check the IP and MAC format', 'error');
  }
}

async function deleteDeviceFromModal() {
  const id = document.getElementById('device-id').value;
  if (!id) return;
  if (!confirm('Delete this device from the inventory?')) return;
  try {
    await apiJson(`/api/v1/hosts/devices/${id}/`, { method: 'DELETE' });
    showToast('Device deleted', 'success');
    closeDeviceModal();
    loadDevices();
  } catch (e) {
    showToast(e.message || 'Delete failed', 'error');
  }
}

const _origNavForDevices = navigateTo;
navigateTo = function(pageName) {
  _origNavForDevices(pageName);
  if (pageName === 'inventory') loadDevices();
};

/* This one applied escHtml inside an onclick attribute — escaping for the
   wrong context, harmless only because the value is a server UUID. */
delegateClick('[data-device-edit]', (el) => openDeviceModal(el.dataset.deviceEdit));

// A row click opens or closes its latency chart. The data-act dispatcher and
// this listener both sit on the document, so a click on the row's own toggle or
// Delete button must be skipped here or it would open the chart too.
delegateClick('.device-check-row', (el, ev) => {
  if (ev.target.closest('[data-act]')) return;
  toggleDeviceCheckChart(el);
});
// The SSH panel's controls (S9 08d): one delegated listener each.
delegateClick('[data-ssh-copy]', (el) => { void _copySshKey(el); });
delegateClick('[data-ssh-rotate]', () => { void _rotateSshKey(); });
delegateClick('[data-ssh-unpin]', () => { void _unpinSshHostKey(); });
delegateClick('[data-backup-view]', (el) => { void _showBackup(el); });
delegateClick('[data-backup-compare]', (el) => { void _compareBackups(el); });
delegateClick('[data-backup-pick]', (el) => _pickBackup(el));
// An interface row's traffic chart: one delegated listener for every table.
delegateClick('.device-check-ifaces tr[data-if-index]', (el) => {
  void _selectInterface(el.closest('table'), el);
});
