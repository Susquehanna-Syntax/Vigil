// The host detail drawer's line about how the agent's last self-update ended.
// Every value comes off the wire from an agent, so a reason has to arrive as
// text and a null update has to clear what the previous host left behind.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const JS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'static', 'js');

class Elem {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.style = {};
    this.attributes = {};
    this.className = '';
    this.hidden = false;
    this._text = '';
    this._html = '';
    this.classList = { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { (on ?? !this._set.has(c)) ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); } };
  }
  // textContent -> innerHTML escapes exactly what a browser's serialiser does
  // for text: & < > (and no quotes).
  set textContent(v) { this._text = String(v ?? ''); this._html = this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  get textContent() { return this._text; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
}

// vigil-host-cards.js renders its pinned-hosts bar while it loads, so the stub
// document has to answer for that render too; the element a test cares about is
// installed afterwards.
function sandbox() {
  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, URLSearchParams, TextEncoder, TextDecoder, Intl,
    document: {
      documentElement: new Elem('html'),
      body: new Elem('body'),
      createElement: (tag) => new Elem(tag),
      getElementById: () => new Elem('div'),
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
      cookie: '',
      readyState: 'complete',
    },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    navigator: { userAgent: 'node', clipboard: {} },
    location: { origin: 'http://vigil.test', pathname: '/', href: 'http://vigil.test/' },
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    fetch: async () => { throw new Error('no network in unit tests'); },
  };
  ctx.window = ctx;
  ctx.addEventListener = () => {};
  ctx.removeEventListener = () => {};
  ctx.globalThis = ctx;
  ctx.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
  vm.createContext(ctx);
  for (const file of ['vigil-utils.js', 'vigil-host-cards.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, file), 'utf8'), ctx, { filename: file });
  }
  return ctx;
}

function collect(el, out = []) {
  out.push(el);
  el.children.forEach((c) => collect(c, out));
  return out;
}

test('a rolled-back reason from an agent becomes text, never an element', () => {
  const ctx = sandbox();
  const el = new Elem('div');
  el.hidden = true;
  ctx.document.getElementById = (id) => (id === 'detail-agent-update' ? el : null);
  const render = vm.runInContext('_renderDetailAgentUpdate', ctx);
  const payload = '<img src=x onerror=alert(1)>';
  render({ state: 'rolled_back', from_version: '2026.16.0', to_version: '2026.17.0',
           reason: payload, at: new Date().toISOString() });
  const nodes = collect(el);
  assert.equal(nodes.some((n) => n.tagName === 'IMG'), false);
  assert.ok(nodes.some((n) => n.textContent === payload));
  assert.ok(nodes.some((n) => n.className === 'detail-agent-update-reason'));
  assert.equal(el.hidden, false);
  assert.ok(el.classList.contains('detail-agent-update--bad'));
  assert.match(el.children[0].textContent,
    /Update to v2026\.17\.0 rolled back to v2026\.16\.0/);
});

test('no update hides the line and clears the previous host\'s', () => {
  const ctx = sandbox();
  const el = new Elem('div');
  ctx.document.getElementById = (id) => (id === 'detail-agent-update' ? el : null);
  const render = vm.runInContext('_renderDetailAgentUpdate', ctx);
  render({ state: 'not_applied', to_version: '2026.17.0', reason: 'still running the old build',
           at: new Date().toISOString() });
  assert.equal(el.hidden, false);
  render(null);
  assert.equal(el.hidden, true);
  assert.equal(el.children.length, 0);
});

test('a success older than a week is not news', () => {
  const ctx = sandbox();
  const el = new Elem('div');
  ctx.document.getElementById = (id) => (id === 'detail-agent-update' ? el : null);
  const render = vm.runInContext('_renderDetailAgentUpdate', ctx);
  render({ state: 'confirmed', to_version: '2026.17.0', at: new Date().toISOString() });
  assert.equal(el.hidden, false);
  assert.ok(el.classList.contains('detail-agent-update--ok'));
  assert.match(el.children[0].textContent, /Updated to v2026\.17\.0/);
  render({ state: 'confirmed', to_version: '2026.17.0',
           at: new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString() });
  assert.equal(el.hidden, true);
});
