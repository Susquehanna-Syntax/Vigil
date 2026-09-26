// vigil-hunts.js
// Owns: the hunt results page. One page per run, opened from a run-detail
// modal ("Open hunt results"). The matches table's columns come from the
// run's own `columns` payload, so a new hunt field needs no UI change.

const HUNT_PAGE_SIZE = 100;

const huntResults = { runId: null, offset: 0, total: 0, columns: [], stepOptions: [] };

const HUNT_STATE_TONE = {
  matched: 'mint',
  not_matched: 'lav',
  pending: 'peach',
  error: 'rose',
  did_not_report: 'lemon',
  not_applicable: 'lav',
};

const HUNT_STATE_LABEL = {
  matched: 'Matched',
  not_matched: 'Not matched',
  pending: 'Pending',
  error: 'Error',
  did_not_report: 'Did not report',
  not_applicable: 'Not applicable',
};

function _huntDataUrl(offset) {
  const params = new URLSearchParams({ limit: String(HUNT_PAGE_SIZE), offset: String(offset || 0) });
  const step = (document.getElementById('hunt-step')?.value || '').trim();
  const host = huntResults.hostFilter || '';
  if (step) params.set('step', step);
  if (host) params.set('host', host);
  return `/api/v1/tasks/runs/${huntResults.runId}/hunt/?${params.toString()}`;
}

function _huntFetch(offset) {
  return apiJson(_huntDataUrl(offset));
}

async function openHuntResults(runId) {
  if (!runId) return;
  // Back returns to whichever page the run was opened from (Tasks or Playbooks).
  const from = document.querySelector('section.page.active');
  const fromName = from ? from.id.replace(/^page-/, '') : '';
  if (fromName && fromName !== 'hunt-results') huntResults.backTo = fromName;
  navigateTo('hunt-results');
  const tbody = document.getElementById('hunt-matches-body');
  if (tbody) tbody.replaceChildren();
  huntResults.runId = runId;
  huntResults.hostFilter = '';
  huntResults.offset = 0;
  huntResults.total = 0;
  huntResults.columns = [];
  huntResults.stepOptions = [];

  const filter = document.getElementById('hunt-filter');
  const stepSel = document.getElementById('hunt-step');
  const stateSel = document.getElementById('hunt-state');
  if (filter) filter.value = '';
  if (stepSel) stepSel.value = '';
  if (stateSel) stateSel.value = '';

  let body;
  try { body = await _huntFetch(0); }
  catch (e) {
    showToast('Could not load hunt results for that run', 'error');
    return;
  }
  const run = await apiJson(`/api/v1/tasks/runs/${runId}/`).catch(() => null);
  const titleEl = document.getElementById('hunt-page-title');
  const subEl = document.getElementById('hunt-page-sub');
  if (titleEl) titleEl.textContent = 'Hunt Results';
  if (subEl) {
    const name = run ? (run.definition_name || run.name_snapshot || 'Run') : '';
    const state = run ? run.state : '';
    subEl.textContent = [name, state].filter(Boolean).join(' · ');
  }
  _huntSetData(body, true);
}

// True when any task of the run carried a hunt step. The run API ships
// params.steps with each step's action, so the button can be decided without
// a second request.
function _runHasHuntSteps(run) {
  const tasks = (run && run.tasks) || [];
  return tasks.some((t) => {
    const steps = ((t.params || {}).steps) || [];
    return steps.some((s) => s && typeof s === 'object'
      && String(s.action || '').startsWith('hunt_'));
  });
}

function _huntSetData(body, reset) {
  huntResults.total = body.total || 0;
  huntResults.columns = body.columns || [];

  if (reset) {
    // A refetch from offset 0 replaces the rows rather than appending a copy.
    const tbody = document.getElementById('hunt-matches-body');
    if (tbody) tbody.replaceChildren();
  }
  huntResults.offset = body.offset + (body.matches || []).length;
  _huntRenderChips(body.hosts || []);
  _huntRenderHosts(body.hosts || []);
  _huntRenderMatchesHead();
  _huntAppendMatches(body.matches || []);
  _huntRenderStepOptions();
  _huntApplyClientFilter();
  _huntMarkActiveChip();
}

function _huntRenderChips(hosts) {
  const el = document.getElementById('hunt-summary');
  if (!el) return;
  const counts = {};
  for (const h of hosts) counts[h.state] = (counts[h.state] || 0) + 1;
  const order = ['matched', 'not_matched', 'pending', 'error', 'did_not_report', 'not_applicable'];
  el.replaceChildren();
  for (const state of order) {
    if (!counts[state]) continue;
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'hunt-chip';
    chip.dataset.state = state;
    chip.innerHTML =
      `<span class="run-state t-${HUNT_STATE_TONE[state] || 'lav'}">${escHtml(HUNT_STATE_LABEL[state] || state)}</span>` +
      `<span class="hunt-chip-count">${escHtml(String(counts[state]))}</span>`;
    chip.addEventListener('click', () => {
      const sel = document.getElementById('hunt-state');
      if (!sel) return;
      sel.value = (sel.value === state) ? '' : state;
      _huntApplyClientFilter();
      _huntMarkActiveChip();
    });
    el.appendChild(chip);
  }
  if (!el.childNodes.length) {
    el.appendChild(document.createTextNode('No hosts reported on this run yet.'));
  }
}

function _huntMarkActiveChip() {
  const state = document.getElementById('hunt-state')?.value || '';
  document.querySelectorAll('#hunt-summary .hunt-chip').forEach((c) => {
    c.classList.toggle('active', c.dataset.state === state);
  });
}

function _huntRenderHosts(hosts) {
  const tbody = document.getElementById('hunt-hosts-body');
  if (!tbody) return;
  tbody.replaceChildren();
  for (const h of hosts) {
    const tr = document.createElement('tr');
    tr.dataset.host = h.host_id;
    tr.title = 'Show only this host\'s matches (click again for all hosts)';
    if (huntResults.hostFilter === h.host_id) tr.classList.add('selected');
    tr.dataset.state = h.state || '';
    const badges = [];
    if (h.truncated) badges.push('truncated');
    if (h.timed_out) badges.push('timed out');
    tr.innerHTML =
      `<td class="mono">${escHtml(h.hostname || '')}</td>` +
      `<td><span class="run-state t-${HUNT_STATE_TONE[h.state] || 'lav'}">${escHtml(HUNT_STATE_LABEL[h.state] || h.state || '')}</span></td>` +
      `<td>${escHtml(String(h.match_count ?? 0))}</td>` +
      `<td>${badges.map((b) => `<span class="hunt-badge">${escHtml(b)}</span>`).join('') || '<span class="muted">—</span>'}</td>`;
    tr.addEventListener('click', () => {
      huntResults.hostFilter = (huntResults.hostFilter === h.host_id) ? '' : h.host_id;
      _huntFetch(0).then((body) => _huntSetData(body, true)).catch((e) => {
        showToast('Could not refilter hunt results', 'error');
      });
    });
    tbody.appendChild(tr);
  }
}

function _huntStepSeen(stepId) {
  if (!stepId) return;
  if (!huntResults.stepOptions.includes(stepId)) huntResults.stepOptions.push(stepId);
}

function _huntRenderStepOptions() {
  const sel = document.getElementById('hunt-step');
  if (!sel) return;
  const current = sel.value;
  sel.replaceChildren();
  sel.appendChild(new Option('All steps', ''));
  for (const id of huntResults.stepOptions) sel.appendChild(new Option(id, id));
  if (current && sel.querySelector(`option[value="${escAttr(current)}"]`)) sel.value = current;
}

function _huntRenderMatchesHead() {
  const thead = document.getElementById('hunt-matches-head');
  if (!thead) return;
  const cols = ['hostname', 'step_id', 'evidence_type'].concat(huntResults.columns);
  thead.innerHTML = `<tr>${cols.map((c) => `<th>${escHtml(c)}</th>`).join('')}</tr>`;
}

function _huntMatchCells(m) {
  const cols = ['hostname', 'step_id', 'evidence_type'].concat(huntResults.columns);
  return cols.map((c) => `<td>${escHtml(String(m[c] ?? ''))}</td>`).join('');
}

function _huntAppendMatches(matches) {
  const tbody = document.getElementById('hunt-matches-body');
  if (!tbody) return;
  for (const m of matches) {
    _huntStepSeen(m.step_id);
    const tr = document.createElement('tr');
    tr.dataset.host = m.host_id || '';
    tr.innerHTML = _huntMatchCells(m);
    tbody.appendChild(tr);
  }
  const more = document.getElementById('hunt-more');
  if (more) more.hidden = huntResults.offset >= huntResults.total;
  const count = document.getElementById('hunt-matches-count');
  if (count) count.textContent = `· ${huntResults.total}`;
  const empty = document.getElementById('hunt-empty');
  if (empty) empty.hidden = huntResults.total > 0;
}

// The API's ?host= / ?step= filters matches only, so hosts and their states
// always arrive with the page. Client-side state filtering needs each match's
// host state: it is looked up from the hosts table of the same fetch.
function _huntHostStateOf(hostId) {
  const tr = document.querySelector(`#hunt-hosts-body tr[data-host="${escAttr(hostId)}"]`);
  return tr ? (tr.dataset.state || '') : '';
}

function _huntApplyClientFilter() {
  const q = (document.getElementById('hunt-filter')?.value || '').trim().toLowerCase();
  const state = document.getElementById('hunt-state')?.value || '';
  document.querySelectorAll('#hunt-hosts-body tr').forEach((tr) => {
    const stateOk = !state || tr.dataset.state === state;
    const qOk = !q || (tr.textContent || '').toLowerCase().includes(q);
    tr.style.display = (stateOk && qOk) ? '' : 'none';
  });
  document.querySelectorAll('#hunt-matches-body tr').forEach((tr) => {
    const stateOk = !state || _huntHostStateOf(tr.dataset.host) === state;
    const qOk = !q || (tr.textContent || '').toLowerCase().includes(q);
    tr.style.display = (stateOk && qOk) ? '' : 'none';
  });
}

function _huntExportCsv() {
  const tbody = document.getElementById('hunt-matches-body');
  const thead = document.getElementById('hunt-matches-head');
  if (!tbody || !thead) return;
  const headers = Array.from(thead.querySelectorAll('th')).map((th) => th.textContent);
  const rows = [];
  tbody.querySelectorAll('tr').forEach((tr) => {
    if (tr.style.display === 'none') return;
    rows.push(Array.from(tr.querySelectorAll('td')).map((td) => td.textContent));
  });

  // RFC 4180: quote every field, double embedded quotes. Cells that would be
  // interpreted as spreadsheet formulas get a leading ' — the exported file is
  // for humans, and an agent-reported value must not run a formula on open.
  const encode = (v) => {
    let s = String(v ?? '');
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
    return '"' + s.replace(/"/g, '""') + '"';
  };
  const csv = [headers, ...rows].map((r) => r.map(encode).join(',')).join('\r\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `hunt-results-${huntResults.runId || 'run'}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('hunt-filter')?.addEventListener('input', _huntApplyClientFilter);

  document.getElementById('hunt-step')?.addEventListener('change', () => {
    _huntFetch(0).then((body) => _huntSetData(body, true)).catch((e) => {
      showToast('Could not refilter hunt results', 'error');
    });
  });

  document.getElementById('hunt-state')?.addEventListener('change', () => {
    _huntApplyClientFilter();
    _huntMarkActiveChip();
  });

  document.getElementById('hunt-back')?.addEventListener('click', () => {
    navigateTo(huntResults.backTo || 'tasks');
  });

  document.getElementById('hunt-refresh')?.addEventListener('click', () => {
    if (!huntResults.runId) return;
    _huntFetch(0).then((body) => _huntSetData(body, true)).catch((e) => {
      showToast('Could not refresh hunt results', 'error');
    });
  });

  document.getElementById('hunt-more')?.addEventListener('click', () => {
    if (!huntResults.runId) return;
    _huntFetch(huntResults.offset).then((body) => _huntSetData(body, false)).catch((e) => {
      showToast('Could not load more matches', 'error');
    });
  });

  document.getElementById('hunt-export')?.addEventListener('click', _huntExportCsv);
});
