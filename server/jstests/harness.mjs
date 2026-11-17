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
    // Set by appendChild and by _walk: the container the node was last placed
    // in, which is what remove() takes itself out of.
    this._parent = null;
    this.classList = { _set: new Set(), add(c) { this._set.add(c); }, remove(c) { this._set.delete(c); },
      toggle(c, on) { (on ?? !this._set.has(c)) ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); }, [Symbol.iterator]() { return this._set.values(); } };
  }
  // className and classList are one store, as in a browser.
  set className(v) { this.classList._set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classList._set].join(' '); }
  // textContent -> innerHTML escapes exactly what a browser's serialiser does
  // for text: & < > (and no quotes).
  set textContent(v) { this._text = String(v ?? ''); this._html = this._text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  get textContent() { return this._text; }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  appendChild(c) { this.children.push(c); c._parent = this; return c; }
  // A chart is the only canvas Vigil draws on and Chart.js itself stays out of
  // these tests, so a context object is enough for the script to hand over.
  getContext() { return { canvas: this }; }
  // Minimal tree helpers, in terms of the one child store: a script that reads
  // its way from a node to its container or takes one of its own children away
  // has to mean the same thing here as in a browser.
  get parentElement() { return this._parent || null; }
  // The element after this one in its container, as a live tree reads it.
  get nextElementSibling() {
    const sibs = this._parent ? this._parent.children : [];
    const i = sibs.indexOf(this);
    return i < 0 ? null : sibs[i + 1] || null;
  }
  remove() { if (this._parent) this._parent.removeChild(this); }
  after(n) {
    const parent = this._parent;
    if (!parent) return;
    parent.children.splice(parent.children.indexOf(this) + 1, 0, n);
    n._parent = parent;
  }
  _walk(fn) {
    const out = [];
    for (const child of this.children) {
      if (fn(child)) out.push(child);
      out.push(...child._walk(fn));
    }
    return out;
  }
  // Simple selectors only: an optional tag, an optional class, an optional
  // [data-x] or [data-x="v"], comma-separated alternatives.
  _matches(sel) {
    return sel.split(',').some((part) => {
      const m = part.trim().match(/^([a-z]+)?(?:\.([A-Za-z0-9_-]+))?(?:\[data-([a-z-]+)(?:="([^"]*)")?\])?$/);
      if (!m || !(m[1] || m[2] || m[3])) return false;
      if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
      if (m[2] && !this.classList.contains(m[2])) return false;
      if (m[3]) {
        const key = m[3].replace(/-(\w)/g, (_, c) => c.toUpperCase());
        if (m[4] === undefined ? this.dataset[key] === undefined : this.dataset[key] !== m[4]) return false;
      }
      return true;
    });
  }
  querySelector(sel) { return this._walk((n) => n._matches(sel))[0] || null; }
  querySelectorAll(sel) { return this._walk((n) => n._matches(sel)); }
  // The DOM methods Vigil's renderers use on a host, in terms of the one child
  // store. clear() only empties: children and text are separate stores here, so
  // dropping text as a side effect would wipe what an earlier test put there.
  replaceChildren(...nodes) { this.children = [...nodes]; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c._parent = null; return c; }
  addEventListener() {}
  closest(sel) { return this._parent && this._parent._matches(sel) ? this._parent : null; }
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
