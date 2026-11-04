// The site picker (S1 04e-5) the create forms share: what it offers, when it
// shows, and what it sends.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

function picker(info) {
  const w = load('vigil-utils.js', 'vigil-sitepick.js');
  w.$(`_sitePickInfo = Promise.resolve(${JSON.stringify(info)})`);
  const select = new w.StubElement('select');
  const field = new w.StubElement('div');
  field.hidden = true;
  return { w, select, field };
}

const WEST = { id: 'w', name: 'West' };
const LAB = { id: 'l', name: 'Lab <x>' };

test('a site admin gets their sites, no Global, and the first one chosen', async () => {
  const { w, select, field } = picker({ enabled: true, global: false, sites: [WEST, LAB] });
  await w.sitePickFill(select, field, null);
  assert.equal(field.hidden, false);
  assert.ok(!select.innerHTML.includes('Global'));
  assert.ok(select.innerHTML.includes('Lab &lt;x&gt;'));     // names are escaped
  assert.equal(select.value, 'w');
});

test('an instance admin also gets Global, and Global is chosen for a new item', async () => {
  const { w, select, field } = picker({ enabled: true, global: true, sites: [WEST] });
  await w.sitePickFill(select, field, null);
  assert.ok(select.innerHTML.startsWith('<option value="" selected>Global (every site)</option>'));
});

test("editing keeps the row's own site listed even where the user cannot create", async () => {
  const { w, select, field } = picker({ enabled: true, global: false, sites: [WEST] });
  await w.sitePickFill(select, field, null, { editing: true });
  assert.ok(select.innerHTML.includes('Global (every site)'));   // the truth about a global row
  const other = picker({ enabled: true, global: false, sites: [WEST] });
  await other.w.sitePickFill(other.select, other.field, 'zz', { editing: true, currentName: 'North' });
  assert.ok(other.select.innerHTML.includes('<option value="zz" selected>North</option>'));
});

test('with no sites the field stays hidden and nothing is sent', async () => {
  const { w, select, field } = picker({ enabled: false, global: true, sites: [] });
  await w.sitePickFill(select, field, null);
  assert.equal(field.hidden, true);
  assert.equal(w.sitePickValue(select, field), undefined);
});

test('sitePickValue sends an id, or null for Global', () => {
  const { w, select, field } = picker({ enabled: true, global: true, sites: [] });
  field.hidden = false;
  select.value = 'w';
  assert.equal(w.sitePickValue(select, field), 'w');
  select.value = '';
  assert.equal(w.sitePickValue(select, field), null);
});

test('sitePickChoose asks only when there is a real choice', async () => {
  let { w } = picker({ enabled: false, global: true, sites: [] });
  assert.equal(await w.sitePickChoose('x'), undefined);
  ({ w } = picker({ enabled: true, global: false, sites: [WEST] }));
  assert.equal(await w.sitePickChoose('x'), 'w');
  ({ w } = picker({ enabled: true, global: true, sites: [] }));
  assert.equal(await w.sitePickChoose('x'), null);
});
