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
 *  values list is drawn just before it. */
function fakeChart(w) {
  w.Chart = function Chart() { this.destroy = () => {}; };
}

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
  container.appendChild(holder);
  const row = new w.StubElement('div');
  row.dataset.deviceCheckToggle = String(checkId);
  row.nextElementSibling = holder;
  return { row, container, posts };
}

const REPORTED = [{ time: new Date().toISOString(), ok: true, latency_ms: 4, detail: 'up' }];

/** What the last loadDeviceChecks kept for a check: _loadedChecks is a const in
 *  the script's own scope, so this runs in that scope to put one there. */
function loaded(w, ...checks) {
  const body = checks.map((c) => `_loadedChecks.set(${JSON.stringify(String(c.id))}, ${JSON.stringify(c)});`).join('\n');
  w.$(body);
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
