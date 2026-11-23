// loadingHtml / emptyHtml / stateRowHtml: the loading and empty states every list shares.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js');

test('loading is a polite status with the default or given label', () => {
  assert.equal(w.loadingHtml(), '<div class="state-loading" role="status" aria-live="polite">Loading…</div>');
  assert.match(w.loadingHtml('Working out what would change…'), />Working out what would change…</);
  assert.match(w.loadingHtml('<b>x</b>'), /&lt;b&gt;x&lt;\/b&gt;/);
});

test('empty is the page block by default and the inline note when compact', () => {
  assert.equal(w.emptyHtml({ title: 'No waves yet', desc: 'Create one.' }),
    '<div class="empty-state"><div class="empty-state-title">No waves yet</div>'
    + '<div class="empty-state-desc">Create one.</div></div>');
  assert.equal(w.emptyHtml({ title: 'No app rules.' }),
    '<div class="empty-state"><div class="empty-state-title">No app rules.</div></div>');
  assert.equal(w.emptyHtml({ title: 'Nothing waiting.', compact: true }),
    '<div class="apps-empty-state">Nothing waiting.</div>');
  assert.match(w.emptyHtml({ title: '<i>', compact: true }), /&lt;i&gt;/);
});

test('a state row spans the table', () => {
  assert.equal(w.stateRowHtml(6, 'x'), '<tr class="state-row"><td colspan="6">x</td></tr>');
});
