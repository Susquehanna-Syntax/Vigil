// showLoadFailure: the in-place failure block with its Try again button.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js');
const element = (tag) => new w.StubElement(tag);

test('the failure block names what failed and carries the error message', () => {
  const c = element('div');
  w.showLoadFailure(c, 'alerts', new Error('HTTP 500: boom'), null);
  assert.equal(c.children.length, 1);
  const block = c.children[0];
  assert.equal(block.className, 'load-failed');
  assert.equal(block.getAttribute('role'), 'alert');
  assert.equal(block.children[0].className, 'load-failed-title');
  assert.equal(block.children[0].textContent, 'Could not load alerts');
  assert.equal(block.children[1].className, 'load-failed-desc');
  assert.equal(block.children[1].textContent, 'HTTP 500: boom');
});

test('error text is never read as markup', () => {
  const c = element('div');
  w.showLoadFailure(c, 'alerts', new Error('<img src=x>'), null);
  const desc = c.children[0].children[1];
  assert.equal(desc.textContent, '<img src=x>');
  assert.deepEqual(desc.children, []);
});

test('a retry gives a Try again button whose click reruns the load once', () => {
  const c = element('div');
  let calls = 0;
  w.showLoadFailure(c, 'rollouts', new Error('offline'), () => { calls += 1; });
  const block = c.children[0];
  const btn = block.children.find((el) => el.tagName === 'BUTTON');
  assert.ok(btn, 'expected a button');
  assert.equal(btn.textContent, 'Try again');
  assert.equal(btn.getAttribute('type'), 'button');
  btn.dispatchEvent({ type: 'click' });
  assert.equal(calls, 1);
});

test('without a retry there is no button', () => {
  const c = element('div');
  w.showLoadFailure(c, 'the inventory', new Error('offline'), null);
  assert.equal(c.children[0].children.filter((el) => el.tagName === 'BUTTON').length, 0);
});

test('an error with no message gets no desc element', () => {
  const c = element('div');
  w.showLoadFailure(c, 'the findings', new Error(''), null);
  const block = c.children[0];
  assert.equal(block.children.length, 1);
  assert.equal(block.children[0].className, 'load-failed-title');
});

test('a missing container is ignored, not thrown on', () => {
  w.showLoadFailure(null, 'alerts', new Error('boom'), () => {});
});
