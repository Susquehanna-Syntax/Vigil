// vigil-runhistory.js
// Owns: Baselines → History sub-tab. Lists TaskRuns produced by baseline and
// automation dispatch, newest first, with the outcome the run settled into.
//
// Before runs were attached to these dispatches, an execution left only loose
// Task rows identified by a step_label string, so there was nothing to show.

const runHistoryState = { page: 1, pages: 1, source: 'automation,baseline' };

const _RUN_STATE_ACCENT = {
  running: 'peach',
  completed: 'mint',
  partial: 'lemon',
  failed: 'rose',
};

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
  const kind = row.source === 'baseline' ? 'baseline' : 'automation';
  // name_snapshot is captured at dispatch, so history still reads correctly
  // after the automation or baseline it came from is deleted.
  const name = row.name_snapshot || row.automation_name || row.baseline_name || 'run';
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
        <div class="run-task">
          <span class="run-task-host">${escHtml(t.hostname || t.host || '')}</span>
          <span class="run-state t-${_RUN_STATE_ACCENT[t.state] || 'lav'}">${escHtml(t.state)}</span>
          <pre class="run-task-out">${escHtml(t.result_output || '')}</pre>
        </div>`).join('')
    : '<p class="muted">No task rows for this run.</p>';

  m.setBody(`
    <div class="modal-title"><span>${escHtml(run.name_snapshot || 'Run')}</span>
      <button class="modal-close" id="rd-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <p class="muted" style="margin-bottom:12px;">${escHtml(run.state)} · ${escHtml(String(run.host_count))} host(s) · ${escHtml(String(run.step_count))} step(s)</p>
    <div class="run-task-list">${rows}</div>
    <div class="confirm-actions"><button class="btn btn-outline btn-sm" id="rd-close">Close</button></div>`);
  const close = () => m.close();
  m.modal.querySelector('#rd-x').onclick = close;
  m.modal.querySelector('#rd-close').onclick = close;
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
    `<button class="btn btn-outline btn-sm" ${page >= pages ? 'disabled' : ''} id="rh-next">Next</button>`;
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
      'Baseline and automation dispatches will appear here with their results.'));
  } else {
    for (const row of body.results) list.appendChild(_buildRunRow(row));
  }
  _renderRunPager();
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('hist-filters')?.addEventListener('click', (e) => {
    const b = e.target.closest('.sa-chip');
    if (!b) return;
    document.querySelectorAll('#hist-filters .sa-chip')
      .forEach((c) => c.classList.toggle('on', c === b));
    runHistoryState.source = b.dataset.src;
    loadRunHistory(1);
  });

  document.querySelectorAll('#page-baselines .sub-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.dataset.subtab === 'hist-panel') loadRunHistory(1);
    });
  });
});
