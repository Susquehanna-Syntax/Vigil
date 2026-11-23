// Settings > Agent Binary: the fleet versions block. A reason that came from an
// agent is shown as text, never parsed as markup.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

function allElements(el, out = []) {
  out.push(el);
  el.children.forEach((c) => allElements(c, out));
  return out;
}

test('a rolled-back reason from an agent renders as text, not an img element', async () => {
  const w = load('vigil-utils.js', 'vigil-settings.js');
  const host = new w.StubElement('div');
  w.document.getElementById = (id) => (id === 'fleet-versions' ? host : null);
  const payload = '<img src=x onerror=alert(1)>';
  w.apiJson = async () => ({
    expected: '2026.17.0',
    total: 3,
    outdated_hosts: 1,
    versions: [
      { version: '2026.17.0', hosts: 2, current: true, outdated: false },
      { version: '2026.16.0', hosts: 1, current: false, outdated: true },
    ],
    updates: { confirmed: 0, rolled_back: 1, not_applied: 0 },
    rolled_back: [{ host_id: 'x', hostname: 'web-1', from_version: '2026.16.0',
                    to_version: '2026.17.0', reason: payload, at: null }],
  });
  await w.loadFleetVersions();
  const tags = allElements(host).map((e) => e.tagName);
  assert.equal(tags.includes('IMG'), false);
  const texts = allElements(host).map((e) => e.textContent);
  assert.ok(texts.includes(payload));
  assert.ok(texts.includes('Rolled back in the last 30 days'));
  assert.ok(texts.includes('web-1: v2026.17.0 back to v2026.16.0'));
});

test('a failed request leaves the block empty and does not throw', async () => {
  const w = load('vigil-utils.js', 'vigil-settings.js');
  const host = new w.StubElement('div');
  w.document.getElementById = (id) => (id === 'fleet-versions' ? host : null);
  w.apiJson = async () => { throw new Error('403'); };
  await w.loadFleetVersions();
  assert.equal(host.children.length, 0);
  assert.equal(host.textContent, '');
});
