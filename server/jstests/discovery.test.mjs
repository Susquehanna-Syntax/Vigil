// The Discover dialog (S9, phase 06e). A scan's cidr, its poller's hostname and
// every found host arrive from a poller that got them from a device: the rows
// have to land as the text they are, never as nodes. The pure helpers — the
// ports box parser, the state words and a row's own line — are tested directly;
// the dialog's drawing runs against the harness's stub DOM through openDiscovery.
import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { load as loadScripts } from './harness.mjs';

// Every window a test loads, so each test ends with its dialog closed: an open
// dialog with a waiting scan keeps a real five-second poll alive, which would
// hold the process open after the last test.
const windows = [];
function load(...files) {
  const w = loadScripts(...files);
  windows.push(w);
  return w;
}
afterEach(() => {
  for (const w of windows.splice(0)) if (typeof w.closeDiscovery === 'function') w.closeDiscovery();
});

const POLLERS = '/api/v1/hosts/discovery/pollers/';
const SCANS = '/api/v1/hosts/discovery/scans/';

/** A settled promise chain: what the dialog fetches is drawn in microtasks. */
const settled = () => new Promise((resolve) => setImmediate(resolve));

async function twoTurns() {
  await settled();
  await settled();
}

const POLLER = { id: 'p-1', hostname: 'poll-1', site_id: null, online: true, can_scan: true };
const WATCHED = { id: 'p-2', hostname: 'old-1', site_id: null, online: false, can_scan: false };

const QUEUED = {
  id: 's-queued', poller: { id: 'p-1', hostname: 'poll-1' }, cidr: '10.0.0.0/24', ports: [22],
  state: 'queued', created_at: '2026-11-17T03:00:00Z', sent_at: null, finished_at: null,
  error: '', scanned: 0, truncated: false, snmp: false, found: 0, requested_by: 'admin',
};
const RUNNING = { ...QUEUED, id: 's-sent', state: 'sent', cidr: '192.168.7.0/24' };
const DONE = {
  ...QUEUED, id: 's-done', state: 'done', sent_at: '2026-11-17T03:00:05Z',
  finished_at: '2026-11-17T03:00:40Z', scanned: 254, found: 3, truncated: true,
  hosts: [
    {
      id: 1, ip: '10.0.0.9', alive_by: 'icmp+snmp', latency_ms: 12.4, open_ports: [22, 80],
      name: '', sys_name: 'switch-a', sys_descr: 'Cisco IOS 15.2(7)E3', known: null,
    },
    {
      // A host that is already a device, so a test can see its row is there to
      // be read and not to be ticked.
      id: 2, ip: '10.0.0.7', alive_by: 'arp', latency_ms: null, open_ports: [],
      name: 'printer-7', sys_name: '', sys_descr: '',
      known: { id: 'd-3', name: 'Office printer' },
    },
  ],
};
const FAILED = { ...QUEUED, id: 's-failed', state: 'failed', error: 'poller went offline' };

/** The page's element lookups; the rest of the Inventory page is not here. */
function page() {
  const w = load('vigil-utils.js', 'vigil-discovery.js');
  const byId = {};
  for (const id of ['discovery-overlay', 'discovery-modal', 'discovery-scans', 'discovery-result']) {
    byId[id] = new w.StubElement('div');
  }
  byId['discovery-form'] = new w.StubElement('form');
  byId['discovery-help'] = new w.StubElement('div');
  byId['discovery-poller'] = new w.StubElement('select');
  for (const id of ['discovery-cidr', 'discovery-ports', 'discovery-community']) {
    byId[id] = new w.StubElement('input');
    byId[id].value = '';
  }
  // The Add Device modal's type pick, which the dialog copies rather than repeats.
  byId['device-type'] = new w.StubElement('select');
  for (const [value, text] of [['router', 'Router'], ['other', 'Other']]) {
    const option = new w.StubElement('option');
    option.value = value;
    option.setAttribute('value', value);
    option.textContent = text;
    byId['device-type'].appendChild(option);
  }
  // The toast box, so a failure path that toasts does not throw on its way past.
  byId['toast-container'] = new w.StubElement('div');
  // The Inventory table beside the dialog is vigil-devices.js's to draw, and it
  // reloads when this one adds devices to it. Held on window because that is the
  // registry the real page uses: loadDevices lives in another script file and the
  // scripts here run in one shared global scope, as they do in a browser.
  w.__reloads = [];
  w.$('window.loadDevices = async () => { window.__reloads.push("devices"); };');
  w.document.getElementById = (id) => byId[id] || null;
  return { w, byId };
}

/** The dialog as the page opens it: two answers, then both are drawn. */
async function opened(pollers, scans) {
  const ctx = page();
  ctx.w.apiJson = async (url) => (url === POLLERS ? pollers : scans);
  await ctx.w.openDiscovery();
  await twoTurns();
  return ctx;
}

/** The answers as they stand once a scan is open on it. */
function withDetail(w, list, detail) {
  const calls = [];
  w.apiJson = async (url, opts) => {
    if (opts && opts.method === 'DELETE') return {};
    if (url === POLLERS) return [POLLER];
    if (url === SCANS) return list;
    if (url.startsWith(SCANS)) { calls.push(url); return detail; }
    throw new Error(`unexpected ${url}`);
  };
  return calls;
}

/** The dialog with one scan open on it. */
async function showing(scan, { list = [scan] } = {}) {
  const ctx = await opened([POLLER], list);
  const details = withDetail(ctx.w, list, scan);
  await ctx.w.openDiscoveryScan(scan.id);
  await twoTurns();
  return { ...ctx, details };
}

/** The toast box, replaced with a list of what the dialog said. */
function toastsOf(w) {
  const seen = [];
  w.showToast = (message, type) => seen.push([type || '', message]);
  return seen;
}

/* ── The pure helpers ────────────────────────────────────────────────────── */

test('a ports box of numbers separated either way parses to numbers', () => {
  const w = load('vigil-utils.js', 'vigil-discovery.js');
  assert.deepEqual([...w.parseDiscoveryPorts('22, 80 443')], [22, 80, 443]);
  assert.deepEqual([...w.parseDiscoveryPorts('  22,, 80 ')], [22, 80]);
  // Empty means the poller's own default set, which is not a refusal.
  assert.deepEqual([...w.parseDiscoveryPorts('')], []);
  assert.deepEqual([...w.parseDiscoveryPorts(null)], []);
});

test('a ports box naming anything but a number is refused as a whole', () => {
  const w = load('vigil-utils.js', 'vigil-discovery.js');
  for (const raw of ['22,x', 'ssh', '22-80', '1e3', '-1', '22 http']) {
    assert.equal(w.parseDiscoveryPorts(raw), null, `${raw} was not refused`);
  }
});

test('a scan state is a word an operator reads', () => {
  const w = load('vigil-utils.js', 'vigil-discovery.js');
  assert.equal(w.$('SCAN_STATES.queued'), 'Waiting');
  assert.equal(w.$('SCAN_STATES.sent'), 'Running');
  assert.equal(w.$('SCAN_STATES.done'), 'Done');
  assert.equal(w.$('SCAN_STATES.failed'), 'Failed');
  // A state this script has not heard of shows its own word rather than nothing.
  assert.equal(w.$('_discoveryStateWord({ state: "cancelled" })'), 'cancelled');
});

test('a done scan says what it found and a failed one says why', () => {
  const w = load('vigil-utils.js', 'vigil-discovery.js');
  assert.equal(w.discoveryScanRowText(DONE), '10.0.0.0/24 · poll-1 · Done');
  assert.equal(w.discoveryScanTailText(DONE), '3 found of 254');
  assert.equal(w.discoveryScanRowText(FAILED), '10.0.0.0/24 · poll-1 · Failed');
  assert.equal(w.discoveryScanTailText(FAILED), 'poller went offline');
  // Still waiting has nothing to report yet, and its row says so by saying no more.
  assert.equal(w.discoveryScanTailText(QUEUED), '');
});

/* ── The scan list ───────────────────────────────────────────────────────── */

test('the scan form is filled with the pollers a scan may start from', async () => {
  const { w, byId } = await opened([POLLER, WATCHED], [QUEUED]);
  assert.equal(byId['discovery-form'].hidden, false);
  assert.equal(byId['discovery-help'].hidden, false);
  const options = byId['discovery-poller'].children;
  assert.deepEqual([...options].map((o) => o.textContent), ['poll-1', 'old-1 (offline)']);
  // The one this user may not scan from is there to be seen and not to be picked.
  assert.equal(options[1].disabled, true);
  assert.equal(byId['discovery-poller'].value, 'p-1');
});

test('a viewer gets the dialog without a way to queue anything', async () => {
  const scan = { ...DONE, truncated: false };
  const ctx = await opened([{ ...POLLER, can_scan: false }], []);
  assert.equal(ctx.byId['discovery-form'].hidden, true);
  assert.equal(ctx.byId['discovery-help'].hidden, true);
  // Reading is not the same as writing: a scan this user opens still says what it
  // found, and says it without either of the two buttons that would change things.
  withDetail(ctx.w, [scan], scan);
  await ctx.w.openDiscoveryScan('s-done');
  await twoTurns();

  const result = ctx.byId['discovery-result'];
  assert.equal(result.querySelector('.discovery-result-title').textContent, '10.0.0.0/24 · Done');
  assert.equal(result.querySelector('[data-act="adoptDiscoveryHosts"]'), null);
  assert.equal(result.querySelector('[data-act="deleteDiscoveryScan"]'), null);
});

test('no poller at all says how to get one', async () => {
  const { byId } = await opened([], []);
  // The scans block is the one that has something to say, and it survives the
  // scan load that runs alongside the empty picker.
  assert.equal(byId['discovery-scans'].children.length, 1);
  assert.equal(byId['discovery-form'].hidden, true);
});

test('no scans says no scans', async () => {
  const { byId } = await opened([POLLER], []);
  assert.equal(byId['discovery-scans'].querySelector('.discovery-note').textContent, 'No scans yet.');
});

test('a row per scan, newest first, each one a button', async () => {
  const { byId } = await opened([POLLER], [DONE, FAILED, RUNNING]);
  const rows = byId['discovery-scans'].querySelectorAll('.discovery-scan-row');
  assert.deepEqual([...rows].map((r) => r.dataset.scanId), ['s-done', 's-failed', 's-sent']);
  assert.deepEqual([...rows].map((r) => r.type), ['button', 'button', 'button']);
  const [done, failed, running] = rows;
  assert.equal(done.querySelector('.discovery-scan-cidr').textContent, '10.0.0.0/24');
  assert.equal(done.querySelector('.discovery-scan-state').textContent, 'Done');
  assert.equal(done.querySelector('.discovery-scan-meta').textContent, 'poll-1 · 3 found of 254');
  assert.equal(failed.querySelector('.discovery-scan-meta').textContent, 'poll-1 · poller went offline');
  // A pill per state, tinted by class rather than by anything the script styles.
  assert.equal(done.querySelector('.discovery-scan-state--done') !== null, true);
  assert.equal(running.querySelector('.discovery-scan-state--sent') !== null, true);
  assert.equal(failed.querySelector('.discovery-scan-state--failed') !== null, true);
});

/* ── Starting a scan ─────────────────────────────────────────────────────── */

test('a queued scan sends the poller and the network, and clears the boxes', async () => {
  const { w, byId } = await opened([POLLER], []);
  const posts = [];
  w.apiJson = async (url, opts) => {
    if (url === SCANS && opts && opts.method === 'POST') {
      posts.push(JSON.parse(opts.body));
      return QUEUED;
    }
    if (url === SCANS) return [QUEUED];
    throw new Error(`unexpected ${url}`);
  };
  byId['discovery-cidr'].value = ' 10.20.0.0/24 ';
  byId['discovery-ports'].value = '22, 80 443';
  byId['discovery-community'].value = ' private-public ';
  const toasts = toastsOf(w);

  await w.startDiscoveryScan({ preventDefault() {} });
  await twoTurns();

  assert.deepEqual(posts, [{ poller: 'p-1', cidr: '10.20.0.0/24', ports: [22, 80, 443],
    community: ' private-public ' }]);
  assert.deepEqual([...toasts.map((t) => t[1])], ['Scan queued']);
  // The community is gone from the page: the box was the only place it stood.
  assert.equal(byId['discovery-community'].value, '');
  assert.equal(byId['discovery-cidr'].value, '');
  assert.equal(byId['discovery-ports'].value, '');
  const rows = byId['discovery-scans'].querySelectorAll('.discovery-scan-row');
  assert.deepEqual([...rows].map((r) => r.dataset.scanId), ['s-queued']);
});

test('an empty ports box and an empty community are left out of the body', async () => {
  const { w, byId } = await opened([POLLER], []);
  const posts = [];
  w.apiJson = async (url, opts) => {
    if (opts && opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return QUEUED; }
    return url === SCANS ? [] : [POLLER];
  };
  byId['discovery-cidr'].value = '10.30.0.0/24';

  await w.startDiscoveryScan({ preventDefault() {} });
  await twoTurns();

  assert.deepEqual(posts, [{ poller: 'p-1', cidr: '10.30.0.0/24' }]);
});

test('a ports box naming anything but a number is refused before anything is sent', async () => {
  const { w, byId } = await opened([POLLER], []);
  const calls = [];
  w.apiJson = async (url, opts) => {
    calls.push(url + ((opts && opts.method) ? ' ' + opts.method : ''));
    return url === SCANS ? [] : [POLLER];
  };
  const toasts = toastsOf(w);
  byId['discovery-cidr'].value = '10.40.0.0/24';
  byId['discovery-ports'].value = '22,ssh';

  await w.startDiscoveryScan({ preventDefault() {} });
  await twoTurns();

  assert.deepEqual(calls, []);
  assert.deepEqual([...toasts.map((t) => t[1])], ['Ports must be numbers, separated by commas']);
  // What was typed stays put to be corrected.
  assert.equal(byId['discovery-ports'].value, '22,ssh');
});

test('the server sentence about a refused scan is the toast', async () => {
  const { w } = await opened([POLLER], []);
  w.apiJson = async (url, opts) => {
    if (opts && opts.method === 'POST') {
      const err = new Error('only private networks can be scanned');
      err.status = 400;
      throw err;
    }
    return url === SCANS ? [] : [POLLER];
  };
  const toasts = toastsOf(w);

  await w.startDiscoveryScan({ preventDefault() {} });
  await twoTurns();

  assert.deepEqual([...toasts], [['error', 'only private networks can be scanned']]);
});

/* ── One scan's hosts ────────────────────────────────────────────────────── */

test('an open scan is a header line and one row per found host', async () => {
  const { byId } = await showing(DONE);
  const result = byId['discovery-result'];
  assert.equal(result.hidden, false);
  assert.equal(result.querySelector('.discovery-result-title').textContent,
    '10.0.0.0/24 · Done · Stopped at 1024 hosts');
  // The dialog is 560px wide and the table wants more: the wrapper scrolls.
  assert.ok(result.querySelector('.discovery-table-wrap'));

  const rows = result.querySelectorAll('tbody tr');
  assert.equal(rows.length, 2);
  const [first, second] = rows;
  // A device's own description is a title, never markup of its own.
  assert.equal(first.getAttribute('title'), 'Cisco IOS 15.2(7)E3');
  assert.deepEqual([...first.children].map((td) => td.textContent),
    ['', '10.0.0.9', 'switch-a', 'icmp+snmp', '22, 80', '12.4 ms']);
  assert.equal(second.children[5].textContent, '-');
  // A device already there says which, on the second line of the name cell.
  assert.equal(second.children[2].children.length, 1);
  assert.equal(second.children[2].children[0].classList.contains('discovery-known'), true);
  assert.equal(second.children[2].children[0].textContent, 'Already a device: Office printer');

  const boxes = result.querySelectorAll('.discovery-pick');
  assert.deepEqual([...boxes].map((b) => b.dataset.hostId), ['1', '2']);
  // A host that is already a device cannot be added again.
  assert.deepEqual([...boxes].map((b) => b.disabled), [false, true]);
});

test('the type pick is the Add Device modal’s, copied rather than repeated', async () => {
  const { byId } = await showing(DONE);
  const type = byId['discovery-result'].querySelector('#discovery-type');
  assert.deepEqual([...type.children].map((o) => o.value), ['router', 'other']);
  assert.deepEqual([...type.children].map((o) => o.textContent), ['Router', 'Other']);
});

test('the add button counts what is ticked and is disabled with nothing ticked', async () => {
  const { w, byId } = await showing(DONE);
  const result = byId['discovery-result'];
  const adopt = result.querySelector('[data-act="adoptDiscoveryHosts"]');
  assert.equal(adopt.disabled, true);
  assert.equal(adopt.textContent, 'Add as devices');

  const [first, second] = result.querySelectorAll('.discovery-pick');
  first.check();
  assert.equal(adopt.disabled, false);
  assert.equal(adopt.textContent, 'Add 1 as devices');

  // "Select all new" takes every box that is not already a device.
  await w.selectAllDiscoveryHosts();
  await twoTurns();
  assert.deepEqual([first.checked, second.checked], [true, false]);
  assert.equal(adopt.textContent, 'Add 1 as devices');
});

test('adding the ticked hosts posts their ids and the type, then reloads both lists', async () => {
  const { w, byId } = await showing(DONE);
  const calls = [];
  w.apiJson = async (url, opts) => {
    if (url === `${SCANS}s-done/adopt/`) {
      calls.push({ url, body: JSON.parse(opts.body) });
      return { created: [{ id: 'd-9', name: 'switch-a', ip_address: '10.0.0.9' }], skipped: 1 };
    }
    if (url === `${SCANS}s-done/`) return DONE;
    throw new Error(`unexpected ${url}`);
  };
  const toasts = toastsOf(w);

  const adopt = byId['discovery-result'].querySelector('[data-act="adoptDiscoveryHosts"]');
  const [first] = byId['discovery-result'].querySelectorAll('.discovery-pick');
  first.check();
  await w.adoptDiscoveryHosts({}, adopt);
  await twoTurns();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${SCANS}s-done/adopt/`);
  assert.deepEqual([...calls[0].body.hosts], [1]);   // numbers, as the API names hosts
  // Whatever the Add Device modal has selected is what goes; here, its default.
  assert.equal(typeof calls[0].body.device_type, 'string');
  assert.deepEqual([...toasts.map((t) => t[1])], ['Added 1 device, 1 already known']);
  // The row is redrawn from the answer: that host is a device now.
  const boxes = byId['discovery-result'].querySelectorAll('.discovery-pick');
  assert.deepEqual([...boxes].map((b) => b.checked), [false, false]);
  // The Inventory table beside the dialog shows them too.
  assert.deepEqual(w.__reloads, ['devices']);
});

test('a scan with nothing to show says so', async () => {
  const { byId } = await showing({ ...DONE, hosts: [], found: 0 });
  const result = byId['discovery-result'];
  assert.equal(result.querySelector('.discovery-note').textContent, 'Nothing answered.');
  assert.equal(result.querySelector('table'), null);
});

test('a scan that has not finished yet has no add row', async () => {
  const { byId } = await showing(RUNNING);
  const result = byId['discovery-result'];
  assert.equal(result.querySelector('[data-act="adoptDiscoveryHosts"]'), null);
  assert.ok(result.querySelector('[data-act="deleteDiscoveryScan"]'));
});

/* ── Deleting a scan, and reading failures ───────────────────────────────── */

test('deleting a scan clears the result and reloads the list', async () => {
  const { w, byId } = await showing(DONE);
  const calls = [];
  w.apiJson = async (url, opts) => {
    if (opts && opts.method === 'DELETE') { calls.push(['DELETE', url]); return {}; }
    return url === SCANS ? [] : [POLLER];
  };

  await w.deleteDiscoveryScan();
  await twoTurns();

  assert.deepEqual(calls, [['DELETE', `${SCANS}s-done/`]]);
  assert.equal(byId['discovery-result'].hidden, true);
  assert.equal(byId['discovery-scans'].querySelector('.discovery-note').textContent, 'No scans yet.');
});

test('a scan list that cannot be read says so', async () => {
  const { w, byId } = await opened([POLLER], [QUEUED]);
  w.apiJson = async () => { throw new Error('gateway timeout'); };

  await w._loadDiscoveryScans();
  await twoTurns();

  assert.equal(byId['discovery-scans'].querySelector('.discovery-note').textContent,
    'Could not load scans.');
});

test('a scan that cannot be read says so', async () => {
  const { w, byId } = await opened([POLLER], [QUEUED]);
  w.apiJson = async (url) => {
    if (url.startsWith(SCANS)) throw new Error('no such scan');
    return [POLLER];
  };

  await w.openDiscoveryScan('s-gone');
  await twoTurns();

  assert.equal(byId['discovery-result'].querySelector('.discovery-note').textContent,
    'Could not load this scan.');
});

/* ── Polling while a scan waits ──────────────────────────────────────────── */

test('the poll runs while a scan waits and stops when none does', async () => {
  const { w } = await opened([POLLER], [QUEUED]);
  assert.equal(w.$('_discoveryTimer !== null'), true);
  assert.equal(w.$('_discoveryWaiting(_discoveryScans)'), true);

  const finished = await opened([POLLER], [DONE]);
  assert.equal(finished.w.$('_discoveryTimer'), null);
  assert.equal(finished.w.$('_discoveryWaiting(_discoveryScans)'), false);
});

test('closing the dialog stops the poll', async () => {
  const { w } = await opened([POLLER], [QUEUED]);
  assert.equal(w.$('_discoveryTimer !== null'), true);
  w.closeDiscovery();
  assert.equal(w.$('_discoveryTimer'), null);
});

test('a scan that finished while the dialog was open is re-read', async () => {
  // The dialog shows a running scan; the next poll's list says it is done, so
  // its hosts are fetched once more and drawn without anyone clicking.
  const { w } = await opened([POLLER], [RUNNING]);
  const details = [];
  let scans = [RUNNING];
  let detail = { ...RUNNING, hosts: [] };
  w.apiJson = async (url) => {
    if (url === SCANS) return scans;
    details.push(url);
    return detail;
  };
  await w.openDiscoveryScan(RUNNING.id);
  await twoTurns();
  assert.equal(details.length, 1);

  scans = [{ ...DONE, id: RUNNING.id }];
  detail = { ...DONE, id: RUNNING.id };
  await w.$('_loadDiscoveryScans()');
  await twoTurns();
  assert.equal(details.length, 2, 'the finished scan was not re-read');
  assert.equal(w.$('_discoveryScanState'), 'done');
  const rows = w.document.getElementById('discovery-result').querySelectorAll('tbody tr');
  assert.equal(rows.length, 2);

  // A further poll with nothing changed reads the list only.
  await w.$('_loadDiscoveryScans()');
  await twoTurns();
  assert.equal(details.length, 2);
});
