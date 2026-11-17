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
    this.id = '';
    this.parent = null;
    this.ownerDocument = null;
    this._listeners = new Map();
    this.clicks = [];
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
  // The id is a property in Vigil's markup as much as an attribute, and the two
  // are one: aria-labelledby, getElementById and `#id` all find what set it.
  get id() { return this._id; }
  set id(v) { this._id = String(v); }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    if (k === 'id') this._id = String(v);
  }
  getAttribute(k) { return k === 'id' ? (this._id || null) : this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; if (k === 'id') this._id = ''; }
  appendChild(c) { c.parent = this; c.ownerDocument = this.ownerDocument; this.children.push(c); return c; }
  addEventListener(type, fn) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this._listeners.get(type) || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }
  // capture → target → bubble, as the DOM's.
  dispatchEvent(ev) {
    if (ev.target === null || ev.target === undefined) ev.target = this;
    for (let node = this; node; node = node.parent) {
      for (const fn of (node._listeners.get(ev.type) || []).slice()) fn.call(node, ev);
      if (ev.bubbles === false) break;
    }
    return !ev.defaultPrevented;
  }
  focus() { if (this.ownerDocument) this.ownerDocument.__vigilFocus(this); }
  click() { this.clicks.push('click'); this.dispatchEvent(new StubMouseEvent('click')); }
  _query(predicate, collect, limitOne) {
    const out = [];
    for (const child of this.children) {
      if (predicate(child)) {
        out.push(collect(child));
        if (limitOne) return out[0];
      }
      const nested = child._query(predicate, collect, limitOne);
      if (limitOne && nested) return nested;
      if (!limitOne) out.push(...nested);
    }
    return limitOne ? null : out;
  }
  matchesSelector(sel) {
    return sel.split(',').some(part => {
      part = part.trim();
      if (part.startsWith('.')) return this.classList.contains(part.slice(1));
      if (part.startsWith('#')) return this.id === part.slice(1);
      if (part.startsWith('[') && part.endsWith(']')) {
        const body = part.slice(1, -1);
        const eq = body.indexOf('=');
        if (eq < 0) return this.getAttribute(body) !== null;
        const name = body.slice(0, eq);
        const want = body.slice(eq + 1).replace(/^["']|["']$/g, '');
        return this.getAttribute(name) === want;
      }
      return this.tagName === part.toUpperCase();
    });
  }
  querySelector(sel) { return this._query(n => n.matchesSelector(sel), n => n, true); }
  querySelectorAll(sel) { return this._query(n => n.matchesSelector(sel), n => n, false); }
  closest(sel) {
    for (let node = this; node; node = node.parent) if (node.matchesSelector(sel)) return node;
    return null;
  }
}

// Browsers key these by the event *type* passed in: new KeyboardEvent('keydown',
// { key: 'ArrowRight' }), and a click dispatches a MouseEvent called 'click'.
class StubEvent {
  constructor(type, init) {
    const opts = (init && typeof init === 'object') ? init : {};
    this.type = String(type);
    this.key = opts.key ?? '';
    this.bubbles = opts.bubbles ?? true;
    this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() { this.defaultPrevented = true; }
}

class StubKeyboardEvent extends StubEvent {}
class StubMouseEvent extends StubEvent {}

export function load(...files) {
  const documentElement = new StubElement('html');
  const store = new Map();
  let active = null;
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    URL, URLSearchParams, TextEncoder, TextDecoder, Intl,
    KeyboardEvent: StubKeyboardEvent,
    MouseEvent: StubMouseEvent,
    document: {
      documentElement,
      body: new StubElement('body'),
      createElement: (tag) => new StubElement(tag),
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener() {},
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
  ctx.document.body.parent = documentElement;
  ctx.document.body.ownerDocument = ctx.document;
  ctx.window = ctx;
  ctx.addEventListener = () => {};
  ctx.removeEventListener = () => {};
  ctx.globalThis = { MouseEvent: StubMouseEvent, KeyboardEvent: StubKeyboardEvent };
  ctx.matchMedia = () => ({ matches: false, addEventListener() {}, addListener() {} });
  vm.createContext(ctx);
  Object.defineProperty(ctx.document, 'activeElement', { get: () => active });
  // A tab bar leaves exactly one tab focusable and that tab is the selected one:
  // a click on a tab focuses it, and vigil-nav.js moves the focus to whichever
  // tab its arrow keys activate. So after a bar settles, its tabindex="0" is
  // where the keyboard sits — which is also how a test aims the next keypress.
  ctx.document.__vigilFocus = (el) => {
    const bar = el.closest('[role="tablist"]');
    const held = bar && bar.querySelectorAll('.tab').find(t => t.getAttribute('tabindex') === '0');
    active = held ?? el;
  };
  for (const file of files) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, file), 'utf8'), ctx, { filename: file });
  }
  // The page's scripts are the first callers of document.querySelector and
  // friends, so those live here — on the tree that exists by then — rather than
  // in the stub above.
  const root = ctx.document.body;
  ctx.document.getElementById = (id) => root.querySelector('#' + id);
  ctx.document.querySelector = (sel) => root.querySelector(sel);
  ctx.document.querySelectorAll = (sel) => root.querySelectorAll(sel);
  // const/let at a script's top level live in the context's shared lexical
  // scope, not on the global object, so read them by evaluating a name.
  ctx.$ = (expr) => vm.runInContext(expr, ctx);
  ctx.StubElement = StubElement;
  Object.defineProperty(ctx, 'activeElement', { get: () => active });
  return ctx;
}
