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
}

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
