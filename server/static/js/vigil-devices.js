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

//: The handler the Add check button names in its data-act attribute. The
//: dispatcher resolves handlers by name at click time, so markup and script are
//: pinned to one string and no inline script is needed under this page's CSP.
const DEVICE_CHECK_ADD_ACT = 'addDeviceCheck';

function closeDeviceModal() {
  collapseDeviceCheckChart();
  document.getElementById('device-overlay').classList.remove('open');
  document.getElementById('device-modal').classList.remove('open');
}

/* ── Checks: what a poller reports about this device (S9) ─────────────────── */

const DEVICE_CHECK_KINDS = { icmp: 'Ping', tcp: 'TCP', http: 'HTTP', dns: 'DNS' };
const NO_POLLER_HINT = 'No results yet: is a poller assigned? See Settings > Agents for hosts with poller: true';

let _checkDeviceId = null;
// One expanded row at a time, and one Chart.js instance to go with it.
let _openCheckId = null;
let _checkChart = null;
// The element the rows and their chart hosts live under — #device-checks-list.
let _chartHost = null;

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
  // The dispatcher resolves a data-act handler by name at click time, so naming
  // it here keeps the markup and this file pinned to one string.
  const addButton = _deviceEl('device-check-add-btn');
  if (addButton) addButton.setAttribute('data-act', DEVICE_CHECK_ADD_ACT);
  if (device) {
    setDeviceCheckKind(_deviceEl('device-check-kind'));
    loadDeviceChecks(device.id);
  } else if (_deviceEl('device-checks-list')) {
    _deviceEl('device-checks-list').replaceChildren();
  }
}

/** A check's target as an operator wrote it, so the row says what is polled. */
function _checkLabel(check) {
  const p = check.params || {};
  let target = '';
  if (check.kind === 'tcp') target = [p.host, p.port].filter(x => x !== undefined && x !== null && x !== '').join(':');
  else if (check.kind === 'http') target = p.url || '';
  else if (check.kind === 'dns') target = p.name || '';
  else target = p.host || '';
  return `${DEVICE_CHECK_KINDS[check.kind] || check.kind}${target ? ` ${target}` : ''}`;
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
}

function collapseDeviceCheckChart() {
  if (_checkChart) { _checkChart.destroy(); _checkChart = null; }
  // The host we last rendered into, tracked here rather than queried: the rows
  // are replaced on every reload and there is exactly one list in the modal.
  if (_chartHost) {
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
  holder.replaceChildren();

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
  _checkChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [{
        label: 'Latency',
        data: results.map(r => ({ x: new Date(r.time).getTime(), y: r.ok ? r.latency_ms : null })),
        borderColor: tok('--mint'), backgroundColor: 'rgba(var(--rgb-mint), var(--tint-a))',
        borderWidth: 1.5, pointRadius: 2, fill: true, spanGaps: false,
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

function setDeviceCheckKind(select) {
  const port = _deviceEl('device-check-port');
  if (!port) return;
  const kind = select && 'value' in select ? select.value : (select || 'icmp');
  port.hidden = kind !== 'tcp';
}

async function addDeviceCheck() {
  if (!_checkDeviceId) return;
  const kind = _deviceEl('device-check-kind').value;
  const target = _deviceEl('device-check-target').value.trim();
  const portRaw = _deviceEl('device-check-port').value.trim();
  const interval = parseInt(_deviceEl('device-check-interval').value, 10);

  let params;
  if (kind === 'http') params = { url: target };
  else if (kind === 'dns') params = { name: target };
  else if (kind === 'tcp') params = { host: target, port: parseInt(portRaw, 10) };
  else params = { host: target };

  try {
    await apiJson(`/api/v1/hosts/devices/${_checkDeviceId}/checks/`, {
      method: 'POST',
      body: JSON.stringify({ kind, params, interval_seconds: interval }),
    });
    showToast('Check added', 'success');
    _deviceEl('device-check-target').value = '';
    _deviceEl('device-check-port').value = '';
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
  if (!await confirmModal('Delete this check? Its results stay until they age out.',
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

/* Two handlers left their markup as inline onclick attributes: the device id
   interpolated into a JS string literal. delegateClick plus the data-act
   dispatcher is how every other button here is wired, and the CSP on this page
   carries no 'unsafe-inline' to make the old form legal anyway. */
delegateClick('[data-device-edit]', (el) => openDeviceModal(el.dataset.deviceEdit));

// A row click opens or closes its latency chart. Its action buttons are not
// rows — the dispatcher handles them first and stops the click there — so
// Disable and Delete never toggle the chart of the check they belong to.
delegateClick('.device-check-row', (el) => toggleDeviceCheckChart(el));
