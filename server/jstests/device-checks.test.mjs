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
  byId['device-check-kind'] = new w.StubElement('select');
  byId['device-check-kind'].value = 'icmp';
  byId['device-check-port'] = new w.StubElement('input');
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

test('the port field is for TCP alone', () => {
  const { w, byId } = page();
  byId['device-check-kind'].value = 'tcp';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, false);
  byId['device-check-kind'].value = 'dns';
  w.setDeviceCheckKind(byId['device-check-kind']);
  assert.equal(byId['device-check-port'].hidden, true);
});

test('an empty results list points at the poller rather than a blank chart', async () => {
  const { w } = await devicePage([{ id: 'd-1' }]);
  const holder = new w.StubElement('div');
  holder.classList.add('device-check-chart');
  const row = new w.StubElement('div');
  row.dataset.deviceCheckToggle = 'c-1';
  row.nextElementSibling = holder;
  await w.toggleDeviceCheckChart(row);
  assert.equal(allElements(holder).some((e) => e.tagName === 'IMG'), false);
  assert.ok(allElements(holder).map((e) => e.textContent)
    .some((t) => t.includes('is a poller assigned?')));
});
