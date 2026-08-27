// vigil-rollout.js
// Owns: staged rollout UI on the Tasks page (Rollouts tab) — the wave
//   progression view, start/halt/resume actions, and the TOTP confirmations.
// Depends on: vigil-utils.js (apiJson, showToast, navigateTo, el)
// API: /api/v1/rollouts/ (GET list, POST start),
//      /api/v1/rollouts/<id>/ (GET), <id>/halt/, <id>/resume/ (POST, TOTP).
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
    actions = `<button class="btn btn-ghost btn-sm" data-rlt="${r.id}" onclick="promptRolloutAction(this,'halt')">Halt now</button>`;
  } else if (r.state === 'halted') {
    actions = `<button class="btn btn-sky btn-sm" data-rlt="${r.id}" onclick="promptRolloutAction(this,'resume')">Resume</button>`;
  }
  const reason = r.halted_reason
    ? `<div style="margin:8px 0 2px;padding:8px 10px;border:1px solid var(--rose);border-radius:6px;color:var(--rose);font-size:12px;">
        <strong>Halted:</strong> ${escHtml(r.halted_reason)}
        ${r.halted_by_name ? `<span style="color:var(--text-3);">by ${escHtml(r.halted_by_name)}</span>` : ''}
       </div>`
    : '';
  return `<div class="def-card" style="padding:14px 16px;">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <strong>${escHtml(r.definition_name)}</strong>
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
    if (!items.length) {
      box.innerHTML = `<div class="empty-state" style="padding:28px;">
        <div class="empty-state-title">No rollouts yet</div>
        <div style="color:var(--text-3);font-size:12px;margin-top:6px;">Start one from a task definition — it fans out wave by wave and halts itself on failure.</div>
      </div>`;
    } else {
      box.innerHTML = items.map(_rolloutCard).join('');
    }
  } catch (e) {
    box.innerHTML = `<div class="empty-state" style="padding:28px;"><div class="empty-state-title">Failed to load rollouts: ${escHtml(e.message)}</div></div>`;
  }
  _scheduleRolloutPolling();
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
  const sel = document.getElementById('rollout-start-def');
  const modal = document.getElementById('rollout-start-modal');
  if (!sel || !modal) return;
  sel.innerHTML = '<option>Loading…</option>';
  try {
    const defs = await apiJson('/api/v1/tasks/definitions/?scope=mine');
    if (!defs.length) {
      sel.innerHTML = '<option value="">No definitions in your library</option>';
      sel.disabled = true;
    } else {
      sel.innerHTML = defs.map(d =>
        `<option value="${d.id}">${escHtml(d.name)}${d.risk ? ` · ${escHtml(d.risk)}` : ''}</option>`
      ).join('');
      sel.disabled = false;
    }
  } catch (e) {
    sel.innerHTML = '<option value="">Failed to load</option>';
    sel.disabled = true;
    showToast('Failed to load definitions: ' + e.message, 'error');
  }
  document.getElementById('rollout-start-totp').value = '';
  document.getElementById('rollout-start-overlay').classList.add('open');
  modal.classList.add('open');
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
  if (!sel.value) { showToast('Pick a task definition first', 'error'); return; }
  if (!/^\d{6}$/.test(totp)) { showToast('Enter the 6-digit TOTP code', 'error'); return; }
  btn.disabled = true;
  try {
    const r = await apiJson('/api/v1/rollouts/', {
      method: 'POST',
      body: JSON.stringify({
        definition_id: sel.value,
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
  const name = r ? r.definition_name : 'the rollout';
  const title = kind === 'halt' ? 'Halt rollout' : 'Resume rollout';
  const message = kind === 'halt'
    ? `Stop ${name} now? The current wave keeps running; nothing new is dispatched. An admin TOTP is required.`
    : `Restart ${name} at its current wave? Failed tasks on the wave are re-queued and the gate re-evaluates.`;
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
    showToast(pending.kind === 'halt' ? 'Rollout halted' : 'Rollout resumed', 'success');
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
