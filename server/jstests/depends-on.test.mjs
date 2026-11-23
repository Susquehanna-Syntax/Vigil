// The drawer's "Depends on" picker: the shared host fetch it loads from, and
// how it reacts when a save is refused.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { load } from './harness.mjs';

const JS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'static', 'js');

function jsonw(body, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

async function fetchAllHosts(fetchImpl) {
  const w = load('vigil-utils.js');
  w.fetch = fetchImpl;
  return w.$('fetchAllHosts()');
}

test('fetchAllHosts follows pages, keeping only the path of an absolute next', async () => {
  const urls = [];
  const rows = await fetchAllHosts(async (url) => {
    urls.push(url);
    if (urls.length === 1) {
      return jsonw({ results: [{ id: 'a', hostname: 'a' }], next: 'http://other-host/api/v1/hosts/?page=2' });
    }
    return jsonw({ results: [{ id: 'b', hostname: 'b' }], next: null });
  });
  assert.deepEqual(Array.from(rows, r => r.id), ['a', 'b']);
  assert.deepEqual(urls, ['/api/v1/hosts/', '/api/v1/hosts/?page=2']);
});

test('fetchAllHosts keeps the pages it read when a later page fails', async () => {
  let calls = 0;
  const rows = await fetchAllHosts(async () => {
    calls += 1;
    if (calls === 1) return jsonw([{ id: 'a', hostname: 'a' }, { id: 'b', hostname: 'b' }]);
    return jsonw({ error: 'boom' }, false, 500);
  });
  assert.deepEqual(Array.from(rows, r => r.id), ['a', 'b']);
});

// The drawer itself. harness.load()'s document is fixed before the scripts
// run, and vigil-host-cards.js renders the pin bar as it loads — so this
// context keeps a live element map: #pin-bar for that render, and a stub
// #detail-depends-on select for the picker to fill.
const HOSTS = [
  { id: 'h1', hostname: 'alpha.internal', site: 's1' },
  { id: 'h2', hostname: 'beta.internal', site: 's1' },
  { id: 'h3', hostname: 'delta.internal', site: 's1' },
  { id: 'h4', hostname: 'gamma.internal', site: 's1' },
  { id: 'h5', hostname: 'elsewhere.internal', site: 's2' },
];

function drawer(rows) {
  const documentElement = new StubSelect('html');
  const elements = new Map();
  const byId = (id) => elements.get(id) || null;
  const pinBar = new StubSelect('div');
  pinBar.querySelectorAll = () => ({ forEach: () => {} });
  elements.set('pin-bar', pinBar);
  const select = new StubSelect('select');
  select.replaceChildren = () => { select.children.length = 0; };
  elements.set('detail-depends-on', select);

  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, URLSearchParams, TextEncoder, TextDecoder, Intl,
    document: {
      documentElement,
      body: new StubSelect('body'),
      createElement: (tag) => new StubSelect(tag),
      getElementById: byId,
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
  ctx.$ = (expr) => vm.runInContext(expr, ctx);
  ctx.fetch = async (url, opts) => rows(url, opts);
  const toasts = [];
  ctx.showToast = (message, type) => toasts.push([message, type]);
  return { c: ctx, select, toasts };
}

// The harness' StubElement plus a removal the pin-bar render expects.
class StubSelect {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this._text = '';
    this._html = '';
    this.classList = { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { (on ?? !this._set.has(c)) ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); } };
  }
  set textContent(v) { this._text = String(v ?? ''); this._html = this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  get textContent() { return this._text; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c) { this.children.unshift(c); return c; }
  remove() {}
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
}

const LIST = (url) => (url === '/api/v1/hosts/h3/' ? jsonw(HOSTS[2])
  : url.startsWith('/api/v1/hosts/') ? jsonw({ results: HOSTS, next: null })
  : (() => { throw new Error(`unexpected ${url}`); })());

test('the picker lists the site\'s other hosts by name, after Nothing, with nothing chosen', async () => {
  const { c, select } = drawer(LIST);
  await c.$('_loadDetailDependsOn("h3")');
  assert.deepEqual(select.children.map(o => o.textContent),
    ['Nothing', 'alpha.internal', 'beta.internal', 'gamma.internal']);
  assert.deepEqual(select.children.map(o => o.value), ['', 'h1', 'h2', 'h4']);
  assert.equal(select.value, '');
  assert.equal(select.disabled, false);
});

test('a parent the user cannot list still shows, by id, and stays chosen', async () => {
  const { c, select } = drawer((url) => (
    url === '/api/v1/hosts/h3/' ? jsonw({ ...HOSTS[2], depends_on: 'h9' }) : LIST(url)
  ));
  await c.$('_loadDetailDependsOn("h3")');
  assert.deepEqual(select.children.map(o => o.value), ['', 'h1', 'h2', 'h4', 'h9']);
  assert.equal(select.value, 'h9');
});

test('a refused save puts the select back to what it was', async () => {
  const { c, select } = drawer(LIST);
  await c.$('_loadDetailDependsOn("h3")');

  c.fetch = async (url, opts) => {
    assert.equal(url, '/api/v1/hosts/h3/depends-on/');
    assert.equal(opts.method, 'PATCH');
    return jsonw({ error: 'A host cannot depend on a host in another site.' }, false, 400);
  };
  select.value = 'h1';
  await c.$('saveDetailDependsOn(document.getElementById("detail-depends-on"))');
  assert.equal(select.value, '');
  assert.equal(select.disabled, false);
});

test('a saved dependency keeps the choice and a later refusal rolls back to it', async () => {
  const { c, select, toasts } = drawer(LIST);
  await c.$('_loadDetailDependsOn("h3")');

  c.fetch = async (url, opts) => {
    assert.equal(JSON.parse(opts.body).depends_on, 'h2');
    return jsonw({ ...HOSTS[2], depends_on: 'h2' });
  };
  select.value = 'h2';
  await c.$('saveDetailDependsOn(document.getElementById("detail-depends-on"))');
  assert.equal(select.value, 'h2');
  assert.deepEqual(toasts[toasts.length - 1], ['Dependency saved', 'success']);

  c.fetch = async () => jsonw({ error: 'That would be a loop.' }, false, 400);
  select.value = '';
  await c.$('saveDetailDependsOn(document.getElementById("detail-depends-on"))');
  assert.equal(select.value, 'h2');
});
