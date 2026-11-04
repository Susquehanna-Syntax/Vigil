// The playbook flow model in vigil-playbook-flow.js: what the editor holds,
// what it sends to the API, and the condition builder's rows. A wrong turn
// here saves a playbook that runs differently from the one on screen.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js', 'vigil-playbook-flow.js');
const plain = (v) => JSON.parse(JSON.stringify(v));   // vm-realm objects -> this realm

const ROW = {
  steps: [
    { step_id: 's1', definition_id: 10, definition_name: 'Check disk', on_failure: 'continue' },
    { step_id: 's2', definition_id: 11, definition_name: 'Clean', inputs: { days: 7 } },
    { step_id: 's3', definition_id: 12, definition_name: 'Page someone', color: 'rose' },
  ],
  flow: [
    { step: 's1' },
    { id: 'b1', if: 'steps.s1.status == "failed"', then: [{ step: 's2' }], else: [{ step: 's3' }] },
    { step: 'gone' },                          // a step the row no longer has is dropped
  ],
};

test('a row becomes a tree and serializes back to the same flow', () => {
  const tree = w.flowTreeFromRow(ROW);
  assert.equal(tree.length, 2);
  assert.equal(tree[0].kind, 'step');
  assert.equal(tree[0].onf, 'continue');
  assert.equal(tree[1].kind, 'if');
  const out = plain(w.flowSerialize(tree));
  assert.equal(out[1].if, 'steps.s1.status == "failed"');
  assert.equal(out[1].then[0].id, 's2');
  assert.deepEqual(out[1].then[0].inputs, { days: 7 });
  assert.equal(out[1].else[0].color, 'rose');
  assert.equal(out[0].on_not_applicable, 'stop');     // defaults are explicit on the wire
});

test('a row with no flow is a straight line of its steps', () => {
  const tree = w.flowTreeFromRow({ steps: ROW.steps });
  assert.deepEqual(plain(tree.map((n) => n.sid)), ['s1', 's2', 's3']);
});

test('every path through a branch is listed', () => {
  const paths = w.flowPaths(w.flowTreeFromRow(ROW));
  assert.deepEqual(plain(paths.map((p) => p.map((s) => s.sid))), [['s1', 's2'], ['s1', 's3']]);
});

test('flowNextSid fills the first free id', () => {
  assert.equal(w.flowNextSid(w.flowTreeFromRow(ROW)), 's4');
  assert.equal(w.flowNextSid([]), 's1');
});

test('condition rows serialize to an expression and parse back', () => {
  const rows = [
    { sid: 's1', what: 'status', op: '==', value: 'failed' },
    { sid: 's2', what: 'free_gb', op: '<', value: '10' },
    { sid: 's2', what: 'mounted', op: '==', value: 'true' },
  ];
  const expr = w.flowCondJoin(rows, 'all');
  assert.equal(expr,
    '(steps.s1.status == "failed") and (steps.s2.result.free_gb < 10) and (steps.s2.result.mounted == True)');
  const back = plain(w.flowCondParse(expr));
  assert.equal(back.mode, 'all');
  assert.deepEqual(back.rows.map((r) => [r.sid, r.what, r.op]),
    [['s1', 'status', '=='], ['s2', 'free_gb', '<'], ['s2', 'mounted', '==']]);
});

test('a value that is not a number or a boolean is quoted, so it cannot become code', () => {
  assert.equal(w.flowCondRow({ sid: 's1', what: 'out', op: '==', value: 'x) or (1' }),
    'steps.s1.result.out == "x) or (1"');
});

test('what the rows cannot hold parses as null, so the editor falls back to text', () => {
  assert.equal(w.flowCondParse('steps.s1.status == "ok" and steps.s2.status == "ok" or steps.s3.status == "ok"'), null);
  assert.equal(w.flowCondParse('not steps.s1.status == "ok"'), null);
  assert.equal(w.flowCondParse('steps.s1.status > "ok"'), null);   // ordering on a status
  assert.deepEqual(plain(w.flowCondParse('')), { mode: 'all', rows: [] });
});

test('conditions read in words', () => {
  assert.ok(w.flowCondWords('steps.s2.result.free_gb >= 5').includes('s2 → free_gb ≥ 5'));
});
