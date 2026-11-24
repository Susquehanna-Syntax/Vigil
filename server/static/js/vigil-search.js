// @ts-check
// vigil-search.js
// Owns: the Search page (QA-25) — one box over hosts, containers, stacks, apps,
//   vulnerabilities, tasks, playbooks, update policies and firing alerts, each
//   only as far as the user may already see it; Ctrl+K (or /) jumps here.
// HTML: #page-search in templates/pages/_search.html.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr, delegateClick),
//   vigil-nav.js (navigateTo — wrapped below), and the pages a result opens:
//   vigil-monitor.js (selectMonitorHost), vigil-containers.js
//   (selectContainersHost), vigil-stacks.js (renderManagedStacks,
//   openStackEditor), vigil-tasks.js (openDefinitionEditor), vigil-playbooks.js
//   (_startEdit), vigil-policies.js (openPolicyEditor).
// API: GET /api/v1/search/?q=

/** @type {{seq: number, timer: ReturnType<typeof setTimeout>|null}} */
const searchState = { seq: 0, timer: null };

/** @param {string} id @returns {HTMLElement|null} */
function _srchEl(id) { return document.getElementById(id); }

/** @param {any} body @returns {void} */
function _renderSearch(body) {
  const out = _srchEl('srch-results');
  if (!body.groups.length) {
    out.innerHTML = `<div class="apps-empty-state">${escHtml(t('Nothing matched “{query}”.', { query: body.query }))}</div>`;
    return;
  }
  out.innerHTML = body.groups.map(g => `<div class="srch-group">
      <div class="srch-label">${escHtml(g.label)} <span class="srch-count">${g.results.length}</span></div>
      ${g.results.map(r => `<button type="button" class="srch-row" data-search-kind="${escAttr(r.kind)}"
          data-search-id="${escAttr(r.id)}" data-search-host="${escAttr(r.host_id || '')}">
          <span class="srch-title">${escHtml(r.title)}</span>
          ${r.subtitle ? `<span class="srch-sub">${escHtml(r.subtitle)}</span>` : ''}
        </button>`).join('')}
    </div>`).join('');
}

/* ── Suggestions: what the page shows before anything is typed ───────────
   An empty Search page read as broken. Three columns instead, each a click
   away: the searches this browser ran recently, a few of the hosts the user
   can see, and the pages people most often go looking for. */

const SEARCH_RECENT_KEY = 'vigil.search.recent';

/** @returns {string[]} */
function _recentSearches() {
  try {
    const list = JSON.parse(localStorage.getItem(SEARCH_RECENT_KEY) || '[]');
    return Array.isArray(list) ? list.filter(q => typeof q === 'string').slice(0, 6) : [];
  } catch (e) { return []; /* state-ok: storage can throw in a private window; no recent list then */ }
}

/** @param {string} q */
function _rememberSearch(q) {
  if (!q || q.length < 2) return;
  const list = [q, ..._recentSearches().filter(x => x.toLowerCase() !== q.toLowerCase())].slice(0, 6);
  try { localStorage.setItem(SEARCH_RECENT_KEY, JSON.stringify(list)); } catch (e) { /* state-ok: a blocked storage area just has no recent list */ }
}

//: The pages a search most often stands in for. Labels are the sidebar's.
const SEARCH_JUMPS = () => [
  ['alerts', t('Alerts')], ['monitor', t('Monitor')], ['containers', t('Containers')],
  ['vulns', t('Vulnerabilities')], ['tasks', t('Tasks')], ['playbooks', t('Deployments')],
  ['apps', t('Apps')], ['settings', t('Settings')],
];

async function _renderSuggestions() {
  const out = _srchEl('srch-results');
  if (!out) return;
  const seq = searchState.seq;
  const recent = _recentSearches();
  let hosts = [];
  try {
    const rows = await apiJson('/api/v1/hosts/');
    hosts = (Array.isArray(rows) ? rows : (rows.results || []))
      .filter(h => h.status !== 'pending')
      .sort((a, b) => (a.status === 'online' ? 0 : 1) - (b.status === 'online' ? 0 : 1)
        || String(a.hostname).localeCompare(String(b.hostname)))
      .slice(0, 6);
  } catch (e) { hosts = []; /* state-ok: the other two columns still help without the host list */ }
  if (seq !== searchState.seq) return;   // the user typed meanwhile
  const group = (label, rows) => `<div class="srch-group">
      <div class="srch-label">${escHtml(label)}</div>${rows}</div>`;
  const parts = [];
  if (recent.length) {
    parts.push(group(t('Recent searches'), recent.map(q => `<button type="button" class="srch-row" data-search-recent="${escAttr(q)}">
        <span class="srch-title">${escHtml(q)}</span></button>`).join('')));
  }
  if (hosts.length) {
    parts.push(group(t('Hosts'), hosts.map(h => `<button type="button" class="srch-row" data-search-kind="host" data-search-id="${escAttr(h.id)}">
        <span class="srch-title">${escHtml(h.hostname)}</span>
        <span class="srch-sub">${escHtml([h.status, h.os].filter(Boolean).join(' · '))}</span></button>`).join('')));
  }
  parts.push(group(t('Jump to'), SEARCH_JUMPS().map(([page, label]) => `<button type="button" class="srch-row" data-search-page="${escAttr(page)}">
      <span class="srch-title">${escHtml(label)}</span></button>`).join('')));
  out.innerHTML = parts.join('');
}

async function runSearch() {
  const q = /** @type {HTMLInputElement} */ (_srchEl('srch-input')).value.trim();
  const seq = ++searchState.seq;
  if (q.length < 2) { _renderSuggestions(); return; }
  try {
    const body = await apiJson(`/api/v1/search/?q=${encodeURIComponent(q)}`);
    if (seq !== searchState.seq) return;   // a newer query is on its way
    _renderSearch(body);
  } catch (e) {
    if (seq !== searchState.seq) return;
    _srchEl('srch-results').innerHTML = `<div class="apps-empty-state">${escHtml(e.message)}</div>`;
  }
}

/** Open a result where it lives. */
async function openSearchResult(d) {
  const id = d.searchId;
  switch (d.searchKind) {
    case 'host':
      navigateTo('monitor');
      selectMonitorHost(id);
      break;
    case 'container':
      navigateTo('containers');
      selectContainersHost(d.searchHost);
      break;
    case 'stack':
      navigateTo('containers');
      selectContainersHost(d.searchHost);
      await renderManagedStacks(d.searchHost);
      openStackEditor(id);
      break;
    case 'app': navigateTo('apps'); break;
    case 'cve': navigateTo('vulns'); break;
    case 'task': openDefinitionEditor(id); break;
    case 'playbook':
      navigateTo('playbooks');
      _startEdit(id);
      break;
    case 'policy':
      navigateTo('policies');
      openPolicyEditor(id);
      break;
    case 'alert': navigateTo('alerts'); break;
    default: break;
  }
}

function focusSearch() {
  navigateTo('search');
  const input = /** @type {HTMLInputElement} */ (_srchEl('srch-input'));
  if (input) { input.focus(); input.select(); }
}

delegateClick('[data-search-kind]', (el) => {
  _rememberSearch(/** @type {HTMLInputElement} */ (_srchEl('srch-input')).value.trim());
  openSearchResult(el.dataset);
});
delegateClick('[data-search-recent]', (el) => {
  const input = /** @type {HTMLInputElement} */ (_srchEl('srch-input'));
  input.value = el.dataset.searchRecent || '';
  input.focus();
  runSearch();
});
delegateClick('[data-search-page]', (el) => navigateTo(el.dataset.searchPage));

document.addEventListener('input', (ev) => {
  if (/** @type {HTMLElement} */ (ev.target).id !== 'srch-input') return;
  clearTimeout(searchState.timer);
  searchState.timer = setTimeout(runSearch, 250);
});

document.addEventListener('keydown', (ev) => {
  const target = /** @type {HTMLElement} */ (ev.target);
  const typing = target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  if (((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k' && !ev.altKey && !ev.shiftKey)
      || (ev.key === '/' && !typing && !ev.ctrlKey && !ev.metaKey && !ev.altKey)) {
    ev.preventDefault();
    focusSearch();
    return;
  }
  // Arrow keys walk from the box through the result rows and back.
  if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
  const rows = /** @type {HTMLElement[]} */ ([...document.querySelectorAll('#srch-results .srch-row')]);
  if (target && target.id === 'srch-input') {
    if (ev.key === 'ArrowDown' && rows.length) { ev.preventDefault(); rows[0].focus(); }
    return;
  }
  const i = rows.indexOf(target);
  if (i < 0) return;
  ev.preventDefault();
  if (ev.key === 'ArrowDown') (rows[i + 1] || rows[i]).focus();
  else if (i === 0) _srchEl('srch-input').focus();
  else rows[i - 1].focus();
});

/* Opening the page puts the cursor in the box. */
(function () {
  const previous = window.navigateTo;
  if (typeof previous !== 'function') return;
  window.navigateTo = function (pageName) {
    previous.apply(this, arguments);
    if (pageName === 'search') {
      setTimeout(() => _srchEl('srch-input')?.focus(), 0);
      const input = /** @type {HTMLInputElement|null} */ (_srchEl('srch-input'));
      if (input && input.value.trim().length < 2) _renderSuggestions();
    }
  };
})();
