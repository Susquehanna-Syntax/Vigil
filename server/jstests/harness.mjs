// Load Vigil's browser scripts into a Node vm context, the way the page's
// <script> tags load them: one shared global scope, no bundler, no modules.
// The DOM is a small stub, enough for the scripts to load and for their pure
// helpers to run. Tests that need real layout belong in the browser suite.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const JS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'static', 'js');

class StubElement {
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
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
}

export function load(...files) {
  const documentElement = new StubElement('html');
  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, URLSearchParams, TextEncoder, TextDecoder, Intl,
    document: {
      documentElement,
      body: new StubElement('body'),
      createElement: (tag) => new StubElement(tag),
      getElementById: () => null,
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
  for (const file of files) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, file), 'utf8'), ctx, { filename: file });
  }
  // const/let at a script's top level live in the context's shared lexical
  // scope, not on the global object, so read them by evaluating a name.
  ctx.$ = (expr) => vm.runInContext(expr, ctx);
  ctx.StubElement = StubElement;
  return ctx;
}
