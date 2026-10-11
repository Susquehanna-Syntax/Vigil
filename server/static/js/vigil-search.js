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

const searchState = { seq: 0, timer: null };

function _srchEl(id) { return document.getElementById(id); }

function _renderSearch(body) {
  const out = _srchEl('srch-results');
  if (!body.groups.length) {
    out.innerHTML = `<div class="apps-empty-state">Nothing matched “${escHtml(body.query)}”.</div>`;
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

async function runSearch() {
  const q = _srchEl('srch-input').value.trim();
  const seq = ++searchState.seq;
  if (q.length < 2) { _srchEl('srch-results').innerHTML = ''; return; }
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
  const input = _srchEl('srch-input');
  if (input) { input.focus(); input.select(); }
}

delegateClick('[data-search-kind]', (el) => openSearchResult(el.dataset));

document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'srch-input') return;
  clearTimeout(searchState.timer);
  searchState.timer = setTimeout(runSearch, 250);
});

document.addEventListener('keydown', (ev) => {
  const t = ev.target;
  const typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
  if (((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k' && !ev.altKey && !ev.shiftKey)
      || (ev.key === '/' && !typing && !ev.ctrlKey && !ev.metaKey && !ev.altKey)) {
    ev.preventDefault();
    focusSearch();
    return;
  }
  // Arrow keys walk from the box through the result rows and back.
  if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
  const rows = [...document.querySelectorAll('#srch-results .srch-row')];
  if (t && t.id === 'srch-input') {
    if (ev.key === 'ArrowDown' && rows.length) { ev.preventDefault(); rows[0].focus(); }
    return;
  }
  const i = rows.indexOf(t);
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
    if (pageName === 'search') setTimeout(() => _srchEl('srch-input')?.focus(), 0);
  };
})();
