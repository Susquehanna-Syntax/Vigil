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
    // An element's id is a property of it as much as an attribute, and the stub
    // has no parser to keep the two in step. So one store with two names: setting
    // either — `el.id = 'x'`, or setAttribute('id', …) from markup a test parsed
    // by hand — makes the node findable by #x.
    this._id = '';
    // What a script attached by name, per element and per kind, so a test can act
    // as the browser would.
    this._listeners = {};
    // Set by appendChild and by _walk: the container the node was last placed
    // in, which is what remove() takes itself out of. `parent` names it too.
    this._parent = null;
    // The document a node was placed under, so focus() can tell it (M6 tabs).
    this.ownerDocument = null;
    this.clicks = [];
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
  // The whole subtree's text, as a browser's textContent would give it. A
  // separate name because the stub's textContent is the element's own text, which
  // other tests compare exactly.
  get deepText() { return this._text + this.children.map((c) => c.deepText).join(''); }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  get id() { return this._id; }
  set id(v) { this._id = String(v ?? ''); this.attributes.id = this._id; }
  setAttribute(k, v) {
    this.attributes[k] = String(v);
    // Two attributes are also plain properties of an element: a script that
    // reads back what it set — `el.id`, or a label's `for` — must see it.
    if (k === 'value' || k === 'id' || k === 'for') this[k] = String(v);
  }
  // A browser's getAttribute sees a name set as a property too, which is how a
  // script reaches `el.title` most of the time and what a test then reads back.
  getAttribute(k) { return k in this ? String(this[k]) : (k in this.attributes ? this.attributes[k] : null); }
  removeAttribute(k) { delete this.attributes[k]; if (k === 'id') this._id = ''; }
  appendChild(c) { this.children.push(c); c._parent = this; c.ownerDocument = this.ownerDocument; return c; }
  get parent() { return this._parent; }
  set parent(p) { this._parent = p; }
  // Browsers take several children at once; the stub says the same thing by
  // taking each in turn through its own appendChild.
  append(...nodes) { for (const n of nodes) this.appendChild(n); }
  cloneNode(deep) {
    const copy = new StubElement(this.tagName);
    copy._text = this._text;
    copy._html = this._html;
    for (const [k, v] of Object.entries(this.attributes)) copy.setAttribute(k, v);
    for (const [k, v] of Object.entries(this.dataset)) copy.dataset[k] = v;
    for (const c of this.classList) copy.classList.add(c);
    copy.hidden = this.hidden;
    copy.disabled = this.disabled;
    copy.value = this.value;
    if (deep !== false) for (const child of this.children) copy.appendChild(child.cloneNode(true));
    return copy;
  }
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
  get options() { return this.children.filter((c) => c.tagName === 'OPTION'); }
  // A checkbox's `checked` and a radio's: false until something ticks it, which
  // is what an element the DOM would call unchecked says too.
  _checked = false;
  get checked() { return this._checked; }
  set checked(v) { this._checked = !!v; }
  // One place what a script attached by name can be read back from, so a test can
  // act as the browser would: a checkbox's own change handler is the only wiring a
  // tick has, and a click reaches an ancestor that delegated for it. A plain field
  // of an object literal could be shared by every stub in a process, which is not
  // what a listener map means.
  addEventListener(kind, fn) { (this._listeners[kind] ||= []).push(fn); }
  removeEventListener(kind, fn) {
    const list = this._listeners[kind] || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  }
  // target, then bubble up the parents, as the DOM's (no capture phase here).
  dispatchEvent(ev) {
    if (ev.target === null || ev.target === undefined) ev.target = this;
    for (let node = this; node; node = node._parent) {
      for (const fn of (node._listeners[ev.type] || []).slice()) fn.call(node, ev);
      if (ev.bubbles === false) break;
    }
    return !ev.defaultPrevented;
  }
  focus() { if (this.ownerDocument && this.ownerDocument.__vigilFocus) this.ownerDocument.__vigilFocus(this); }
  // Simple selectors only: an optional tag, then any number of classes in either
  // order, an optional id, and one [data-x] or [data-x="v"], with comma-separated
  // alternatives. Two things the old reader could not say at all: a row inside a
  // container (`tbody tr`), which is how a test names a table's rows from outside
  // it, and two classes on one element (`table.x.y`). A browser reads each as what
  // it is — the node answers the pair's last word and an ancestor answers its
  // first; a compound is one element's parts read together — so these do too.
  _matches(sel) {
    return sel.split(',').some((part) => {
      const pieces = part.trim().split(/\s+/);
      if (pieces.length === 1) return this._matchesCompound(pieces[0]);
      if (pieces.length !== 2) return false;
      return this._matchesCompound(pieces[1]) && !!this._parent
        && this._parent._matchesCompound(pieces[0]);
    });
  }

  /** A whole compound — an element name, `#id`, any number of `.class`es and one
   *  `[data-x]` — read together. The parts already pin the element down, so a tag
   *  written beside them (`table.device-check-ifaces`) only says which word the
   *  test reached for: it is not demanded twice over. */
  _matchesCompound(text) {
    // The parts of a compound follow its element name with no space between them,
    // so the plain words are stripped before the rest is read — otherwise the tag
    // swallows everything after it (`table.device-check-ifaces` comes back as one
    // long word, and `.` matches nothing).
    const head = text.match(/^[a-zA-Z][A-Za-z0-9_-]*/);
    const rest = head ? text.slice(head[0].length) : text;
    if (rest !== '' && !/^[.#[[]/.test(rest)) return false;
    const bits = rest.match(/(?:#[A-Za-z0-9_-]+)|(?:\.[A-Za-z0-9_-]+)|\[[^\]]+\]/g) || [];
    // An element name, at most one `#id` and one `[data-x]`, plus any number of
    // `.class`es: as far as a selector here goes, and no further.
    if (bits.filter((b) => b.startsWith('#')).length > 1) return false;
    if (bits.filter((b) => b.startsWith('[')).length > 1) return false;
    // A tag standing alone is the whole of what the selector asks for. Beside
    // other parts it names the element those parts already pin down, so it is not
    // demanded twice over — `table.x` finds whatever carries that class.
    if (bits.length === 0) return head && this.tagName === head[0].toUpperCase();
    return bits.every((b) => this._matchesBit(b));
  }

  /** One part of a compound: `#id`, `.class` or `[data-x]` / `[data-x="v"]`. An
   *  element name is not one — the compound above weighs that itself, since what
   *  such a word constrains depends on what else it stands beside. */
  _matchesBit(bit) {
    if (bit.startsWith('#')) return this.id === bit.slice(1);
    if (bit.startsWith('.')) return this.classList.contains(bit.slice(1));
    if (!bit.startsWith('[')) return false;
    const m = bit.match(/^\[data-([a-z-]+)(?:="([^"]*)")?\]$/);
    if (!m) {
      // Any other attribute: [role="tablist"], [aria-selected], [tabindex="0"].
      const g = bit.match(/^\[([A-Za-z_:][-A-Za-z0-9_:.]*)(?:=["']?([^"'\]]*)["']?)?\]$/);
      if (!g) return false;
      const have = this.getAttribute(g[1]);
      return g[2] === undefined ? have !== null : have === g[2];
    }
    const key = m[1].replace(/-(\w)/g, (_, c) => c.toUpperCase());
    const have = this.dataset[key];
    return m[2] === undefined ? have !== undefined : have === m[2];
  }
  querySelector(sel) { return this._walk((n) => n._matches(sel))[0] || null; }
  querySelectorAll(sel) { return this._walk((n) => n._matches(sel)); }
  // A test's way of acting on an element: `click()` fires what a browser fires —
  // the element's own click handlers, then that event bubbling to any ancestor
  // that delegated for it — and `check()` is a ticked box, whose change does not
  // bubble and reaches only the box itself. Both are how the page finds out
  // anything happened, so a test that acts needs exactly one of them.
  click() { this.clicks.push('click'); this.dispatchEvent(new StubMouseEvent('click')); }
  check() { this.checked = true; this.dispatchEvent(new StubEvent('change', { bubbles: false })); }
  // `change` does not bubble and `click` does: each goes where the browser takes
  // it, since that is what the dialog's own wiring listens for.
  _fire(kind) {
    for (const fn of this._listeners[kind] || []) fn.call(this);
    if (kind !== 'click') return;
    let host = this._parent;
    while (host) {
      for (const fn of host._listeners[kind] || []) fn({ target: this });
      host = host._parent;
    }
  }
  // The DOM methods Vigil's renderers use on a host, in terms of the one child
  // store. clear() only empties: children and text are separate stores here, so
  // dropping text as a side effect would wipe what an earlier test put there.
  replaceChildren(...nodes) {
    // A browser's drops the elements it removes, and the parent link has to go
    // the same way: a node left pointing at a container that no longer holds it
    // answers closest() and remove() with somewhere they should not be.
    for (const child of this.children) child._parent = null;
    this.children = [...nodes];
    for (const n of nodes) if (n && typeof n === 'object') { n._parent = this; n.ownerDocument = this.ownerDocument; }
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c._parent = null; return c; }
  // The element itself, then each ancestor, as the DOM's.
  closest(sel) {
    for (let node = this; node; node = node._parent) if (node._matches(sel)) return node;
    return null;
  }
  matchesSelector(sel) { return this._matches(sel); }
  matches(sel) { return this._matches(sel); }
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
  ctx.globalThis = ctx;
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
