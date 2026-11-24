// @ts-check
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

//: Open *and* modal: a floating or minimised window is open but is not part
//: of the stack — it neither takes Escape nor makes the page behind inert.
function _isStacked(el) {
  return _isOpen(el) && !el.classList.contains('floating') && !el.classList.contains('minimized');
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
  _windowCleanup();
  for (let i = _modalStack.length - 1; i >= 0; i--) {
    const entry = _modalStack[i];
    if (_isStacked(entry.el)) continue;
    _modalStack.splice(i, 1);
    if (entry.el.classList.contains('recede')) entry.el.classList.remove('recede');
    _overlayFor(entry.el)?.classList.remove('stacked');
    changed = true;
    const trig = entry.trigger;
    const focusLost = !document.activeElement || document.activeElement === document.body
      || entry.el.contains(document.activeElement);
    // A dialog that became a window or was minimised is still open: only a
    // real close hands focus back to whatever opened it.
    if (focusLost && !_isOpen(entry.el) && trig && trig.isConnected && typeof trig.focus === 'function') {
      // After the inert attributes are lifted, or the focus call is refused.
      requestAnimationFrame(() => trig.focus({ preventScroll: true }));
    }
  }
  document.querySelectorAll(_MODAL_SEL).forEach(el => {
    if (!_isStacked(el) || _modalStack.some(e => e.el === el)) return;
    const below = _modalStack[_modalStack.length - 1];
    if (below) {
      if (below.el.classList.contains('modal')) below.el.classList.add('recede');
      _overlayFor(el)?.classList.add('stacked');
    }
    _modalStack.push({ el, trigger: _openerOf(el) });
    _resizableInit(el);
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
    if (r.type === 'attributes' ? /** @type {HTMLElement} */ (r.target).matches?.(_MODAL_SEL)
      : (_modalStack.length || [...r.addedNodes].some((/** @type {Node} */ n) => n.nodeType === 1 && /** @type {HTMLElement} */ (n).matches(_MODAL_SEL)))) {
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
    // A menu keeps its own Escape, and so does a terminal or remote desktop
    // (vim and less need it): those dialogs close from their own buttons.
    if (!_isStacked(top.el) || /** @type {HTMLElement} */ (e.target).closest?.('[role="menu"], [data-keeps-tab]')) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    closeModalEl(top.el);
    return;
  }
  if (e.key !== 'Tab') return;
  // A terminal or a remote desktop needs Tab itself (completion, Alt+Tab on
  // the far side), so a dialog or region marked data-keeps-tab is left alone.
  if (top.el.hasAttribute('data-keeps-tab') || /** @type {HTMLElement} */ (e.target).closest?.('[data-keeps-tab]')) return;
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

/* ── Resizable dialogs ──────────────────────────────────────────────────────
   User, 2026-10-15: "We should also be able to resize the modals that we're
   editing in." A grip at the bottom-right corner of every wide dialog drags it
   to a new size; the size is remembered per dialog id and comes back the next
   time that dialog opens. Double-click the grip to go back to the default.

   Small dialogs — confirms and prompts (.m-pop) — never get a grip. */

const _RESIZABLE_SEL = '.modal.modal-wide, .modal.modal-xwide, .modal.modal-flow, .modal[data-resizable]';
const _MODAL_SIZE_KEY = 'vigil.modalSize.';

//: Clamp a dragged size to the whole window, rounding to whole pixels. The max
//: wins when the window is smaller than the minimum, so clamp min first.
function modalClampSize(w, h, vw, vh) {
  const maxW = vw - 32;
  const maxH = vh - 32;
  return {
    w: Math.round(Math.min(maxW, Math.max(360, w))),
    h: Math.round(Math.min(maxH, Math.max(240, h))),
  };
}

function _readModalSize(el) {
  if (!el.id) return null;
  try {
    const raw = localStorage.getItem(_MODAL_SIZE_KEY + el.id);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (!s || typeof s.w !== 'number' || typeof s.h !== 'number') return null;
    return s;
  } catch (e) { return null; }   // storage can throw in a private window
}

function _saveModalSize(el, w, h) {
  if (!el.id) return;
  // storage can throw in a private window
  try { localStorage.setItem(_MODAL_SIZE_KEY + el.id, JSON.stringify({ w: w, h: h })); } catch (e) { /* state-ok: a blocked storage area keeps the size for this visit only */ }
}

function _forgetModalSize(el) {
  if (!el.id) return;
  // storage can throw in a private window
  try { localStorage.removeItem(_MODAL_SIZE_KEY + el.id); } catch (e) { /* state-ok: a blocked storage area has nothing stored to forget */ }
}

//: The dialog's layout size. Not getBoundingClientRect(): that includes the
//: open animation's scale (and .recede's 0.95), which would save a size a few
//: percent smaller than the one the user dragged to.
function _modalLayoutSize(el) {
  return { width: el.offsetWidth, height: el.offsetHeight };
}

function _applyModalSize(el, w, h) {
  el.style.width = w + 'px';
  el.style.height = h + 'px';
  el.style.maxHeight = 'none';
  el.style.maxWidth = 'none';
  el.classList.add('is-resized');
}

function _resetModalSize(el) {
  el.style.width = '';
  el.style.height = '';
  el.style.maxHeight = '';
  el.style.maxWidth = '';
  el.classList.remove('is-resized');
  _forgetModalSize(el);
}

//: The dialog is centred by translate(-50%, -50%), so it grows in both
//: directions at once and only half of the pointer's movement reaches the
//: corner. Double the delta to keep the grip under the pointer.
function _modalDragStart(el, grip, ev) {
  ev.preventDefault();
  const rect = _modalLayoutSize(el);
  const startW = rect.width;
  const startH = rect.height;
  const startX = ev.clientX;
  const startY = ev.clientY;
  // setPointerCapture is best-effort: the id can be gone by the time we get here
  try { grip.setPointerCapture(ev.pointerId); } catch (e) { /* state-ok: the pointer can be gone already; the drag still tracks on move events */ }

  const move = mv => {
    const size = modalClampSize(startW + 2 * (mv.clientX - startX),
                                startH + 2 * (mv.clientY - startY),
                                window.innerWidth, window.innerHeight);
    _applyModalSize(el, size.w, size.h);
  };
  const stop = mv => {
    grip.removeEventListener('pointermove', move);
    grip.removeEventListener('pointerup', stop);
    grip.removeEventListener('pointercancel', stop);
    // the pointer is already up when pointercancel ends the drag
    try { grip.releasePointerCapture(mv.pointerId); } catch (e) { /* state-ok: a cleanup on a pointer already released */ }
    const now = _modalLayoutSize(el);
    const size = modalClampSize(now.width, now.height, window.innerWidth, window.innerHeight);
    _saveModalSize(el, size.w, size.h);
    // Canvases inside the dialog measure themselves on resize; tell them again.
    window.dispatchEvent(new Event('resize'));
  };
  grip.addEventListener('pointermove', move);
  grip.addEventListener('pointerup', stop);
  grip.addEventListener('pointercancel', stop);
}

function _resizableInit(el) {
  if (!el.matches(_RESIZABLE_SEL) || el.matches('.m-pop')) return;

  if (!el.dataset.resizableReady) {
    el.dataset.resizableReady = '1';
    const grip = document.createElement('button');
    grip.type = 'button';
    grip.className = 'modal-grip';
    grip.setAttribute('aria-label', 'Resize dialog');
    grip.title = 'Drag to resize · double-click to reset';
    grip.addEventListener('pointerdown', ev => _modalDragStart(el, grip, ev));
    grip.addEventListener('dblclick', () => _resetModalSize(el));
    grip.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 64 : 16;
      let dW = 0, dH = 0;
      if (ev.key === 'ArrowRight') dW = step;
      else if (ev.key === 'ArrowLeft') dW = -step;
      else if (ev.key === 'ArrowDown') dH = step;
      else if (ev.key === 'ArrowUp') dH = -step;
      else return;                  // Escape in particular: the window handler owns it
      ev.preventDefault();
      const rect = _modalLayoutSize(el);
      const size = modalClampSize(rect.width + dW, rect.height + dH, window.innerWidth, window.innerHeight);
      _applyModalSize(el, size.w, size.h);
      _saveModalSize(el, size.w, size.h);
    });
    el.appendChild(grip);
  }
  _windowChrome(el);

  const saved = _readModalSize(el);
  if (saved) {
    const size = modalClampSize(saved.w, saved.h, window.innerWidth, window.innerHeight);
    _applyModalSize(el, size.w, size.h);
  }
}

// A dialog the window has shrunk under slides back on screen.
window.addEventListener('resize', () => {
  document.querySelectorAll('.modal.is-resized').forEach(el => {
    if (!_isOpen(el)) return;
    const rect = _modalLayoutSize(el);
    const size = modalClampSize(rect.width, rect.height, window.innerWidth, window.innerHeight);
    _applyModalSize(el, size.w, size.h);
  });
});


/* ── Windows: move, minimise, float, pop out (2026.17 rc2) ──────────────────
   User, 2026-11-23: "It would also be cool if we could minimize them and move
   them around so we can have multiple at once open to work on things, maybe
   even pop them into their own browser tab."

   Every resizable dialog gets two buttons beside its close button and a
   draggable title:
   - Drag the title and the dialog becomes a window: its scrim goes, the page
     behind works again, and another dialog can open beside it. Clicking a
     window brings it to the front.
   - Minimise parks it in the dock at the bottom of the screen with its title;
     clicking the chip brings it back exactly as it was (a live shell keeps
     running).
   - Pop out, only on a dialog that names a link in data-popout ("#open/task/
     <id>" and the like, see vigil-deeplink.js), opens that in a new tab.

   The module that owns a dialog never learns any of this: it still adds and
   removes `open`, and closing a window by any route puts it back to normal. */

const _WINDOW_Z = 150;          // under every modal scrim (MODAL_BASE_Z = 200)
let _windowOrder = [];          // floating windows, back to front

function _windowTitleText(el) {
  const title = el.querySelector('.modal-title');
  const span = title && title.querySelector('span, h2, h3');
  const text = ((span && span.textContent) || (title && title.textContent) || '').trim();
  return (text || t('Dialog')).replace(/\s+/g, ' ').slice(0, 48);
}

function _windowButton(cls, label, svg) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'modal-close modal-winbtn ' + cls;
  b.setAttribute('aria-label', label);
  b.title = label;
  b.innerHTML = svg;
  return b;
}

function _windowChrome(el) {
  const title = el.querySelector('.modal-title');
  if (!title) return;
  let group = title.querySelector('.modal-winbtns');
  if (!group) {
    const close = title.querySelector('.modal-close');
    group = document.createElement('span');
    group.className = 'modal-winbtns';
    const min = _windowButton('modal-min', t('Minimise'),
      '<svg viewBox="0 0 24 24"><line x1="6" y1="18" x2="18" y2="18"/></svg>');
    min.addEventListener('click', () => windowMinimise(el));
    group.appendChild(min);
    if (close) { close.before(group); group.appendChild(close); } else title.appendChild(group);
  }
  // One dialog shows a different item each time it opens: the pop-out button
  // follows whatever link the opener set on it this time, if any.
  let pop = group.querySelector('.modal-pop');
  if (el.dataset.popout && !pop) {
    pop = _windowButton('modal-pop', t('Open in a new tab'),
      '<svg viewBox="0 0 24 24"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>');
    pop.addEventListener('click', () => _windowPopOut(el));
    group.prepend(pop);
  } else if (!el.dataset.popout && pop) {
    pop.remove();
  }
  if (!title.dataset.dragReady) {
    title.dataset.dragReady = '1';
    title.classList.add('modal-title-drag');
    title.addEventListener('pointerdown', ev => _windowDragStart(el, title, ev));
  }
  if (!el.dataset.windowReady) {
    el.dataset.windowReady = '1';
    el.addEventListener('pointerdown', () => { if (el.classList.contains('floating')) _windowRaise(el); }, true);
  }
}

function _windowPopOut(el) {
  const link = el.dataset.popout || '';
  if (!/^#open\/[a-z]+\/[0-9A-Za-z-]{1,64}$/.test(link)) return;
  window.open(location.pathname + link, '_blank', 'noopener');
}

function _windowRaise(el) {
  _windowOrder = _windowOrder.filter(w => w !== el && w.isConnected);
  _windowOrder.push(el);
  _windowOrder.forEach((w, i) => { w.style.zIndex = String(_WINDOW_Z + i * 2); });
}

//: Turn a dialog into a window: no scrim, out of the modal stack.
function _windowFloat(el) {
  if (el.classList.contains('floating')) return;
  _overlayFor(el)?.classList.add('released');
  el.classList.add('floating');
  _windowRaise(el);
}

function _windowDragStart(el, title, ev) {
  if (ev.button !== 0 || /** @type {HTMLElement} */ (ev.target).closest('button, a, input, select, textarea, [contenteditable]')) return;
  if (window.innerWidth <= 640) return;
  ev.preventDefault();
  const rect = el.getBoundingClientRect();
  // The dialog is centred on its left/top by translate(-50%, -50%).
  const startX = rect.left + rect.width / 2;
  const startY = rect.top + rect.height / 2;
  const fromX = ev.clientX;
  const fromY = ev.clientY;
  let moved = false;
  try { title.setPointerCapture(ev.pointerId); } catch (e) { /* state-ok: the pointer can be gone already; move events still arrive */ }
  const move = mv => {
    const dx = mv.clientX - fromX;
    const dy = mv.clientY - fromY;
    if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    if (!moved) { moved = true; el.classList.add('is-resized'); _windowFloat(el); }
    // The whole window stays on screen (title and close button included);
    // one wider than the screen can only slide as far as its own overflow.
    const halfW = rect.width / 2;
    const halfH = rect.height / 2;
    const clamp = (v, lo, hi) => (lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)));
    const x = clamp(startX + dx, halfW + 8, window.innerWidth - halfW - 8);
    const y = clamp(startY + dy, halfH + 8, window.innerHeight - halfH - 8);
    el.style.left = x + 'px';
    el.style.top = y + 'px';
  };
  const stop = up => {
    title.removeEventListener('pointermove', move);
    title.removeEventListener('pointerup', stop);
    title.removeEventListener('pointercancel', stop);
    try { title.releasePointerCapture(up.pointerId); } catch (e) { /* state-ok: a cleanup on a pointer already released */ }
  };
  title.addEventListener('pointermove', move);
  title.addEventListener('pointerup', stop);
  title.addEventListener('pointercancel', stop);
}

function _windowDock() {
  let dock = document.getElementById('modal-dock');
  if (!dock) {
    dock = document.createElement('div');
    dock.id = 'modal-dock';
    dock.className = 'modal-dock';
    dock.setAttribute('role', 'toolbar');
    dock.setAttribute('aria-label', t('Minimised dialogs'));
    document.body.appendChild(dock);
  }
  return dock;
}

/** Park an open dialog in the dock. @param {HTMLElement} el */
function windowMinimise(el) {
  if (!_isOpen(el) || el.classList.contains('minimized')) return;
  el.classList.add('minimized');
  _overlayFor(el)?.classList.add('released');
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'modal-dock-chip';
  chip.dataset.for = el.id || '';
  chip.textContent = _windowTitleText(el);
  chip.title = t('Restore');
  chip.addEventListener('click', () => windowRestore(el));
  /** @type {any} */ (chip)._vigilWindow = el;
  _windowDock().appendChild(chip);
}

/** Bring a minimised dialog back. @param {HTMLElement} el */
function windowRestore(el) {
  _windowChipFor(el)?.remove();
  el.classList.remove('minimized');
  if (!el.classList.contains('floating')) _overlayFor(el)?.classList.remove('released');
  else _windowRaise(el);
  window.dispatchEvent(new Event('resize'));
}

function _windowChipFor(el) {
  const dock = document.getElementById('modal-dock');
  if (!dock) return null;
  return [...dock.children].find(c => /** @type {any} */ (c)._vigilWindow === el) || null;
}

//: A window closed by any route goes back to an ordinary dialog, so the next
//: open starts centred and modal.
function _windowCleanup() {
  document.querySelectorAll('.modal.floating, .modal.minimized').forEach(node => {
    const el = /** @type {HTMLElement} */ (node);
    if (_isOpen(el)) return;
    el.classList.remove('floating', 'minimized');
    el.style.left = '';
    el.style.top = '';
    _overlayFor(el)?.classList.remove('released');
    _windowChipFor(el)?.remove();
    _windowOrder = _windowOrder.filter(w => w !== el);
  });
  // A chip whose dialog left the page entirely.
  const dock = document.getElementById('modal-dock');
  if (dock) [...dock.children].forEach(c => { if (!(/** @type {any} */ (c)._vigilWindow || {}).isConnected) c.remove(); });
}
