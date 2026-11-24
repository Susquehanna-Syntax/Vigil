// @ts-check
// vigil-nav.js
// Owns: page navigation (sidebar icons → page sections) and tab-bar switching.
// Depends on: vigil-utils.js
// Note: feature files (monitor, tasks, deploy, vulns, settings, inventory) wrap
//   navigateTo() to fire page-specific load actions. Each wrap stacks on the
//   previous one, so script load order in base.html matters.

/* ── Sidebar navigation ──────────────────────────────────────────────── */
const sidebarIcons = document.querySelectorAll('.sidebar-icon[data-page]');
const pages = document.querySelectorAll('.page');

function navigateTo(pageName) {
  pages.forEach(p => p.classList.remove('active'));
  sidebarIcons.forEach(i => i.classList.remove('active'));
  const page = document.getElementById('page-' + pageName);
  const icon = document.querySelector(`.sidebar-icon[data-page="${pageName}"]`);
  if (page) page.classList.add('active');
  if (icon) icon.classList.add('active');
  document.querySelector('.main').scrollTop = 0;
  _sbFollowPage(pageName, icon);
}

/* ── Sidebar groups (design branch) ──────────────────────────────────────
   The page's group colours the whole app (data-group on <html>, read by
   16-groups.css as --grp). In the expanded sidebar a group folds from its
   heading and a page with sections opens a list of them, like Cloudflare's
   dashboard; both are remembered in this browser. A section jumps straight
   to that tab of its page. */

const SB_FOLDED_KEY = 'vigil.sb.folded';
const SB_OPEN_KEY = 'vigil.sb.open';

/** @param {string} key @returns {Set<string>} */
function _sbRead(key) {
  try { return new Set(JSON.parse(localStorage.getItem(key) || '[]')); } catch (e) { return new Set(); /* state-ok: no stored layout, everything starts open */ }
}

/** @param {string} key @param {Set<string>} set */
function _sbWrite(key, set) {
  try { localStorage.setItem(key, JSON.stringify([...set])); } catch (e) { /* state-ok: the layout lasts this visit */ }
}

function _sbSetFolded(group, folded) {
  const box = document.querySelector(`.sb-group[data-group="${group}"]`);
  if (!box) return;
  box.classList.toggle('is-folded', folded);
  box.querySelector('.sb-group-head')?.setAttribute('aria-expanded', String(!folded));
}

function _sbSetOpen(page, open) {
  const subs = /** @type {HTMLElement|null} */ (document.querySelector(`[data-sb-subs="${page}"]`));
  if (!subs) return;
  subs.hidden = !open;
  document.querySelector(`[data-sb-sub-toggle="${page}"]`)?.setAttribute('aria-expanded', String(open));
}

/** The section of *page* that is showing, as the sidebar names it. */
function _sbMarkCurrent(page, key) {
  document.querySelectorAll('.sb-sub.is-current').forEach(b => b.classList.remove('is-current'));
  if (!key) return;
  document.querySelector(`.sb-sub[data-sb-goto="${page}"][data-sb-key="${key}"]`)?.classList.add('is-current');
}

/** The tab element on a page that a sidebar section stands for. */
function _sbTabFor(page, kind, key) {
  const root = document.getElementById('page-' + page);
  if (!root) return null;
  const sel = kind === 'subtab' ? `.sub-tab[data-subtab="${key}"]`
    : kind === 'ctr-tab' ? `[data-ctr-tab="${key}"]` : `.tab[data-tab="${key}"]`;
  return /** @type {HTMLElement|null} */ (root.querySelector(sel));
}

/** Which section of *page* is active right now, if it has any. */
function _sbActiveKey(page) {
  const subs = document.querySelectorAll(`.sb-sub[data-sb-goto="${page}"]`);
  for (const b of subs) {
    const el = /** @type {HTMLElement} */ (b);
    const tab = _sbTabFor(page, el.dataset.sbKind, el.dataset.sbKey);
    if (tab && (tab.classList.contains('active') || tab.getAttribute('aria-selected') === 'true')) return el.dataset.sbKey;
  }
  return '';
}

function _sbFollowPage(pageName, icon) {
  const group = icon ? icon.closest('.sb-group') : null;
  const name = group ? /** @type {HTMLElement} */ (group).dataset.group : '';
  if (name) document.documentElement.dataset.group = name;
  else delete document.documentElement.dataset.group;
  // A folded group opens to show the page you are on, and so does its list.
  if (name && group.classList.contains('is-folded')) _sbSetFolded(name, false);
  if (document.querySelector(`[data-sb-subs="${pageName}"]`)) _sbSetOpen(pageName, true);
  _sbMarkCurrent(pageName, _sbActiveKey(pageName));
}

document.addEventListener('DOMContentLoaded', () => {
  _sbRead(SB_FOLDED_KEY).forEach(g => _sbSetFolded(g, true));
  _sbRead(SB_OPEN_KEY).forEach(p => _sbSetOpen(p, true));
  const active = /** @type {HTMLElement|null} */ (document.querySelector('.sidebar-icon[data-page].active'));
  if (active) _sbFollowPage(active.dataset.page, active);
});

document.addEventListener('click', (ev) => {
  const target = /** @type {HTMLElement} */ (ev.target);
  const head = /** @type {HTMLElement|null} */ (target.closest('[data-sb-group-toggle]'));
  if (head) {
    const group = head.dataset.sbGroupToggle;
    const folded = !head.closest('.sb-group').classList.contains('is-folded');
    _sbSetFolded(group, folded);
    const set = _sbRead(SB_FOLDED_KEY);
    if (folded) set.add(group); else set.delete(group);
    _sbWrite(SB_FOLDED_KEY, set);
    return;
  }
  const toggle = /** @type {HTMLElement|null} */ (target.closest('[data-sb-sub-toggle]'));
  if (toggle) {
    const page = toggle.dataset.sbSubToggle;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    _sbSetOpen(page, open);
    const set = _sbRead(SB_OPEN_KEY);
    if (open) set.add(page); else set.delete(page);
    _sbWrite(SB_OPEN_KEY, set);
    return;
  }
  const sub = /** @type {HTMLElement|null} */ (target.closest('[data-sb-goto]'));
  if (sub) {
    const page = sub.dataset.sbGoto;
    navigateTo(page);
    const tab = _sbTabFor(page, sub.dataset.sbKind, sub.dataset.sbKey);
    if (tab) tab.click();
    _sbMarkCurrent(page, sub.dataset.sbKey);
    return;
  }
  // A tab clicked on the page itself moves the sidebar's marker with it.
  const pageTab = /** @type {HTMLElement|null} */ (target.closest('.page .tab[data-tab], .page .sub-tab[data-subtab], .page [data-ctr-tab]'));
  if (pageTab) {
    const page = pageTab.closest('.page').id.replace(/^page-/, '');
    const key = pageTab.dataset.tab || pageTab.dataset.subtab || pageTab.dataset.ctrTab || '';
    _sbMarkCurrent(page, key);
  }
});

sidebarIcons.forEach((/** @type {HTMLElement} */ icon) => {
  icon.addEventListener('click', () => navigateTo(icon.dataset.page));
});

/* ── Tab bars (alerts, tasks history) ────────────────────────────────── */
// Every .tab-bar is a WAI-ARIA tablist whose selected tab is its only stop in the tab order, and arrow keys,
// Home and End move between them. Other scripts hang their own work off clicks
// on .tab — vigil-tasks.js reloads its library that way — so activation goes
// through click() rather than activate() direct, and every key finishes with the
// focus on the tab it selected: a browser gives it to a clicked tab, and leaving
// the keyboard behind on a tab that is no longer selected strands the user.
function initTabBar(bar) {
  /** @type {HTMLElement} */
  const list = /** @type {HTMLElement} */ (bar);
  if (list.dataset.tabsInit === '1') return;      // tabs can be added later
  list.dataset.tabsInit = '1';
  list.setAttribute('role', 'tablist');
  /** @type {HTMLElement[]} */
  const tabs = Array.from(list.querySelectorAll('.tab'));
  let activeIndex = tabs.findIndex(t => t.classList.contains('active'));
  if (activeIndex < 0) activeIndex = 0;
  tabs.forEach((tab, i) => {
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(i === activeIndex));
    tab.setAttribute('tabindex', i === activeIndex ? '0' : '-1');
    const panelId = tab.dataset.tab;
    if (!tab.id && panelId) tab.id = 'tab-' + panelId;
    const panel = panelId ? document.getElementById(panelId) : null;
    if (panel && tab.id) {
      tab.setAttribute('aria-controls', /** @type {HTMLElement} */ (panel).id);
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', tab.id);
    }
  });

  function activate(tab) {
    tabs.forEach(t => {
      const on = t === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', String(on));
      t.setAttribute('tabindex', on ? '0' : '-1');
    });
    const group = list.closest('.page');
    if (!group) return;
    group.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    const target = document.getElementById(tab.dataset.tab);
    if (target) target.classList.add('active');
  }

  tabs.forEach(tab => {
    tab.addEventListener('click', () => activate(tab));
  });

  list.addEventListener('keydown', (/** @type {KeyboardEvent} */ ev) => {
    const target = /** @type {HTMLElement} */ (ev.target);
    if (!target.classList || !target.classList.contains('tab')) return;
    const index = tabs.indexOf(target);
    let next = null;
    switch (ev.key) {
      case 'ArrowRight': next = tabs[(index + 1) % tabs.length]; break;
      case 'ArrowLeft': next = tabs[(index - 1 + tabs.length) % tabs.length]; break;
      case 'Home': next = tabs[0]; break;
      case 'End': next = tabs[tabs.length - 1]; break;
      // Space would scroll the page, Enter would submit an enclosing form; both
      // mean "use this tab" here, and the activation is a click on it.
      case 'Enter':
      case ' ':
        ev.preventDefault();
        target.click();
        return;
      default: return;                            // any other key belongs to the page
    }
    if (!next) return;
    ev.preventDefault();
    next.click();
    next.focus();
  });
}

document.querySelectorAll('.tab-bar').forEach(/** @param {HTMLElement} bar */ initTabBar);
