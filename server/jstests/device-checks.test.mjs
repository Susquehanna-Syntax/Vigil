// The Checks section of the device editor (S9, phase 04a). A check's detail
// arrives from a poller that got it from the device: an HTML fragment has to
// land as the text it is, never as a node.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const PAYLOAD = '<img src=x onerror=alert(1)>';

function allElements(el, out = []) {
  out.push(el);
  el.children.forEach((c) => allElements(c, out));
  return out;
}

const CHECK = {
  id: 'c-1', kind: 'http', params: { url: 'http://shop.lan/health' },
  interval_seconds: 60, enabled: true, poller: null, created_at: null,
  latest: { ok: false, latency_ms: null, detail: PAYLOAD, time: '2026-11-17T03:00:00Z' },
};

/** The page's element lookups; the rest of the modal is not in this fixture.
 *  vigil-nav.js first, because that is where navigateTo comes from. */
function page() {
  const w = load('vigil-utils.js', 'vigil-nav.js', 'vigil-sitepick.js', 'vigil-devices.js');
  const byId = {};
  for (const id of ['device-checks', 'device-checks-list', 'device-check-add']) {
    byId[id] = new w.StubElement('div');
  }
  // The walk checkbox is a label around an input, exactly as the add row has it.
  byId['device-check-walk'] = new w.StubElement('label');
  byId['device-check-interfaces'] = new w.StubElement('input');
  byId['device-check-interfaces'].checked = false;
  for (const id of ['device-check-kind', 'device-check-target', 'device-check-port',
                    'device-check-community', 'device-check-oids', 'device-check-interval']) {
    byId[id] = new w.StubElement(id.endsWith('interval') || id.endsWith('kind') ? 'select' : 'input');
    byId[id].value = '';
  }
  byId['device-check-kind'].value = 'icmp';
  byId['device-check-interval'].value = '60';
  // openDeviceModal fills the whole form before it opens the Checks section, so
  // the fixture has to have a field for each one it touches.
  for (const id of ['device-modal-title', 'device-overlay', 'device-modal',
                    'device-delete-btn']) {
    byId[id] = new w.StubElement('div');
  }
  for (const id of ['device-id', 'device-name', 'device-type', 'device-ip', 'device-mac',
                    'device-vendor', 'device-location', 'device-serial', 'device-warranty',
                    'device-owner', 'device-extra', 'device-notes']) {
    byId[id] = new w.StubElement('input');
  }
  // The site picker is part of the form too (vigil-sitepick.js), and it reads
  // the install's site info from _sitePickInfo — resolved here so filling it
  // fetches nothing.
  byId['device-site'] = new w.StubElement('select');
  byId['device-site-field'] = new w.StubElement('div');
  w.$('_sitePickInfo = Promise.resolve({ enabled: false, global: false, sites: [] });');
  // The device list answers can_edit_checks, and the write controls come from
  // it — so a test says who may write by loading rows through loadDevices, as
  // the page does. That needs window: vigil-devices.js wraps the global
  // navigateTo, storing the wrapper on window (in a browser) or, here, leaving
  // it in the script's scope while window resolves to the context itself.
  w.$('window = globalThis;');
  w.document.getElementById = (id) => byId[id] || null;
  return { w, byId };
}

/** The device list endpoint is what answers can_edit_checks, and the write
 *  controls come from it — so tests say who may write by loading rows through
 *  loadDevices, exactly as the page does. Its own fetch is then replaced with
 *  the checks this page should draw. */
async function devicePage(devices, checks = [CHECK]) {
  const ctx = page();
  ctx.w.apiJson = async () => devices;
  await ctx.w.loadDevices();
  // The Checks section is a field of the device modal: openDeviceModal reveals
  // it, and addDeviceCheck does nothing at all until it knows the device. So a
  // page that writes checks opens one — any row the list says may be written.
  if (devices.some((d) => d && d.can_edit_checks)) ctx.w.openDeviceModal(devices[0].id);
  ctx.w.apiJson = async (url) => (url.includes('/results/') ? [] : checks);
  return ctx;
}

test('a check whose detail is markup renders as text, not an img', async () => {
  const { w, byId } = await devicePage([{ id: 'd-1', name: 'sw-01' }]);
  await w.loadDeviceChecks('d-1');

  const list = byId['device-checks-list'];
  assert.equal(allElements(list).some((e) => e.tagName === 'IMG'), false);
  const texts = allElements(list).map((e) => e.textContent);
  assert.ok(texts.includes(PAYLOAD), 'the detail should be there, as words');
  assert.ok(texts.some((t) => t.startsWith('HTTP http://shop.lan/health')));
});

test('a failing check gets the rose dot and no latency reads as a dash', async () => {
  const { w, byId } = await devicePage([{ id: 'd-1' }]);
  await w.loadDeviceChecks('d-1');
  const row = byId['device-checks-list'].children[0];
  assert.equal(row.children[0].className, 'device-check-dot device-check-dot--bad');
  assert.equal(row.children[2].textContent, '-');
});

test('a check with no results yet is neither up nor down', async () => {
  const { w, byId } = await devicePage([{ id: 'd-1' }], [{ ...CHECK, latest: null }]);
  await w.loadDeviceChecks('d-1');
  const row = byId['device-checks-list'].children[0];
  assert.equal(row.children[0].className, 'device-check-dot device-check-dot--none');
});

test('the write controls appear only where the server says checks may be written', async () => {
  const admin = await devicePage([{ id: 'd-1', can_edit_checks: true }]);
  await admin.w.loadDeviceChecks('d-1');
  const row = admin.byId['device-checks-list'].children[0];
  const actions = row.children[row.children.length - 1];
  assert.equal(actions.className, 'device-check-actions');
  assert.ok(actions.children.some((b) => b.dataset.act === 'deleteDeviceCheck'));
  assert.ok(actions.children.some((b) => b.getAttribute('role') === 'switch'));

  const viewer = await devicePage([{ id: 'd-2' }]);   // the list said nothing
  await viewer.w.loadDeviceChecks('d-2');
  const theirRow = viewer.byId['device-checks-list'].children[0];
  assert.equal(theirRow.children.some((c) => c.className === 'device-check-actions'), false);
});

test('the add form is hidden for someone the list says may not write', async () => {
  const admin = await devicePage([{ id: 'd-1', name: 'sw', can_edit_checks: true }]);
  admin.w.openDeviceModal('d-1');
  assert.equal(admin.byId['device-check-add'].hidden, false);
  assert.equal(admin.byId['device-checks'].hidden, false);

  const viewer = await devicePage([{ id: 'd-2', name: 'sw' }]);
  viewer.w.openDeviceModal('d-2');
  assert.equal(viewer.byId['device-check-add'].hidden, true);
});

test('a new device shows no checks at all — it has no id to hang one on', async () => {
  const { w, byId } = await devicePage([{ id: 'd-1', can_edit_checks: true }]);
  w.openDeviceModal(null);
  assert.equal(byId['device-checks'].hidden, true);
});

test('the port field is for TCP and SNMP only', () => {
  const { w, byId } = page();
  byId['device-check-kind'].value = 'tcp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, false);
  byId['device-check-kind'].value = 'dns';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, true);
});

test('an SNMP row shows the port, the community and the OIDs box', () => {
  const { w, byId } = page();
  byId['device-check-kind'].value = 'snmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, false);
  assert.equal(byId['device-check-port'].placeholder, 'Port (161)');
  assert.equal(byId['device-check-community'].hidden, false);
  assert.equal(byId['device-check-oids'].hidden, false);

  byId['device-check-kind'].value = 'icmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, true);
  assert.equal(byId['device-check-community'].hidden, true);
  assert.equal(byId['device-check-oids'].hidden, true);
});

test('_checkLabel names an SNMP port only when the server stored one', () => {
  const { w } = page();
  // The API stores no port for the default 161, so a plain host is the rule.
  assert.equal(w._checkLabel({ kind: 'snmp', params: { host: '10.0.0.2' } }), 'SNMP 10.0.0.2');
  assert.equal(w._checkLabel({ kind: 'snmp', params: { host: '10.0.0.2', port: 1161 } }),
               'SNMP 10.0.0.2:1161');
});

test('parseSnmpOids reads label = OID lines and names the line it cannot', () => {
  const { w } = page();
  const good = w.parseSnmpOids(
    'if_in = 1.3.6.1.2.1.2.2.1.10.1\n\n# the device name\nsysInfo=1.3.6.1.2.1.1.5.0\n');
  assert.equal(good.error, null);
  // Objects built in the script's realm have that realm's prototypes, so strict
  // deep-equal cannot see them as plain values; compare through JSON instead.
  assert.deepEqual(JSON.parse(JSON.stringify(good.oids)), [
    { label: 'if_in', oid: '1.3.6.1.2.1.2.2.1.10.1' },
    { label: 'sysInfo', oid: '1.3.6.1.2.1.1.5.0' },
  ]);

  // Only the first '=' splits; anything after it belongs to the OID side.
  assert.deepEqual(JSON.parse(JSON.stringify(w.parseSnmpOids('a = 1.3.6=7').oids)),
                   [{ label: 'a', oid: '1.3.6=7' }]);

  const bad = w.parseSnmpOids('if_in = 1.3.6\n# a note\nsysDescr\n');
  assert.deepEqual(JSON.parse(JSON.stringify(bad.oids)), []);
  // N counts every line, notes and blanks included: it is the one to fix.
  assert.equal(bad.error, 'Line 3: write label = OID');
});

/** Chart.js stands outside these tests: a row with results builds one, and the
 *  values list is drawn just before it. The calls are kept so a test can read
 *  what an interface's traffic chart was handed. */
function fakeChart(w) {
  w.__charts = [];
  w.Chart = function Chart(canvas, config) {
    this.destroy = () => {};
    w.__charts.push(config);
  };
}

/** A settled promise chain: the answers a row's own requests wait for are run
 *  unawaited, so a test has to let them land before it looks. */
const settled = () => new Promise((resolve) => setImmediate(resolve));

test('an empty results list points at the poller rather than a blank chart', async () => {
  const { w } = await devicePage([{ id: 'd-1' }]);
  const { row, container } = checkedRow(w, 'c-1', []);
  await w.toggleDeviceCheckChart(row);
  assert.equal(allElements(container).some((e) => e.tagName === 'IMG'), false);
  assert.ok(allElements(container).map((e) => e.textContent)
    .some((t) => t.includes('is a poller assigned?')));
});

/** One expanded row: the chart host plus the container that host was placed
 *  in. loadDeviceChecks appends a row and its chart host straight into
 *  #device-checks-list, so that list is the container they share and where an
 *  expanded row puts its values list. */
function checkedRow(w, checkId, results = [CHECK]) {
  // apiJson stands in for both the results request and the create a test may
  // or may not make; loadDeviceChecks would redraw the rows we hand-build.
  w.loadDeviceChecks = async () => {};
  const posts = [];
  w.apiJson = async (url, opts) => {
    if (opts && opts.method === 'POST') { posts.push({ url, body: JSON.parse(opts.body) }); return {}; }
    return results;
  };
  const container = new w.StubElement('div');
  const holder = new w.StubElement('div');
  holder.classList.add('device-check-chart');
  const row = new w.StubElement('div');
  row.dataset.deviceCheckToggle = String(checkId);
  // loadDeviceChecks appends a row and then its chart host into the one list;
  // that order is what an expansion reads `nextElementSibling` from.
  container.appendChild(row);
  container.appendChild(holder);
  return { row, container, posts };
}

const REPORTED = [{ time: new Date().toISOString(), ok: true, latency_ms: 4, detail: 'up' }];

/** What the last loadDeviceChecks kept for a check: _loadedChecks is a const in
 *  the script's own scope, so this runs in that scope to put one there. */
function loaded(w, ...checks) {
  const body = checks.map((c) => `_loadedChecks.set(${JSON.stringify(String(c.id))}, ${JSON.stringify(c)});`).join('\n');
  w.$(body);
}

/** The container the rows and everything an expansion draws go into: the
 *  element loadDeviceChecks would have filled in. `deviceId` is what it hangs a
 *  check path on, which is how a test reads the URLs back. */
function containerOf(w, deviceId = 'd-none') {
  w.$('_chartHost = document.createElement("div");');
  const list = w.$('_chartHost');
  // loadDeviceChecks sets this before any expansion can run; without it there
  // is no device to name in a check's path and no previous row to collapse.
  w.$(`_checkDeviceId = ${JSON.stringify(deviceId)};`);
  return list;
}

/** An SNMP check as the editor holds it, and the row drawn for it: the chart
 *  host sits in the shared container, right after its row, as loadDeviceChecks
 *  leaves every pair. `deviceId` is the one its URLs will name. Built with the
 *  page's own factory, so a click on any of it reaches what delegation bound. */
function snmpRow(w, params, extra = null, deviceId = 'd-none') {
  loaded(w, { id: 'c-if', kind: 'snmp', params, latest: extra ? { extra } : null });
  const list = containerOf(w, deviceId);
  const row = new w.StubElement('div');
  row.dataset.deviceCheckToggle = 'c-if';
  const holder = new w.StubElement('div');
  holder.classList.add('device-check-chart');
  // The pair in the order loadDeviceChecks puts them: an expansion finds its
  // chart host as the row's next element.
  list.appendChild(row);
  list.appendChild(holder);
  return { row, holder, list };
}

// _loadedChecks holds the checks loadDeviceChecks drew, and an expanded row
// finds its own there — which is what a chart with no check behind it would
// otherwise quietly miss.
test('the checks of the open device are readable back by id', async () => {
  const { w } = await devicePage([{ id: 'd-1', can_edit_checks: true }]);
  await w.loadDeviceChecks('d-1');
  assert.equal(w.$('_loadedChecks.get("c-1").kind'), 'http');
  assert.equal(w.$('_loadedChecks.has("nope")'), false);
});

test('an expanded SNMP row lists the values of its newest result', async () => {
  const { w } = page();
  fakeChart(w);
  // What loadDeviceChecks would have kept for this id, extras included.
  loaded(w, {
    id: 'c-1', kind: 'snmp', params: { host: '10.0.0.2' },
    latest: { extra: { kind: 'snmp', target: '10.0.0.2', sys_name: '<b>sw</b>',
                       uptime_s: 3 * 86400 + 4 * 3600 + 5 * 60, if_in: 91234, if_out: null } },
  });
  const { row, container } = checkedRow(w, 'c-1', REPORTED);
  await w.toggleDeviceCheckChart(row);

  const list = container.children.find((c) => c.tagName === 'DL');
  assert.ok(list, 'the expanded row carries no values list');
  // Right under its own chart, not at the foot of the list of rows.
  assert.equal(container.children.indexOf(list), container.children.findIndex((c) => c.classList.contains('device-check-chart')) + 1);
  assert.equal(list.className, 'device-check-values');
  // The two named ones first, then the rest in key order; kind and target are
  // what the row above already says. A value is text, never markup.
  assert.deepEqual(list.children.filter((c) => c.tagName === 'DT').map((c) => c.textContent),
                   ['Name', 'Uptime', 'if_in', 'if_out']);
  assert.deepEqual(list.children.filter((c) => c.tagName === 'DD').map((c) => c.textContent),
                   ['<b>sw</b>', '3d 4h 5m', '91234', '-']);
});

test('collapsing a row takes its values with the chart', async () => {
  const { w } = page();
  fakeChart(w);
  loaded(w, { id: 'c-1', kind: 'snmp', latest: { extra: { sys_name: 'sw-01' } } });
  const { row, container } = checkedRow(w, 'c-1', REPORTED);
  await w.toggleDeviceCheckChart(row);
  assert.ok(container.children.some((c) => c.tagName === 'DL'));
  await w.toggleDeviceCheckChart(row);   // the same row again closes it
  assert.equal(container.children.filter((c) => c.tagName === 'DL').length, 0);
});

test('a row of another kind gets the line and no values list', async () => {
  const { w } = page();
  fakeChart(w);
  loaded(w, { id: 'c-2', kind: 'icmp', latest: { extra: { sys_name: 'nope' } } });
  const { row, container } = checkedRow(w, 'c-2', REPORTED);
  await w.toggleDeviceCheckChart(row);
  assert.equal(container.children.filter((c) => c.tagName === 'DL').length, 0);
});

test('adding an SNMP check posts the community and the OIDs it was given', async () => {
  const { w, byId } = await devicePage([{ id: 'd-7', can_edit_checks: true }]);
  byId['device-check-kind'].value = 'snmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  byId['device-check-target'].value = '10.0.0.9';
  byId['device-check-port'].value = '1161';
  byId['device-check-community'].value = 's3cret';
  byId['device-check-oids'].value = 'if_in = 1.3.6.1.2.1.2.2.1.10.1';
  const posts = [];
  w.loadDeviceChecks = async () => {};
  // The success toast is vigil-utils' own, and it wants a node to hang itself
  // on; this fixture's document has none, and the toast is not what is tested.
  w.showToast = () => {};
  w.apiJson = async (url, opts) => { posts.push({ url, body: JSON.parse(opts.body) }); return {}; };

  await w.addDeviceCheck();

  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/v1/hosts/devices/d-7/checks/');
  assert.deepEqual(posts[0].body, {
    kind: 'snmp', interval_seconds: 60, community: 's3cret',
    params: { host: '10.0.0.9', port: 1161, oids: [{ label: 'if_in', oid: '1.3.6.1.2.1.2.2.1.10.1' }] },
  });
  // Cleared for the next one — a community left in the box is a leak waiting.
  assert.equal(byId['device-check-target'].value, '');
  assert.equal(byId['device-check-port'].value, '');
  assert.equal(byId['device-check-community'].value, '');
  assert.equal(byId['device-check-oids'].value, '');
});

test('a bad OIDs line is a toast and no request', async () => {
  const { w, byId } = await devicePage([{ id: 'd-8', can_edit_checks: true }]);
  byId['device-check-kind'].value = 'snmp';
  byId['device-check-target'].value = '10.0.0.9';
  byId['device-check-community'].value = 's3cret';
  byId['device-check-oids'].value = 'if_in = 1.3.6\nsysDescr\n';
  const posts = [];
  const toasts = [];
  w.loadDeviceChecks = async () => {};
  w.showToast = (message, kind) => { toasts.push([message, kind]); };
  w.apiJson = async (url, opts) => { posts.push(JSON.parse(opts.body)); return {}; };

  await w.addDeviceCheck();

  assert.equal(posts.length, 0);
  assert.deepEqual(toasts, [['Line 2: write label = OID', 'error']]);
});

/* ── The interface table, and one interface's traffic (phase 05f) ─────────── */
test('_fmtBps reads in the largest unit that still leaves a number of one', () => {
  const { w } = page();
  assert.equal(w._fmtBps(null), '-');
  assert.equal(w._fmtBps(0), '0 b/s');
  assert.equal(w._fmtBps(950), '950 b/s');
  assert.equal(w._fmtBps(80000), '80 kb/s');
  assert.equal(w._fmtBps(1500000), '1.5 Mb/s');
  assert.equal(w._fmtBps(2000000000), '2 Gb/s');
});

test('_fmtSpeed says Mb/s below a gigabit and Gb/s above it', () => {
  const { w } = page();
  assert.equal(w._fmtSpeed(null), '-');
  assert.equal(w._fmtSpeed(100), '100 Mb/s');
  assert.equal(w._fmtSpeed(1000), '1 Gb/s');
  assert.equal(w._fmtSpeed(2500), '2.5 Gb/s');
});

/** One point a day back: `time` is only ever read by Date, never asserted. */
const DAY = new Date(Date.now() - 86_400_000).toISOString();

const EXTRA = { kind: 'snmp', target: 'rtr', sys_name: 'rtr', uptime_s: 3600 };

const IFACES = [
  { time: DAY, index: 1, name: 'eth0', up: true, speed_mbps: 1000, in_bps: 80000, out_bps: null },
  { time: DAY, index: 7, name: '', up: false, speed_mbps: 100, in_bps: 950, out_bps: 0 },
  { time: DAY, index: 9, name: '<b>x</b>', up: null, speed_mbps: null, in_bps: null, out_bps: null },
];

test('the interface table says which interface it is, and what it did', () => {
  const { w } = page();
  const table = w._interfaceTable(IFACES);
  assert.equal(table.tagName, 'TABLE');
  assert.ok(table.classList.contains('device-check-ifaces'));
  // The header is the table's first child and its single row; the stub's
  // querySelector walks a node's whole subtree, so th cells are found that way.
  assert.deepEqual(table.querySelectorAll('th').map((th) => th.textContent),
                   ['Interface', 'State', 'Speed', 'In', 'Out']);

  const rows = table.querySelectorAll('tr[data-if-index]');
  assert.equal(rows.length, 3);                    // one per interface walked
  assert.deepEqual(rows.map((tr) => tr.dataset.ifIndex), ['1', '7', '9']);
  // An interface with no ifDescr is still worth a row: its index is the name.
  assert.deepEqual(rows.slice(0, 2).map((tr) => tr.children[0].textContent), ['eth0', 'if7']);
  const states = rows.map((tr) => tr.children[1].children[0]);
  assert.deepEqual(states.map((span) => span.textContent), ['Up', 'Down', '-']);
  assert.deepEqual(states.map((span) => [...span.classList]),
                  [['device-check-if-state', 'up'], ['device-check-if-state', 'down'], ['device-check-if-state']]);
  assert.deepEqual(rows.slice(0, 2).map((tr) => tr.children[2].textContent), ['1 Gb/s', '100 Mb/s']);
  assert.deepEqual(rows.slice(0, 2).map((tr) => tr.children[3].textContent), ['80 kb/s', '950 b/s']);
  assert.deepEqual(rows.slice(0, 2).map((tr) => tr.children[4].textContent), ['-', '0 b/s']);
});

test('an interface state nobody reported has no colour to carry', () => {
  const { w } = page();
  const table = w._interfaceTable([{ index: 3, name: 'x', up: null }]);
  const row = table.querySelectorAll('tr[data-if-index]')[0];
  // The state's own span, not the cell around it: querySelector walks children.
  const state = table.querySelector('.device-check-if-state');
  assert.equal(state.textContent, '-');
  assert.deepEqual([...state.classList], ['device-check-if-state']);
});

test('an interface name is text, never markup', () => {
  const { w } = page();
  const row = w._interfaceTable([{ index: 2, name: '<b>x</b>' }]).querySelectorAll('tr[data-if-index]')[0];
  assert.equal(row.children[0].textContent, '<b>x</b>');
  assert.equal(row.children[0].children.length, 0);
});

test('the walk checkbox shows for an SNMP check and nothing else', async () => {
  const { w, byId } = await devicePage([{ id: 'd-14', can_edit_checks: true }]);
  assert.equal(byId['device-check-walk'].hidden, true);   // the add row starts on icmp

  byId['device-check-kind'].value = 'snmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-walk'].hidden, false);

  byId['device-check-kind'].value = 'icmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-walk'].hidden, true);
});

test('the walk checkbox adds the param once, and clears itself', async () => {
  const { w, byId } = await devicePage([{ id: 'd-15', can_edit_checks: true }]);
  byId['device-check-kind'].value = 'snmp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  byId['device-check-target'].value = 'router1';
  const posts = [];
  w.loadDeviceChecks = async () => {};
  // The success toast is vigil-utils' own and wants a node to hang itself on;
  // this fixture's document has none, and the toast is not what is tested.
  w.showToast = () => {};
  w.apiJson = async (url, opts) => { posts.push(JSON.parse(opts.body)); return {}; };

  byId['device-check-interfaces'].checked = true;
  await w.addDeviceCheck();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].params.interfaces, true);
  // Back to unticked: the next check is not quietly a walking one.
  assert.equal(byId['device-check-interfaces'].checked, false);

  await w.addDeviceCheck();
  assert.equal(posts.length, 2);
  assert.ok(!('interfaces' in posts[1].params), JSON.stringify(posts[1].params));
});

test('expanding a walking SNMP check asks for its interfaces after the values', async () => {
  const { w } = await devicePage([{ id: 'd-16' }]);
  fakeChart(w);
  const { row, holder, list } = snmpRow(w, { host: 'rtr', interfaces: true }, EXTRA, 'd-16');
  const asked = [];
  w.apiJson = async (url) => {
    asked.push(url);
    return url.endsWith('results/?hours=24') ? [{ time: DAY, ok: true, latency_ms: 3 }] : IFACES;
  };

  await w.toggleDeviceCheckChart(row);
  assert.equal(holder.hidden, false);
  await settled();

  assert.deepEqual(asked, [
    '/api/v1/hosts/devices/d-16/checks/c-if/results/?hours=24',
    '/api/v1/hosts/devices/d-16/checks/c-if/interfaces/',
  ]);
  // Chart host, values list, then the interfaces, before any later row.
  const values = holder.nextElementSibling;
  assert.equal(values.tagName, 'DL');
  const box = values.nextElementSibling;
  assert.equal(box.children[0].tagName, 'TABLE');
  assert.ok(box.children[0].classList.contains('device-check-ifaces'));
  assert.equal(list.querySelectorAll('table.device-check-ifaces').length, 1);
});

test('the table comes after the chart host when the check has no values', async () => {
  const { w } = await devicePage([{ id: 'd-17' }]);
  fakeChart(w);
  const { row, holder } = snmpRow(w, { host: 'rtr', interfaces: true }, null, 'd-17');
  w.apiJson = async () => IFACES;

  await w.toggleDeviceCheckChart(row);
  await settled();
  assert.equal(holder.nextElementSibling.children[0].tagName, 'TABLE');
});

test('a check that does not walk the interfaces asks for none', async () => {
  const { w } = await devicePage([{ id: 'd-18' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr' }, EXTRA, 'd-18');
  const asked = [];
  w.apiJson = async (url) => {
    asked.push(url);
    return url.endsWith('results/?hours=24') ? [{ time: DAY, ok: true, latency_ms: 3 }] : IFACES;
  };

  await w.toggleDeviceCheckChart(row);
  await settled();
  assert.deepEqual(asked, ['/api/v1/hosts/devices/d-18/checks/c-if/results/?hours=24']);
  assert.equal(list.querySelectorAll('table.device-check-ifaces').length, 0);
});

test('no interfaces yet is said, not left blank', async () => {
  const { w } = await devicePage([{ id: 'd-19' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, null, 'd-19');
  w.apiJson = async (url) => (url.endsWith('results/?hours=24')
    ? [{ time: DAY, ok: true, latency_ms: 3 }] : []);

  await w.toggleDeviceCheckChart(row);
  await settled();
  const note = list.querySelector('.device-check-note');
  assert.ok(note, 'the empty answer has to explain itself');
  assert.equal(note.textContent, 'No interface data yet');
});

test('collapsing a row takes its interfaces with the chart', async () => {
  const { w } = await devicePage([{ id: 'd-20' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, EXTRA, 'd-20');
  w.apiJson = async (url) => (url.endsWith('results/?hours=24')
    ? [{ time: DAY, ok: true, latency_ms: 3 }] : IFACES);

  await w.toggleDeviceCheckChart(row);
  await settled();
  assert.equal(list.querySelectorAll('table.device-check-ifaces').length, 1);

  w.collapseDeviceCheckChart();
  assert.equal(list.querySelectorAll('table.device-check-ifaces').length, 0);
  assert.equal(list.querySelector('.device-check-note'), null);
});

test('an answer that lands after the row closed renders nothing', async () => {
  const { w } = await devicePage([{ id: 'd-21' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, null, 'd-21');
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  w.apiJson = async (url) => (url.endsWith('results/?hours=24')
    ? [{ time: DAY, ok: true, latency_ms: 3 }] : gate.then(() => IFACES));

  await w.toggleDeviceCheckChart(row);
  w.collapseDeviceCheckChart();      // the operator moved on mid-request
  release(IFACES);
  await settled();
  assert.equal(list.querySelectorAll('table.device-check-ifaces').length, 0);
});

test('clicking an interface draws its traffic, and clicking it again hides it', async () => {
  const { w } = await devicePage([{ id: 'd-22' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, EXTRA, 'd-22');
  // The click's own request is the one asked for by the row's index; the walk's
  // list request names no index, so the two never blur together here.
  const detail = [];
  const SERIES = [
    { time: DAY, in_bps: 80000, out_bps: null },
    { time: new Date(Date.now() - 60_000).toISOString(), in_bps: 2000000000, out_bps: 1500000 },
  ];
  w.apiJson = async (url) => {
    if (url.includes('/interfaces/') && !url.endsWith('/interfaces/')) detail.push(url);
    if (url.endsWith('results/?hours=24')) return [{ time: DAY, ok: true, latency_ms: 3 }];
    if (url.endsWith('/interfaces/')) return IFACES;
    return { index: 1, name: 'eth0', series: SERIES };
  };

  await w.toggleDeviceCheckChart(row);
  await settled();
  const table = list.querySelector('table.device-check-ifaces');
  const second = table.querySelectorAll('tr[data-if-index]')[1];
  await w._selectInterface(table, second);

  assert.deepEqual(detail, ['/api/v1/hosts/devices/d-22/checks/c-if/interfaces/7/?hours=24']);
  // The other two rows are there and none of them asked for anything.
  assert.equal(table.querySelectorAll('tr[data-if-index]').length, 3);
  const chart = w.__charts[w.__charts.length - 1];
  assert.equal(chart.type, 'line');
  assert.deepEqual([...chart.data.datasets.map((d) => d.label)], ['In', 'Out']);
  assert.deepEqual([...chart.data.datasets[0].data.map((pt) => pt.y)], [80000, 2000000000]);
  // A rate nobody worked out is a gap in the line, not a zero on the axis.
  assert.deepEqual([...chart.data.datasets[1].data.map((pt) => pt.y)], [null, 1500000]);
  assert.equal(chart.options.plugins.legend.display, true);
  assert.equal(chart.options.scales.y.ticks.callback(80000), '80 kb/s');

  const host = table.nextElementSibling;
  assert.ok(host.classList.contains('device-check-if-chart'));
  // A click anywhere inside a row is that row's: exactly one row says it is
  // selected, and the chart sits directly after the table rather than elsewhere.
  assert.equal(table.querySelectorAll('tr.selected').length, 1);
  assert.equal(second.classList.contains('selected'), true);

  // The row again: chart gone, nothing asked for.
  await w._selectInterface(table, second);
  assert.equal(list.querySelectorAll('.device-check-if-chart').length, 0);
  assert.equal(table.querySelectorAll('tr.selected').length, 0);
  assert.equal(detail.length, 1);
});

test('a second interface takes the chart from the first', async () => {
  const { w } = await devicePage([{ id: 'd-23' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, null, 'd-23');
  w.apiJson = async (url) => (url.endsWith('results/?hours=24')
    ? [{ time: DAY, ok: true, latency_ms: 3 }]
    : (url.endsWith('/interfaces/') ? IFACES : { index: 1, name: 'x', series: [] }));

  await w.toggleDeviceCheckChart(row);
  await settled();
  const table = list.querySelector('table.device-check-ifaces');
  const rows = table.querySelectorAll('tr[data-if-index]');
  await w._selectInterface(table, rows[0]);
  await w._selectInterface(table, rows[1]);

  assert.equal(list.querySelectorAll('.device-check-if-chart').length, 1);
  assert.equal(table.querySelectorAll('tr.selected').length, 1);
  assert.equal(rows[1].classList.contains('selected'), true);
});

test('an interface whose traffic cannot be read says why where the chart was', async () => {
  const { w } = await devicePage([{ id: 'd-24' }]);
  fakeChart(w);
  const { row, list } = snmpRow(w, { host: 'rtr', interfaces: true }, null, 'd-24');
  w.apiJson = async (url) => {
    if (url.endsWith('results/?hours=24')) return [{ time: DAY, ok: true, latency_ms: 3 }];
    if (url.endsWith('/interfaces/')) return IFACES;
    throw new Error('no such interface');
  };

  await w.toggleDeviceCheckChart(row);
  await settled();
  const table = list.querySelector('table.device-check-ifaces');
  const tr = list.querySelector('tr[data-if-index]');
  await w._selectInterface(table, tr);

  const note = list.querySelector('.device-check-note');
  assert.ok(note);
  assert.equal(note.textContent, 'no such interface');
  assert.equal(list.querySelectorAll('.device-check-if-chart').length, 0);
});
