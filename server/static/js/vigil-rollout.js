// vigil-rollout.js
// Owns: staged rollout UI on the Tasks page (Rollouts tab) — the wave
//   progression view, start/halt/resume actions, and the TOTP confirmations.
// Depends on: vigil-utils.js (apiJson, showToast, navigateTo, el)
// API: /api/v1/rollouts/ (GET list, POST start),
//      /api/v1/rollouts/<id>/ (GET), <id>/halt/, <id>/resume/,
//      <id>/skip-validation/ (POST, TOTP).
// The server's beat task (tasks.advance_rollouts, every 5 min) advances the
// state machine; this page only reads and sends operator actions.

const WAVE_STATUS_COLOR = {
  passed: 'var(--mint)',
  running: 'var(--sky)',
  validating: 'var(--sky)',
  halted: 'var(--rose)',
  pending: 'var(--text-3)',
};

const ROLLOUT_STATE_COLOR = {
  pending: 'var(--text-3)',
  running: 'var(--sky)',
  validating: 'var(--sky)',
  halted: 'var(--rose)',
  completed: 'var(--mint)',
  cancelled: 'var(--text-3)',
};

const _rolloutState = {
  items: [],
  pollTimer: null,
  pendingAction: null,   // { title, message, fn(totp) } for the confirm modal
};

function _waveDot(status) {
  const color = WAVE_STATUS_COLOR[status] || 'var(--text-3)';
  return `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color};box-shadow:0 0 6px ${status === 'pending' ? 'none' : color};"></span>`;
}

function _fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function _waveProgress(r) {
  return (r.waves || []).map(wave => {
    const color = WAVE_STATUS_COLOR[wave.status] || 'var(--text-3)';
    const count = wave.tasks_total
      ? `${wave.tasks_done + wave.tasks_failed}/${wave.tasks_total} reported`
      : (wave.hosts ? 'queued' : 'no hosts');
    const validation = wave.validation_hours ? ` · validation ${wave.validation_hours}h` : '';
    return `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--s2);">
      ${_waveDot(wave.status)}
      <span style="min-width:130px;font-weight:600;color:${color};">${escHtml(wave.name)}</span>
      <span style="color:var(--text-3);font-size:12px;">${wave.hosts} host${wave.hosts === 1 ? '' : 's'} · ${count}${validation}</span>
      <span style="margin-left:auto;color:var(--text-3);font-size:11px;">${(wave.tags || []).map(escHtml).join(', ')}</span>
    </div>`;
  }).join('');
}

function _rolloutCard(r) {
  const color = ROLLOUT_STATE_COLOR[r.state] || 'var(--text-3)';
  const active = r.state === 'running' || r.state === 'validating';
  let actions = '';
  if (active) {
    actions = `<button class="btn btn-ghost btn-sm" data-rlt="${r.id}" onclick="event.stopPropagation();promptRolloutAction(this,'halt')">Halt now</button>`;
  } else if (r.state === 'halted') {
    actions = `<button class="btn btn-sky btn-sm" data-rlt="${r.id}" onclick="event.stopPropagation();promptRolloutAction(this,'resume')">Resume</button>`;
  }
  const reason = r.halted_reason
    ? `<div style="margin:8px 0 2px;padding:8px 10px;border:1px solid var(--rose);border-radius:6px;color:var(--rose);font-size:12px;">
        <strong>Halted:</strong> ${escHtml(r.halted_reason)}
        ${r.halted_by_name ? `<span style="color:var(--text-3);">by ${escHtml(r.halted_by_name)}</span>` : ''}
       </div>`
    : '';
  return `<div class="def-card" style="padding:14px 16px;cursor:pointer;"
       onclick="openRolloutDetail('${r.id}')" role="button" tabindex="0"
       onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openRolloutDetail('${r.id}');}">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <strong>${escHtml(r.target_name || r.definition_name || r.baseline_name || '(deleted)')}</strong>${r.action_kind === 'baseline' ? ' <span class="chip">baseline</span>' : ''}
      <span style="color:${color};font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.04em;">${escHtml(r.state)}</span>
      ${r.current_wave_name ? `<span style="color:var(--text-3);font-size:12px;">wave: ${escHtml(r.current_wave_name)}</span>` : ''}
      <span style="color:var(--text-3);font-size:11px;margin-left:auto;">${escHtml(r.created_by_name || '')} · started ${_fmtTs(r.started_at)}</span>
    </div>
    ${reason}
    <div style="margin-top:10px;">${_waveProgress(r)}</div>
    <div style="display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap;">
      <span style="color:var(--text-3);font-size:11px;">
        gate: halt above ${r.failure_threshold_pct}% · min ${r.min_results_before_halt} results
        ${r.resumed_by_name ? ` · resumed by ${escHtml(r.resumed_by_name)}` : ''}
      </span>
      ${actions}
    </div>
  </div>`;
}

async function refreshRollouts() {
  const box = document.getElementById('rollout-list');
  if (!box) return;
  try {
    const items = await apiJson('/api/v1/rollouts/');
    _rolloutState.items = items;
    _renderRollouts();
  } catch (e) {
    box.innerHTML = `<div class="empty-state" style="padding:28px;"><div class="empty-state-title">Failed to load rollouts: ${escHtml(e.message)}</div></div>`;
  }
  _scheduleRolloutPolling();
}

// Search filters what is already loaded rather than refetching — the list is
// small and a round-trip per keystroke would fight the 5s poll.
function _filterRollouts() {
  const q = (document.getElementById('rollout-search')?.value || '').trim().toLowerCase();
  if (!q) return _rolloutState.items;
  return _rolloutState.items.filter(r =>
    (r.target_name || r.definition_name || r.baseline_name || '').toLowerCase().includes(q) ||
    (r.state || '').toLowerCase().includes(q) ||
    (r.current_wave_name || '').toLowerCase().includes(q) ||
    (r.waves || []).some(w => (w.name || '').toLowerCase().includes(q) ||
                              (w.tags || []).some(t => (t || '').toLowerCase().includes(q))));
}

function _renderRollouts() {
  const box = document.getElementById('rollout-list');
  if (!box) return;
  const items = _filterRollouts();
  {
    if (!items.length) {
      box.innerHTML = `<div class="empty-state" style="padding:28px;">
        <div class="empty-state-title">${_rolloutState.items.length ? 'No rollouts match that search' : 'No rollouts yet'}</div>
        <div style="color:var(--text-3);font-size:12px;margin-top:6px;">${_rolloutState.items.length ? 'Clear the search to see them all.' : 'Start one from a task definition — it fans out wave by wave and halts itself on failure.'}</div>
      </div>`;
    } else {
      box.innerHTML = items.map(_rolloutCard).join('');
    }
  }
}

function _rolloutsTabVisible() {
  // Rollouts moved from the Tasks page into Deployments (Baselines / Automation
  // / Rollouts / History) — this is a sub-tab now, not a tab.
  const tab = document.querySelector('.sub-tab[data-subtab="rollout-panel"]');
  return !!(tab && tab.classList.contains('active'));
}

function _anyRolloutActive() {
  return _rolloutState.items.some(r => r.state === 'running' || r.state === 'validating');
}

function _scheduleRolloutPolling() {
  // Poll while the tab is visible and a rollout is moving; the server advances
  // waves on its own 5-minute beat, so this only refreshes the read-out.
  if (_rolloutState.pollTimer) { clearInterval(_rolloutState.pollTimer); _rolloutState.pollTimer = null; }
  if (_rolloutsTabVisible() && _anyRolloutActive()) {
    _rolloutState.pollTimer = setInterval(refreshRollouts, 5000);
  }
}

/* ── Start modal ─────────────────────────────────────────────────────── */

async function openRolloutStart() {
  const modal = document.getElementById('rollout-start-modal');
  if (!modal) return;
  // Cleared on every open so a previous pick can't be submitted by accident.
  document.getElementById('rollout-start-def').value = '';
  document.getElementById('rollout-start-def-label').textContent = 'Choose a task or baseline…';
  document.getElementById('rollout-start-totp').value = '';
  document.getElementById('rollout-start-overlay').classList.add('open');
  modal.classList.add('open');
}

function pickRolloutTarget() {
  openPicker({
    type: 'rollout_target',
    title: 'Pick a task or baseline to roll out',
    allowAdd: false,
    onSelect: (item) => {
      document.getElementById('rollout-start-def').value = item.key;
      document.getElementById('rollout-start-def-label').textContent = item.name;
    },
  });
}

function closeRolloutStart() {
  document.getElementById('rollout-start-overlay').classList.remove('open');
  document.getElementById('rollout-start-modal').classList.remove('open');
}

async function submitRolloutStart() {
  const sel = document.getElementById('rollout-start-def');
  const totp = document.getElementById('rollout-start-totp').value.trim();
  const threshold = parseInt(document.getElementById('rollout-start-threshold').value, 10);
  const minResults = parseInt(document.getElementById('rollout-start-min').value, 10);
  const btn = document.getElementById('rollout-start-submit');
  if (!sel.value) { showToast('Pick something to roll out first', 'error'); return; }
  if (!/^\d{6}$/.test(totp)) { showToast('Enter the 6-digit TOTP code', 'error'); return; }
  btn.disabled = true;
  try {
    const r = await apiJson('/api/v1/rollouts/', {
      method: 'POST',
      // The option value is "task:<id>" or "baseline:<id>"; the API wants
      // exactly one of the two id fields and rejects both or neither.
      body: JSON.stringify({
        ...(sel.value.startsWith('baseline:')
          ? { baseline_id: sel.value.slice('baseline:'.length) }
          : { definition_id: sel.value.replace(/^task:/, '') }),
        failure_threshold_pct: threshold,
        min_results_before_halt: minResults,
        totp,
      }),
    });
    closeRolloutStart();
    showToast(`Rollout started — wave 1 dispatched (${r.waves?.[0]?.tasks_total ?? 0} host(s))`, 'success');
    refreshRollouts();
  } catch (e) {
    const msg = e.message || 'Request failed';
    if (/TOTP|two-factor/i.test(msg)) {
      showToast('TOTP code rejected — check your authenticator', 'error');
      document.getElementById('rollout-start-totp').value = '';
      document.getElementById('rollout-start-totp').focus();
    } else {
      showToast('Could not start rollout: ' + msg, 'error');
    }
  } finally {
    btn.disabled = false;
  }
}

/* ── Halt / resume (TOTP confirm modal) ──────────────────────────────── */

function promptRolloutAction(btn, kind) {
  const modal = document.getElementById('rollout-action-modal');
  const overlay = document.getElementById('rollout-action-overlay');
  if (!modal || !overlay) return;
  const rolloutId = btn ? btn.dataset.rlt : null;
  const r = _rolloutState.items.find(x => x.id === rolloutId);
  const name = r ? (r.target_name || r.definition_name || r.baseline_name) : 'the rollout';
  const title = kind === 'halt' ? 'Halt rollout'
    : kind === 'resume' ? 'Resume rollout'
    : 'Skip the validation window';
  const message = kind === 'halt'
    ? `Stop ${name} now? The current wave keeps running; nothing new is dispatched. An admin TOTP is required.`
    : kind === 'resume'
      ? `Restart ${name} at its current wave? Failed tasks on the wave are re-queued and the gate re-evaluates.`
      : `End this wave's validation window for ${name} and move to the next wave now? `
        + `The window exists so a slow failure has time to show up, so only do this if you have `
        + `checked the wave yourself. The failure gate still applies.`;
  _rolloutState.pendingAction = { title, message, rolloutId, kind };
  document.getElementById('rollout-action-title').textContent = title;
  document.getElementById('rollout-action-message').textContent = message;
  document.getElementById('rollout-action-totp').value = '';
  overlay.classList.add('open');
  modal.classList.add('open');
  setTimeout(() => document.getElementById('rollout-action-totp').focus(), 50);
}

function closeRolloutAction() {
  const modal = document.getElementById('rollout-action-modal');
  const overlay = document.getElementById('rollout-action-overlay');
  if (modal) modal.classList.remove('open');
  if (overlay) overlay.classList.remove('open');
  _rolloutState.pendingAction = null;
}

async function confirmRolloutAction() {
  const pending = _rolloutState.pendingAction;
  if (!pending) return;
  const totp = document.getElementById('rollout-action-totp').value.trim();
  const btn = document.getElementById('rollout-action-confirm');
  if (!/^\d{6}$/.test(totp)) { showToast('Enter the 6-digit TOTP code', 'error'); return; }
  btn.disabled = true;
  try {
    await apiJson(`/api/v1/rollouts/${pending.rolloutId}/${pending.kind}/`, {
      method: 'POST',
      body: JSON.stringify({ totp }),
    });
    closeRolloutAction();
    closeRolloutDetail();
    showToast(pending.kind === 'halt' ? 'Rollout halted'
      : pending.kind === 'resume' ? 'Rollout resumed'
      : 'Validation window skipped — moving to the next wave', 'success');
    refreshRollouts();
  } catch (e) {
    const msg = e.message || 'Request failed';
    if (/TOTP|two-factor/i.test(msg)) {
      showToast('TOTP code rejected — check your authenticator', 'error');
      document.getElementById('rollout-action-totp').value = '';
      document.getElementById('rollout-action-totp').focus();
    } else if (/admin|permission/i.test(msg)) {
      showToast('Halting a rollout requires an admin account', 'error');
    } else {
      showToast('Action failed: ' + msg, 'error');
    }
  } finally {
    btn.disabled = false;
  }
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

// Refresh when the Rollouts sub-tab opens, and on navigation to Deployments.
// Both live on the baselines page now — the sidebar entry is labelled
// "Deployments" but its data-page is still `baselines`.
document.getElementById('rollout-search')?.addEventListener('input', _renderRollouts);
document.getElementById('rollout-start-def-btn')?.addEventListener('click', pickRolloutTarget);

document.querySelectorAll('.sub-tab[data-subtab]').forEach(tab => {
  tab.addEventListener('click', () => {
    if (tab.dataset.subtab === 'rollout-panel') refreshRollouts();
  });
});
const _origNavigateForRollouts = navigateTo;
navigateTo = function (pageName) {
  _origNavigateForRollouts(pageName);
  if (pageName === 'baselines' && _rolloutsTabVisible()) refreshRollouts();
};
document.addEventListener('DOMContentLoaded', () => {
  if (_rolloutsTabVisible()) refreshRollouts();
});


// Escape closes the modal, matching the deploy modal and host detail. Without
// it the overlay stays up and swallows every click on the page behind it.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('rollout-start-modal')?.classList.contains('open')) {
    closeRolloutStart();
  }
});


/* ── Detail ───────────────────────────────────────────────────────────── */

function _detailRow(label, value) {
  if (value === null || value === undefined || value === '') return '';
  return `<div style="display:flex;gap:12px;padding:5px 0;border-bottom:1px solid var(--s2);">
    <span style="min-width:170px;color:var(--text-3);font-size:12px;">${escHtml(label)}</span>
    <span style="font-size:12px;">${escHtml(String(value))}</span>
  </div>`;
}

async function openRolloutDetail(rolloutId) {
  const r = _rolloutState.items.find(x => String(x.id) === String(rolloutId));
  if (!r) return;
  const modal = document.getElementById('rollout-detail-modal');
  if (!modal) return;

  document.getElementById('rollout-detail-title').textContent =
    r.target_name || r.definition_name || r.baseline_name || 'Rollout';

  const gate = `halts above ${r.failure_threshold_pct}% failures, once at least ` +
               `${r.min_results_before_halt} host${r.min_results_before_halt === 1 ? '' : 's'} have reported`;

  document.getElementById('rollout-detail-body').innerHTML = `
    ${r.halted_reason ? `<div style="margin-bottom:10px;padding:9px 11px;border:1px solid var(--rose);border-radius:6px;color:var(--rose);font-size:12px;">
        <strong>Halted:</strong> ${escHtml(r.halted_reason)}</div>` : ''}
    <div style="margin-bottom:12px;">${_waveProgress(r)}</div>
    ${_detailRow('What is rolling out', (r.target_name || '') + (r.action_kind === 'baseline' ? ' (baseline)' : ' (task)'))}
    ${_detailRow('State', r.state)}
    ${_detailRow('Current wave', r.current_wave_name)}
    ${_detailRow('Failure gate', gate)}
    ${_detailRow('Started', _fmtTs(r.started_at))}
    ${_detailRow('This wave started', _fmtTs(r.wave_started_at))}
    ${_detailRow('Finished', _fmtTs(r.finished_at))}
    ${_detailRow('Started by', r.created_by_name)}
    ${_detailRow('Halted by', r.halted_by_name)}
    ${_detailRow('Resumed by', r.resumed_by_name)}`;

  // Which actions apply depends entirely on state, so build them per open
  // rather than showing greyed buttons that cannot do anything.
  const acts = [];
  if (r.state === 'validating') {
    acts.push(`<button class="btn btn-mint btn-sm" data-rlt="${r.id}"
      onclick="promptRolloutAction(this,'skip-validation')">Skip validation, continue now</button>`);
  }
  if (r.state === 'running' || r.state === 'validating') {
    acts.push(`<button class="btn btn-ghost btn-sm" style="color:var(--rose);" data-rlt="${r.id}"
      onclick="promptRolloutAction(this,'halt')">Halt now</button>`);
  }
  if (r.state === 'halted') {
    acts.push(`<button class="btn btn-sky btn-sm" data-rlt="${r.id}"
      onclick="promptRolloutAction(this,'resume')">Resume from this wave</button>`);
  }
  const hint = r.state === 'validating'
    ? 'Skipping ends this wave\'s validation window now. The failure gate still applies — a wave that failed will not advance.'
    : r.state === 'halted'
      ? 'Resuming re-queues the failed hosts on this wave and re-checks the gate.'
      : r.state === 'running'
        ? 'This wave is still reporting. Halting stops the rollout where it is.'
        : 'This rollout has finished — nothing left to do.';
  document.getElementById('rollout-detail-actions').innerHTML =
    `<div class="confirm-hint" style="flex:1;margin:0;">${escHtml(hint)}</div>` + acts.join(' ');

  document.getElementById('rollout-detail-overlay').classList.add('open');
  modal.classList.add('open');
}

function closeRolloutDetail() {
  document.getElementById('rollout-detail-overlay')?.classList.remove('open');
  document.getElementById('rollout-detail-modal')?.classList.remove('open');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' &&
      document.getElementById('rollout-detail-modal')?.classList.contains('open')) {
    closeRolloutDetail();
  }
});
