// vigil-modal.js
// Owns: the behaviour every dialog and drawer shares (M12, SQSY design
//   language): focus moves in on open, Tab stays inside, Esc closes the top
//   one only, the page behind is inert, focus returns to whatever opened it,
//   and a dialog opened over another makes the lower one step back.
// HTML: any .modal or .detail-panel; its scrim is the sibling whose id swaps
//   "-modal"/"-panel" for "-overlay".
// Depends on: vigil-utils.js (_closeFnFor). Loads straight after it.
//
// Modules keep their own open/close functions — they add and remove the
// `open` class as they always have. This file watches that class instead of
// asking twenty-two dialogs to call into it, so a dialog written tomorrow gets
// the same behaviour without remembering to.

const _MODAL_SEL = '.modal, .detail-panel';
const _FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), '
  + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

//: The last few focused elements: a module often focuses a field inside its
//: dialog before this file hears the dialog opened, so the opener is the
//: latest of these outside the dialog, not document.activeElement.
const _focusTrail = [];
document.addEventListener('focusin', (e) => {
  _focusTrail.push(e.target);
  if (_focusTrail.length > 6) _focusTrail.shift();
});

function _openerOf(el) {
  for (let i = _focusTrail.length - 1; i >= 0; i--) {
    const f = _focusTrail[i];
    if (f.isConnected && !el.contains(f)) return f;
  }
  return null;
}

//: Open dialogs, bottom first: { el, trigger }.
const _modalStack = [];
//: Elements this file made inert, so closing restores exactly those.
let _inerted = [];

function _overlayFor(el) {
  return el.id ? document.getElementById(el.id.replace(/-(modal|panel)$/, '-overlay')) : null;
}

function _isOpen(el) {
  return el.isConnected && el.classList.contains('open');
}

function _visible(el) {
  return el.offsetParent !== null || getComputedStyle(el).position === 'fixed';
}

//: Everything outside the top dialog goes inert: at each level from the
//: dialog up to <body>, its siblings — except scrims, toasts and scripts.
function _applyInert() {
  _inerted.forEach(n => { n.inert = false; });
  _inerted = [];
  const top = _modalStack[_modalStack.length - 1];
  if (!top) return;
  for (let node = top.el; node && node !== document.body; node = node.parentElement) {
    const parent = node.parentElement;
    if (!parent) break;
    for (const sib of parent.children) {
      if (sib === node || sib.inert) continue;
      if (sib.matches('script, style, .toast-container, .modal-overlay, .detail-overlay')) continue;
      sib.inert = true;
      _inerted.push(sib);
    }
  }
}

function _focusInto(el) {
  requestAnimationFrame(() => {
    if (!_isOpen(el) || el.contains(document.activeElement)) return;
    const fields = [...el.querySelectorAll('input, select, textarea')]
      .filter(f => !f.disabled && f.type !== 'hidden' && _visible(f));
    const target = fields[0]
      || [...el.querySelectorAll(_FOCUSABLE)].find(f => !f.matches('.modal-close, .detail-close') && _visible(f));
    if (target) { target.focus({ preventScroll: true }); return; }
    if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
    el.focus({ preventScroll: true });
  });
}

//: Bring the stack in line with the DOM: drop what closed, push what opened.
function _syncModals() {
  let changed = false;
  for (let i = _modalStack.length - 1; i >= 0; i--) {
    const entry = _modalStack[i];
    if (_isOpen(entry.el)) continue;
    _modalStack.splice(i, 1);
    if (entry.el.classList.contains('recede')) entry.el.classList.remove('recede');
    _overlayFor(entry.el)?.classList.remove('stacked');
    changed = true;
    const trig = entry.trigger;
    const focusLost = !document.activeElement || document.activeElement === document.body
      || entry.el.contains(document.activeElement);
    if (focusLost && trig && trig.isConnected && typeof trig.focus === 'function') {
      // After the inert attributes are lifted, or the focus call is refused.
      requestAnimationFrame(() => trig.focus({ preventScroll: true }));
    }
  }
  document.querySelectorAll(_MODAL_SEL).forEach(el => {
    if (!_isOpen(el) || _modalStack.some(e => e.el === el)) return;
    const below = _modalStack[_modalStack.length - 1];
    if (below) {
      if (below.el.classList.contains('modal')) below.el.classList.add('recede');
      _overlayFor(el)?.classList.add('stacked');
    }
    _modalStack.push({ el, trigger: _openerOf(el) });
    _focusInto(el);
    changed = true;
  });
  // Guarded: classList.remove rewrites the attribute even when the class is
  // absent, which queues a mutation record and would wake this observer again.
  const top = _modalStack[_modalStack.length - 1];
  if (top && top.el.classList.contains('recede')) top.el.classList.remove('recede');
  if (changed) _applyInert();
}

new MutationObserver((records) => {
  for (const r of records) {
    if (r.type === 'attributes' ? r.target.matches?.(_MODAL_SEL)
      : (_modalStack.length || [...r.addedNodes].some(n => n.nodeType === 1 && n.matches(_MODAL_SEL)))) {
      _syncModals();
      return;
    }
  }
}).observe(document.documentElement, {
  subtree: true, childList: true, attributes: true, attributeFilter: ['class'],
});

//: Close one dialog the way its module would: its own close function, else
//: its close button (which resolves a confirm's promise), else the function
//: its scrim's data-act names, else just the classes.
function closeModalEl(el) {
  const fn = el.id ? _closeFnFor(el.id) : null;
  if (fn) { fn(); return; }
  const closeBtn = el.querySelector('.modal-title .modal-close, .detail-close');
  if (closeBtn) { closeBtn.click(); return; }
  const overlay = _overlayFor(el);
  const act = overlay && overlay.dataset.act;
  if (act && typeof window[act] === 'function') { window[act](); return; }
  el.classList.remove('open');
  if (overlay) overlay.classList.remove('open');
}

// Captured on window, ahead of every module's own Escape listener: several of
// those close their dialog unconditionally, so Esc on a confirm over the
// deploy dialog used to close both. This closes the top one and stops the
// event there. A menu open inside a dialog keeps its own Escape.
window.addEventListener('keydown', (e) => {
  const top = _modalStack[_modalStack.length - 1];
  if (!top) return;
  if (e.key === 'Escape') {
    if (!_isOpen(top.el) || e.target.closest?.('[role="menu"]')) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    closeModalEl(top.el);
    return;
  }
  if (e.key !== 'Tab') return;
  const items = [...top.el.querySelectorAll(_FOCUSABLE)].filter(_visible);
  if (!items.length) { e.preventDefault(); return; }
  const first = items[0];
  const last = items[items.length - 1];
  if (!top.el.contains(document.activeElement)) {
    e.preventDefault();
    first.focus();
  } else if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}, true);

//: The "Destructive" variant: a typed confirmation that does not match.
function shakeEl(el) {
  if (!el) return;
  el.classList.remove('shake');
  void el.offsetWidth; // restart the animation
  el.classList.add('shake');
  el.addEventListener('animationend', () => el.classList.remove('shake'), { once: true });
}
