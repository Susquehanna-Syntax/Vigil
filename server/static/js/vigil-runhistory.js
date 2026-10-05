// vigil-runhistory.js
// Owns: Playbooks → History sub-tab. Lists TaskRuns produced by playbook and
// automation dispatch, newest first, with the outcome the run settled into.
//
// Before runs were attached to these dispatches, an execution left only loose
// Task rows identified by a step_label string, so there was nothing to show.

const runHistoryState = { page: 1, pages: 1, source: 'automation,playbook' };

const _RUN_STATE_ACCENT = {
  running: 'peach',
  completed: 'mint',
  partial: 'lemon',
  failed: 'rose',
  not_applicable: 'lav',
};

// The agent writes one line per step into the task output —
// "[OK] svc: …", "[SKIPPED] absent: branch b1.else not taken",
// "[PROBE] relevant-1 (hunt_process): 2 match(es)" — keyed here by step id so
// each row can say what happened in words next to its outputs.
const _STEP_LINE = /^\[(OK|ERROR|FAILED|SKIPPED|PROBE|NOT APPLICABLE)\]\s+([^:(]+?)(?:\s+\(([^)]+)\))?:\s*(.*)$/;

function _stepLines(output) {
  const byId = {};
  const notes = [];
  String(output || '').split('\n').forEach((line) => {
    const m = _STEP_LINE.exec(line.trim());
    if (m && !byId[m[2]]) byId[m[2]] = { tag: m[1], action: m[3] || '', text: m[4] };
    else if (line.trim().startsWith('[')) notes.push(line.trim());
  });
  return { byId, notes };
}

// A hunt's line is its JSON result; say it as a count instead.
function _stepMessage(text) {
  const t = String(text || '').trim();
  if (t.startsWith('{')) {
    try {
      const body = JSON.parse(t);
      if (Array.isArray(body.matches)) {
        const n = body.matches.length;
        return `${n} match${n === 1 ? '' : 'es'}${body.truncated ? ' (cut off at the limit)' : ''}${body.timed_out ? ' — timed out' : ''}`;
      }
    } catch (e) { /* not JSON after all: show it as text */ }
  }
  return t.length > 240 ? t.slice(0, 240) + '…' : t;
}

function _stepResultsHtml(resultData, output) {
  // One row per step: what happened, in words, next to what it reported.
  // Everything here is agent-reported, so every value goes through escHtml.
  const steps = (resultData && Array.isArray(resultData.steps)) ? resultData.steps : [];
  if (!steps.length) return '';
  const { byId, notes } = _stepLines(output);
  const rows = steps.map((s) => {
    const id = String((s && s.id) || '');
    const line = byId[id] || {};
    let status = String((s && s.status) || '');
    if (line.tag === 'ERROR' || line.tag === 'FAILED') status = 'error';
    const probe = id.startsWith('relevant-');
    const pairs = Object.entries((s && s.result) || {})
      .map(([k, v]) => `<span class="run-step-kv"><span class="k">${escHtml(k)}</span> ${escHtml(String(v))}</span>`)
      .join('');
    const message = line.text ? _stepMessage(line.text) : '';
    const label = probe ? 'applies-when check' : (line.action || '');
    return `<div class="step-row s-${escHtml(status || 'none')}">
      <span class="step-row-status">${escHtml(status === 'ok' ? 'ok' : status || '—')}</span>
      <div class="step-row-body">
        <div class="step-row-head"><span class="mono">${escHtml(id)}</span>${label ? ` <span class="step-row-kind">${escHtml(label)}</span>` : ''}</div>
        ${message ? `<div class="step-row-msg">${escHtml(message)}</div>` : ''}
        ${pairs ? `<div class="step-row-kv">${pairs}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  const noteHtml = notes.filter(n => /^\[(NOT APPLICABLE|ERROR)\]/.test(n))
    .map(n => `<div class="step-row-note">${escHtml(n)}</div>`).join('');
  return `<div class="run-step-results">${rows}${noteHtml}</div>`;
}

function _runWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return d.toLocaleDateString();
}

function _runDuration(row) {
  if (!row.finished_at) return '';
  const ms = new Date(row.finished_at) - new Date(row.created_at);
  if (ms < 1000) return '<1s';
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  return `${Math.round(ms / 60000)}m`;
}

function _buildRunRow(row) {
  const el = document.createElement('div');
  el.className = 'run-row';
  const accent = _RUN_STATE_ACCENT[row.state] || 'lav';
  const kind = row.source === 'playbook' ? 'playbook' : 'automation';
  // name_snapshot is captured at dispatch, so history still reads correctly
  // after the automation or playbook it came from is deleted.
  const name = row.name_snapshot || row.automation_name || row.playbook_name || 'run';
  const hosts = `${row.host_count} host${row.host_count === 1 ? '' : 's'}`;
  const steps = `${row.step_count} step${row.step_count === 1 ? '' : 's'}`;
  const dur = _runDuration(row);

  el.innerHTML =
    `<span class="run-kind run-kind-${kind}">${escHtml(kind)}</span>` +
    `<span class="run-name">${escHtml(name)}</span>` +
    `<span class="run-meta">${escHtml(hosts)} · ${escHtml(steps)}${dur ? ' · ' + escHtml(dur) : ''}</span>` +
    `<span class="run-when">${escHtml(_runWhen(row.created_at))}</span>` +
    `<span class="run-state t-${accent}">${escHtml(row.state)}</span>`;
  el.addEventListener('click', () => _openRunDetail(row.id));
  return el;
}

async function _openRunDetail(runId) {
  let run;
  try { run = await apiJson(`/api/v1/tasks/runs/${runId}/`); }
  catch (e) { showToast('Could not load that run', 'error'); return; }

  const m = mountModal('run-detail', { wide: true });
  const tasks = run.tasks || [];
  const rows = tasks.length
    ? tasks.map((t) => `
        <div class="run-task" data-host="${escAttr(String(t.host))}">
          <span class="run-task-host">${escHtml(t.host_hostname || t.hostname || t.host || '')}</span>
          <span class="run-state t-${_RUN_STATE_ACCENT[t.state] || 'lav'}">${escHtml(t.state)}</span>
          ${_stepResultsHtml(t.result_data, t.result_output)
            ? `${_stepResultsHtml(t.result_data, t.result_output)}<details class="step-output-more"><summary>Full output</summary><pre class="run-task-out">${escHtml(t.result_output || '')}</pre></details>`
            : `<pre class="run-task-out">${escHtml(t.result_output || '')}</pre>`}
        </div>`).join('')
    : '<p class="muted">No task rows for this run.</p>';
  const huntBtn = _runHasHuntSteps(run)
    ? `<button class="btn btn-sky btn-sm" id="rd-hunt" type="button">Open hunt results</button>`
    : '';
  m.setBody(`
    <div class="modal-title"><span>${escHtml(run.name_snapshot || 'Run')}</span>
      <button class="modal-close" id="rd-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <p class="muted" style="margin-bottom:12px;">${escHtml(run.state)} · ${escHtml(String(run.host_count))} host(s) · ${escHtml(String(run.step_count))} step(s)</p>
    ${typeof runSummaryHtml === 'function' ? runSummaryHtml(run.summary) : ''}
    ${typeof flowRunHtml === 'function' ? flowRunHtml(run) : ''}
    <div class="run-task-list">${rows}</div>
    <div class="confirm-actions">${huntBtn}<button class="btn btn-outline btn-sm" id="rd-close">Close</button></div>`);
  const close = () => m.close();
  m.modal.querySelector('#rd-x').onclick = close;
  m.modal.querySelector('#rd-close').onclick = close;
  if (typeof wireRunSummary === 'function') wireRunSummary(m.modal, run.summary);
  // A playbook run draws its flow: give it the room the lanes need.
  if (run.flow_snapshot) m.modal.classList.add('modal-flow');
  const huntBtnEl = m.modal.querySelector('#rd-hunt');
  if (huntBtnEl) huntBtnEl.onclick = () => { m.close(); openHuntResults(run.id); };
  requestAnimationFrame(m.open);
}

function _renderRunPager() {
  const el = document.getElementById('run-history-pager');
  if (!el) return;
  const { page, pages } = runHistoryState;
  if (pages <= 1) { el.replaceChildren(); return; }
  el.innerHTML =
    `<button class="btn btn-outline btn-sm" ${page <= 1 ? 'disabled' : ''} id="rh-prev">Previous</button>` +
    `<span class="muted">Page ${page} of ${pages}</span>` +
    `<button class="btn btn-sky btn-sm" ${page >= pages ? 'disabled' : ''} id="rh-next">Next</button>`;
  el.querySelector('#rh-prev')?.addEventListener('click', () => loadRunHistory(page - 1));
  el.querySelector('#rh-next')?.addEventListener('click', () => loadRunHistory(page + 1));
}

async function loadRunHistory(page) {
  const list = document.getElementById('run-history-list');
  if (!list) return;
  const p = page || runHistoryState.page || 1;
  let body;
  try {
    body = await apiJson(
      `/api/v1/tasks/runs/?page=${p}&source=${encodeURIComponent(runHistoryState.source)}`);
  } catch (e) {
    // Silent on poll failure — keep the last good list on screen.
    return;
  }
  runHistoryState.page = body.page;
  runHistoryState.pages = body.pages;
  list.replaceChildren();
  if (!body.results.length) {
    list.appendChild(_buildEmptyState(
      'No runs yet',
      'Playbook and automation dispatches will appear here with their results.'));
  } else {
    for (const row of body.results) list.appendChild(_buildRunRow(row));
  }
  _renderRunPager();
}

document.addEventListener('DOMContentLoaded', () => {
  // Client-side filter over the page already fetched. Deliberately not a
  // server round-trip: history is paged, and re-querying on every keystroke
  // would fight the pager.
  document.getElementById('hist-search')?.addEventListener('input', (e) => {
    const q = (e.target.value || '').trim().toLowerCase();
    document.querySelectorAll('#hist-list .bl-card, #hist-list tr').forEach(row => {
      row.style.display = !q || (row.textContent || '').toLowerCase().includes(q) ? '' : 'none';
    });
  });

  document.getElementById('hist-filters')?.addEventListener('click', (e) => {
    const b = e.target.closest('.sa-chip');
    if (!b) return;
    document.querySelectorAll('#hist-filters .sa-chip')
      .forEach((c) => c.classList.toggle('on', c === b));
    runHistoryState.source = b.dataset.src;
    loadRunHistory(1);
  });

  document.querySelectorAll('#page-playbooks .sub-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.dataset.subtab === 'hist-panel') loadRunHistory(1);
    });
  });
});
