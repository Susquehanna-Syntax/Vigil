// vigil-discovery.js — the Discover dialog on the Inventory page (S9, phase 06e).
// Depends on: vigil-utils.js (apiJson, showToast, delegateClick), vigil-devices.js
//   (loadDevices, so an adopted device shows in the table beside this dialog).
// API: GET /api/v1/hosts/discovery/pollers/, GET/POST .../discovery/scans/,
//      GET/DELETE .../discovery/scans/<id>/, POST .../discovery/scans/<id>/adopt/

const DISCOVERY_SCAN_BASE = '/api/v1/hosts/discovery/scans/';
const DISCOVERY_POLL_MS = 5000;

// The scan states the dialog names. Anything else — a state the server learned
// after this script was written — shows its own word, which is honest and needs
// no change here to stay readable.
const SCAN_STATES = { queued: 'Waiting', sent: 'Running', done: 'Done', failed: 'Failed' };

let _discoveryPollers = [];
let _discoveryScans = [];
// The scan whose hosts are drawn, so a poll can tell whether the row it just
// reloaded — and so whether the detail needs re-reading too.
let _discoveryScanId = null;
let _discoveryScanState = null;
let _discoveryTimer = null;
// Settled once the last thing the dialog fetched is drawn and its next poll is
// scheduled. The page never waits on it; a test does, to say "now look".
let _discoverySettled = Promise.resolve();

function _discoveryEl(id) {
  return document.getElementById(id);
}

function _discoveryStateWord(scan) {
  return SCAN_STATES[scan.state] || scan.state;
}

/** True while a listed scan still has its poller to hear from. */
function _discoveryWaiting(scans) {
  return scans.some((s) => s.state === 'queued' || s.state === 'sent');
}

/** Whether the dialog is open: the one thing that keeps the poll alive. */
function _discoveryIsOpen() {
  const modal = _discoveryEl('discovery-modal');
  return !!modal && modal.classList.contains('open');
}

/** "22, 80 443" or "22 80" to [22, 80, 443]; null when a piece is not a number. */
function parseDiscoveryPorts(raw) {
  const text = String(raw || '').trim();
  if (!text) return [];
  const ports = [];
  for (const part of text.split(/[\s,]+/)) {
    if (!/^\d+$/.test(part)) return null;
    ports.push(parseInt(part, 10));
  }
  return ports;
}

/** One scan row's own line: the network and poller, with its state alongside. */
function discoveryScanRowText(scan) {
  const bits = [scan.cidr, scan.poller ? scan.poller.hostname : '', _discoveryStateWord(scan)];
  return bits.filter(Boolean).join(' · ');
}

/** The count a finished scan reports, or the sentence a failed one left behind. */
function discoveryScanTailText(scan) {
  if (scan.state === 'done') return `${scan.found} found of ${scan.scanned}`;
  if (scan.state === 'failed') return scan.error || '';
  return '';
}

/* ── Opening and closing ─────────────────────────────────────────────────── */

async function openDiscovery() {
  return _trackDiscovery(_openDiscovery());
}

async function _openDiscovery() {
  document.getElementById('discovery-overlay').classList.add('open');
  document.getElementById('discovery-modal').classList.add('open');
  _discoveryStopPolling();
  await Promise.all([_loadDiscoveryPollers(), _loadDiscoveryScans()]);
}

function _trackDiscovery(promise) {
  // Each action chains onto the one before, so waiting on the last tells a
  // caller — a test, or anything else that needs the dialog settled — that
  // nothing is in flight. Nothing here depends on that itself.
  _discoverySettled = _discoverySettled.then(() => promise, () => promise);
  return promise;
}

function closeDiscovery() {
  _discoveryStopPolling();
  for (const id of ['discovery-overlay', 'discovery-modal']) {
    const el = _discoveryEl(id);
    if (el) el.classList.remove('open');
  }
}

async function _loadDiscoveryPollers() {
  const form = _discoveryEl('discovery-form');
  const help = _discoveryEl('discovery-help');
  let pollers;
  try {
    pollers = await apiJson('/api/v1/hosts/discovery/pollers/');
  } catch (e) {
    // The scans below still say something useful, so this block says only that
    // one of its two sources is missing.
    _discoveryNote('discovery-scans', 'Could not load pollers.');
    showToast(`Could not load pollers: ${e.message}`, 'error');
    return;
  }
  _discoveryPollers = Array.isArray(pollers) ? pollers : [];
  const scannable = _discoveryPollers.filter((p) => p && p.can_scan);
  if (!_discoveryPollers.length) {
    if (form) form.hidden = true;
    if (help) help.hidden = true;
    _discoveryNote('discovery-scans', 'No poller yet. Turn on "Poll devices" on a host\'s agent profile to scan from it.');
    return;
  }
  const select = _discoveryEl('discovery-poller');
  if (select) {
    // Rebuilt every open: a poller added since the dialog last opened should be
    // here, and an option that is not in the answer should be gone.
    select.replaceChildren();
    for (const p of _discoveryPollers) {
      const option = document.createElement('option');
      option.value = p.id;
      option.textContent = p.hostname + (p.online ? '' : ' (offline)');
      option.disabled = !p.can_scan;
      select.appendChild(option);
    }
    const firstOpen = _discoveryPollers.find((p) => p.can_scan);
    if (firstOpen) select.value = firstOpen.id;
  }
  // A viewer — or a site-scoped admin with no writable poller — reads the same
  // dialog without the means to queue anything.
  if (form) form.hidden = !scannable.length;
  if (help) help.hidden = !scannable.length;
}

async function _loadDiscoveryScans() {
  let scans;
  try {
    scans = await apiJson(DISCOVERY_SCAN_BASE);
  } catch (e) {
    _discoveryNote('discovery-scans', 'Could not load scans.');
    showToast(`Could not load scans: ${e.message}`, 'error');
    return;
  }
  _discoveryScans = Array.isArray(scans) ? scans : [];
  renderDiscoveryScans();
  if (_discoveryScanId) await _refreshOpenScan();
  // Reloaded rather than opened, so nothing else scheduled the next poll. A poll
  // that just re-read the open scan scheduled it once already and must not reset
  // the five seconds; a load from anywhere else has no timer to keep.
  if (_discoveryTimer === null && !_discoveryScanId) _discoverySchedule();
}

/* ── The scan list ───────────────────────────────────────────────────────── */

function renderDiscoveryScans() {
  const list = _discoveryEl('discovery-scans');
  if (!list) return;
  if (!_discoveryScans.length) {
    _discoveryNote('discovery-scans', 'No scans yet.');
    return;
  }
  // The answer is newest first and stays that way: a row is a button, so the
  // dialog needs no second click target to open one.
  const rows = _discoveryScans.map((scan) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'discovery-scan-row';
    row.dataset.scanId = scan.id;
    const head = document.createElement('span');
    head.className = 'discovery-scan-head';
    const cidr = document.createElement('span');
    cidr.className = 'discovery-scan-cidr';
    cidr.textContent = scan.cidr;
    const state = document.createElement('span');
    state.className = `discovery-scan-state discovery-scan-state--${scan.state}`;
    state.textContent = _discoveryStateWord(scan);
    head.append(cidr, state);
    const meta = document.createElement('span');
    meta.className = 'discovery-scan-meta';
    meta.textContent = [scan.poller ? scan.poller.hostname : '', discoveryScanTailText(scan)]
      .filter(Boolean).join(' · ');
    row.append(head, meta);
    if (scan.id === _discoveryScanId) row.classList.add('selected');
    return row;
  });
  list.replaceChildren(...rows);
}

/** One muted line inside a block of the dialog. */
function _discoveryNote(id, message) {
  const block = _discoveryEl(id);
  if (!block) return;
  const note = document.createElement('div');
  note.className = 'discovery-note';
  note.textContent = message;
  block.replaceChildren(note);
}

/* ── Polling while something is waiting ──────────────────────────────────── */

function _discoverySchedule() {
  _discoveryStopPolling();
  if (!_discoveryIsOpen() || !_discoveryWaiting(_discoveryScans)) return;
  _discoveryTimer = setTimeout(() => {
    _discoveryTimer = null;
    _trackDiscovery(_loadDiscoveryScans());
  }, DISCOVERY_POLL_MS);
}

function _discoveryStopPolling() {
  if (_discoveryTimer !== null) clearTimeout(_discoveryTimer);
  _discoveryTimer = null;
}

/** The open scan's row changed state under the poll: re-read its hosts. */
async function _refreshOpenScan() {
  // The id moves only with something to draw — a scan that cannot be read leaves
  // the dialog holding what it was showing, so the row still highlighted is the
  // scan still on screen. Hence the state is keyed by that same id.
  if (!_discoveryScanId) return;
  const open = _discoveryScans.find((s) => s.id === _discoveryScanId);
  if (!open) {
    // Deleted from elsewhere, or scrolled out of the 50 the list carries.
    _discoveryScanId = null;
    _discoveryScanState = null;
    const result = _discoveryEl('discovery-result');
    if (result) {
      result.hidden = true;
      result.replaceChildren();
    }
    return;
  }
  if (open.state !== _discoveryScanState) await loadDiscoveryScan(open.id);
}

/* ── Starting a scan ─────────────────────────────────────────────────────── */

async function startDiscoveryScan(ev) {
  return _trackDiscovery(_startDiscoveryScan(ev));
}

async function _startDiscoveryScan(ev) {
  ev.preventDefault();
  const poller = _discoveryEl('discovery-poller').value;
  const cidrBox = _discoveryEl('discovery-cidr');
  const portsBox = _discoveryEl('discovery-ports');
  const communityBox = _discoveryEl('discovery-community');
  const body = { poller, cidr: cidrBox.value.trim() };
  const ports = parseDiscoveryPorts(portsBox.value);
  if (ports === null) {
    showToast('Ports must be numbers, separated by commas', 'error');
    return;
  }
  if (ports.length) body.ports = ports;
  // Sent as typed: a community with a leading space is that community. Cleared
  // below, so the box is the only place it ever stands — never a toast, a log
  // line or the rest of the dialog.
  const community = communityBox.value;
  if (community) body.community = community;
  try {
    await apiJson(DISCOVERY_SCAN_BASE, { method: 'POST', body: JSON.stringify(body) });
  } catch (e) {
    // The server's own sentence is the useful part: "only private networks can
    // be scanned" says more than "400" ever will.
    showToast(e.message, 'error');
    return;
  }
  cidrBox.value = '';
  portsBox.value = '';
  communityBox.value = '';
  showToast('Scan queued', 'success');
  await _loadDiscoveryScans();
}

/* ── One scan's hosts ────────────────────────────────────────────────────── */

async function openDiscoveryScan(scanId) {
  return _trackDiscovery(_openDiscoveryScan(scanId));
}

async function _openDiscoveryScan(scanId) {
  await loadDiscoveryScan(scanId);
}

async function loadDiscoveryScan(scanId) {
  return _trackDiscovery(_loadDiscoveryScan(scanId));
}

async function _loadDiscoveryScan(scanId) {
  let scan;
  try {
    scan = await apiJson(`${DISCOVERY_SCAN_BASE}${scanId}/`);
  } catch (e) {
    // The dialog keeps holding whatever it was showing: half a new scan is worse
    // than the last whole one, so the id moves only with something to draw.
    _discoveryNote('discovery-result', 'Could not load this scan.');
    showToast(`Could not load this scan: ${e.message}`, 'error');
    return;
  }
  // A poll re-reads a scan that finished while the dialog was open under its own
  // id — which is the scan already drawn. Only one whose id differs is another
  // scan taking over, and only then does the list's highlight move with it.
  if (scan.id !== _discoveryScanId) {
    _discoveryScanId = scan.id;
    renderDiscoveryScans();
  }
  _discoveryScanState = scan.state;
  _discoverySchedule();
  renderDiscoveryScan(scan);
}

function renderDiscoveryScan(scan) {
  const result = _discoveryEl('discovery-result');
  if (!result) return;
  result.hidden = false;
  result.replaceChildren();

  const header = document.createElement('div');
  header.className = 'discovery-result-header';
  const headline = document.createElement('span');
  headline.className = 'discovery-result-title';
  headline.textContent = [scan.cidr, _discoveryStateWord(scan), scan.truncated ? 'Stopped at 1024 hosts' : '']
    .filter(Boolean).join(' · ');
  header.appendChild(headline);
  if (_discoveryCanScan()) {
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn-ghost btn-sm discovery-delete';
    del.dataset.act = 'deleteDiscoveryScan';
    del.textContent = 'Delete scan';
    header.appendChild(del);
  }
  result.appendChild(header);

  const hosts = Array.isArray(scan.hosts) ? scan.hosts : [];
  if (!hosts.length) {
    _discoveryAppendNote(result, scan.state === 'done' ? 'Nothing answered.' : 'Nothing to show yet.');
    return;
  }
  result.appendChild(_discoveryTable(hosts));
  if (scan.state === 'done' && _discoveryCanScan()) result.appendChild(_discoveryAdoptRow(scan));
}

function _discoveryCanScan() {
  return _discoveryPollers.some((p) => p && p.can_scan);
}

function _discoveryAppendNote(block, message) {
  const note = document.createElement('div');
  note.className = 'discovery-note';
  note.textContent = message;
  block.appendChild(note);
}

function _discoveryTable(hosts) {
  const wrap = document.createElement('div');
  wrap.className = 'discovery-table-wrap';
  const table = document.createElement('table');
  table.className = 'ctr-table discovery-table';
  const head = document.createElement('thead');
  head.appendChild(_discoveryRow('tr', [
    _discoveryCell('th', ''), _discoveryCell('th', 'IP'), _discoveryCell('th', 'Name'),
    _discoveryCell('th', 'Answered by'), _discoveryCell('th', 'Open ports'),
    _discoveryCell('th', 'Latency'),
  ]));
  const body = document.createElement('tbody');
  for (const host of hosts) {
    body.appendChild(_discoveryHostRow(host));
  }
  table.append(head, body);
  wrap.appendChild(table);
  return wrap;
}

function _discoveryHostRow(host) {
  const tr = document.createElement('tr');
  if (host.sys_descr) tr.title = host.sys_descr;

  const pick = document.createElement('td');
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.className = 'discovery-pick';
  box.dataset.hostId = String(host.id);
  box.disabled = !!host.known;
  box.addEventListener('change', _updateDiscoveryAdopt);
  pick.appendChild(box);

  const name = document.createElement('td');
  name.textContent = host.sys_name || host.name || '-';
  if (host.known) {
    const known = document.createElement('div');
    known.className = 'discovery-known';
    known.textContent = `Already a device: ${host.known.name}`;
    name.appendChild(known);
  }

  tr.append(
    pick,
    _discoveryCell('td', host.ip, 'discovery-mono'),
    name,
    _discoveryCell('td', host.alive_by || '-'),
    _discoveryCell('td', (host.open_ports || []).join(', '), 'discovery-mono'),
    _discoveryCell('td', host.latency_ms === null || host.latency_ms === undefined
      ? '-' : `${host.latency_ms} ms`, 'discovery-mono'),
  );
  return tr;
}

function _discoveryRow(tag, cells) {
  const row = document.createElement(tag);
  row.append(...cells);
  return row;
}

function _discoveryCell(tag, text, className) {
  const cell = document.createElement(tag);
  cell.textContent = text;
  if (className) cell.className = className;
  return cell;
}

/* ── Adding the ticked hosts as devices ──────────────────────────────────── */

function _discoveryAdoptRow(scan) {
  const row = document.createElement('div');
  row.className = 'discovery-adopt';

  // Read from the Add Device modal rather than a second copy of its list: the
  // two selects then cannot drift apart when a device type is added.
  const type = document.createElement('select');
  type.id = 'discovery-type';
  type.setAttribute('aria-label', 'Device type for the hosts to add');
  type.className = 'form-control discovery-type';
  const source = document.getElementById('device-type');
  if (source) {
    // The option nodes themselves, cloned rather than rebuilt: an <option>'s
    // value is its value attribute and `selected` a property of its own, and a
    // copy that read only textContent would lose both.
    for (const option of source.options) type.appendChild(option.cloneNode(true));
  }

  const all = document.createElement('button');
  all.type = 'button';
  all.className = 'btn btn-ghost btn-sm';
  all.dataset.act = 'selectAllDiscoveryHosts';
  all.textContent = 'Select all new';

  const adopt = document.createElement('button');
  adopt.type = 'button';
  adopt.className = 'btn btn-sm btn-mint';
  adopt.dataset.act = 'adoptDiscoveryHosts';
  adopt.dataset.scanId = scan.id;
  adopt.disabled = true;
  adopt.textContent = 'Add as devices';

  row.append(type, all, adopt);
  return row;
}

function _selectedDiscoveryHostIds() {
  const result = _discoveryEl('discovery-result');
  if (!result) return [];
  return [...result.querySelectorAll('.discovery-pick')]
    .filter((box) => box.checked)
    // The API names hosts by number; a dataset value is always a string.
    .map((box) => Number(box.dataset.hostId));
}

function _updateDiscoveryAdopt() {
  const result = _discoveryEl('discovery-result');
  const adopt = result && result.querySelector('[data-act="adoptDiscoveryHosts"]');
  if (!adopt) return;
  const count = _selectedDiscoveryHostIds().length;
  adopt.disabled = count === 0;
  adopt.textContent = count ? `Add ${count} as devices` : 'Add as devices';
}

async function selectAllDiscoveryHosts() {
  return _trackDiscovery(_selectAllDiscoveryHosts());
}

function _selectAllDiscoveryHosts() {
  const result = _discoveryEl('discovery-result');
  if (!result) return;
  for (const box of result.querySelectorAll('.discovery-pick')) {
    if (!box.disabled) box.checked = true;
  }
  _updateDiscoveryAdopt();
}

async function adoptDiscoveryHosts(ev, el) {
  return _trackDiscovery(_adoptDiscoveryHosts(ev, el));
}

async function _adoptDiscoveryHosts(ev, el) {
  const scanId = (el && el.dataset.scanId) || _discoveryScanId;
  const hosts = _selectedDiscoveryHostIds();
  if (!scanId || !hosts.length) return;
  const type = _discoveryEl('discovery-type');
  let created;
  try {
    created = await apiJson(`${DISCOVERY_SCAN_BASE}${scanId}/adopt/`, {
      method: 'POST',
      body: JSON.stringify({ hosts, device_type: type ? type.value : 'other' }),
    });
  } catch (e) {
    showToast(e.message, 'error');
    return;
  }
  const n = created && Array.isArray(created.created) ? created.created.length : 0;
  const skipped = created ? created.skipped : 0;
  const added = `Added ${n} device${n === 1 ? '' : 's'}`;
  showToast(skipped ? `${added}, ${skipped} already known` : added, 'success');
  await loadDiscoveryScan(scanId);
  // The Inventory table beside this dialog is vigil-devices.js's to draw.
  if (typeof loadDevices === 'function') await loadDevices();
}

async function deleteDiscoveryScan() {
  return _trackDiscovery(_deleteDiscoveryScan());
}

async function _deleteDiscoveryScan() {
  if (!_discoveryScanId) return;
  const scanId = _discoveryScanId;
  try {
    await apiJson(`${DISCOVERY_SCAN_BASE}${scanId}/`, { method: 'DELETE' });
  } catch (e) {
    showToast(e.message, 'error');
    return;
  }
  _discoveryScanId = null;
  _discoveryScanState = null;
  const result = _discoveryEl('discovery-result');
  if (result) {
    result.hidden = true;
    result.replaceChildren();
  }
  await _loadDiscoveryScans();
}

/* ── Wiring ──────────────────────────────────────────────────────────────── */

delegateClick('.discovery-scan-row', (el) => {
  if (el.dataset.scanId) openDiscoveryScan(el.dataset.scanId);
});
