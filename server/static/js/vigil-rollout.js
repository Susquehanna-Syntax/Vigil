// @ts-check
// vigil-rollout.js
// Owns: staged rollout UI on the Tasks page (Rollouts tab) — the wave
//   progression view, start/halt/resume actions, and the TOTP confirmations.
// Depends on: vigil-utils.js (apiJson, showToast, navigateTo, el)
// API: /api/v1/rollouts/ (GET list, POST start),
//      /api/v1/rollouts/<id>/ (GET), <id>/halt/, <id>/resume/,
//      <id>/skip-validation/ (POST, TOTP), /api/v1/rollouts/batch/<batch>/halt/
//      (POST, TOTP), /api/v1/wave-groups/ (GET).
// The server's beat task (tasks.advance_rollouts, every 5 min) advances the
// state machine; this page only reads and sends operator actions.
// The look lives in .rlt-* classes in static/css/vigil/12-playbooks-apps-stacks.css.

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
  groups: [],            // picked wave-group tags, in pick order — order decides where a host on two ladders goes
  groupChoices: [],      // the enabled groups GET /api/v1/wave-groups/ offered on open
};

const RLT_ACTIVE = ['pending', 'running', 'validating'];

function _waveDot(status) {
  const color = WAVE_STATUS_COLOR[status] || 'var(--text-3)';
  return `<span class="rlt-wave-dot" data-css="background:${color};box-shadow:0 0 6px ${status === 'pending' ? 'none' : color};"></span>`;
}

function _fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/* The wave rows: six cells always, so the columns line up with the next card.
   An empty slot renders an empty <span>; `''` collapsed to nothing and
   everything after the gap shifted. */
function _waveProgress(r) {
  return (r.waves || []).map(wave => {
    const color = WAVE_STATUS_COLOR[wave.status] || 'var(--text-3)';
    const count = wave.tasks_total
      ? t('{done}/{total} reported', { done: wave.tasks_done + wave.tasks_failed, total: wave.tasks_total })
      : (wave.hosts ? t('queued') : t('no hosts'));
    const validation = wave.validation_hours
      ? ' · ' + t('validation {hours}h', { hours: wave.validation_hours }) : '';
    // A wave that dispatched anything is worth opening: the counts say how many
    // failed, never which machines or why.
    const openable = wave.tasks_total > 0;
    const failed = wave.tasks_failed
      ? `<span class="chip rlt-chip-failed">${escHtml(t('{n} failed', { n: wave.tasks_failed }))}</span>`
      : '<span></span>';
    return `<div class="rlt-wave">
      ${_waveDot(wave.status)}
      <span class="rlt-wave-name" data-css="color:${color};">${escHtml(wave.name)}</span>
      <span class="rlt-wave-count">${escHtml(tn('{n} host', '{n} hosts', wave.hosts))} · ${escHtml(count)}${escHtml(validation)}</span>
      <span class="rlt-wave-failed">${failed}</span>
      <span class="rlt-wave-tags">${(wave.tags || []).map(escHtml).join(', ')}</span>
      <span class="rlt-wave-act">${openable ? `<button class="btn btn-sky btn-xs" data-wave-hosts data-rollout="${escAttr(r.id)}" data-wave="${escAttr(wave.id)}">${escHtml(t('Machines'))}</button>` : '<span></span>'}</span>
    </div>`;
  }).join('');
}

/* Every machine in one wave, and for a failure the output that explains it.
   Without this, acting on a halted rollout meant matching hosts up by hand in
   the run history. */
async function openWaveHosts(rolloutId, waveId) {
  const rollout = (_rolloutState.items || []).find(r => String(r.id) === String(rolloutId));
  const wave = ((rollout || {}).waves || []).find(w => String(w.id) === String(waveId));
  const waveName = wave ? wave.name : t('Wave');
  const m = mountModal('wave-hosts', { xwide: true });
  m.setBody(`<div class="modal-title"><span>${escHtml(t('Wave:'))} ${escHtml(waveName)}</span>
      <button class="modal-close" id="wh-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button></div>
    <div id="wh-body">${loadingHtml()}</div>`);
  /** @type {HTMLElement} */ (m.modal.querySelector('#wh-x')).onclick = m.close;
  m.open();

  let data;
  try {
    data = await apiJson(`/api/v1/rollouts/${rolloutId}/waves/${waveId}/hosts/`);
  } catch (e) {
    showLoadFailure(document.getElementById('wh-body'), t('this wave\'s hosts'), e,
      () => openWaveHosts(rolloutId, waveId));
    return;
  }

  const rows = (data.hosts || []).map((h) => {
    const stateColor = h.failed ? 'var(--rose)'
      : (h.state === 'completed' ? 'var(--mint)' : 'var(--text-3)');
    const output = (h.output || '').trim();
    return `<tr>
      <td><strong>${escHtml(h.hostname)}</strong>
        <div class="muted-note">${escHtml(h.ip_address || '')}</div></td>
      <td class="rlt-host-state" data-css="color:${stateColor};">${escHtml(h.state)}</td>
      <td>${h.completed_at ? escHtml(new Date(h.completed_at).toLocaleString()) : '—' /* i18n-ignore */}</td>
      <td>${output
        ? `<pre class="rlt-host-output">${escHtml(output)}</pre>`
        : `<span class="muted-note">${escHtml(t('no output'))}</span>`}</td>
    </tr>`;
  }).join('');

  document.getElementById('wh-body').innerHTML = `
    <div class="muted-note rlt-summary-below">
      ${escHtml(tn('{n} machine', '{n} machines', data.total))} ·
      <span class="rlt-failed-count" data-css="color:${data.failed ? 'var(--rose)' : 'var(--mint)'};">${escHtml(t('{n} failed', { n: data.failed }))}</span>
    </div>
    <div class="rlt-hosts-scroll">
      <table class="vuln-table">
        <thead><tr><th>${escHtml(t('Machine'))}</th><th>${escHtml(t('State'))}</th><th>${escHtml(t('Finished'))}</th><th>${escHtml(t('Output'))}</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4" class="muted-note">${escHtml(t('Nothing dispatched in this wave.'))}</td></tr>`}</tbody>
      </table>
    </div>`;
}

function _rolloutCard(r) {
  const color = ROLLOUT_STATE_COLOR[r.state] || 'var(--text-3)';
  const active = r.state === 'running' || r.state === 'validating';
  let actions = '';
  if (active) {
    actions = `<button class="btn btn-ghost btn-sm" data-rlt="${escAttr(r.id)}" data-rlt-act="halt" data-stop>${escHtml(t('Halt now'))}</button>`;
  } else if (r.state === 'halted') {
    actions = `<button class="btn btn-sky btn-sm" data-rlt="${escAttr(r.id)}" data-rlt-act="resume" data-stop>${escHtml(t('Resume'))}</button>`;
  }
  const reason = r.halted_reason
    ? `<div class="rlt-halted">
        <strong>${escHtml(t('Halted:'))}</strong> ${escHtml(r.halted_reason)}
        ${r.halted_by_name ? `<span class="rlt-halted-by">${escHtml(t('by {name}', { name: r.halted_by_name }))}</span>` : ''}
       </div>`
    : '';
  return `<div class="def-card rlt-card"
       data-rlt-open="${escAttr(r.id)}" role="button" tabindex="0">
    <div class="rlt-body">
      <div class="rlt-head">
        <strong>${escHtml(r.target_name || r.definition_name || r.playbook_name || '(deleted)')}</strong>${r.action_kind === 'playbook' ? ` <span class="chip">${escHtml(t('playbook'))}</span>` : ''}
        <span class="rlt-state" data-css="color:${color};">${escHtml(r.state)}</span>
        ${r.current_wave_name ? `<span class="rlt-wave-count">${escHtml(t('wave:'))} ${escHtml(r.current_wave_name)}</span>` : ''}
        <span class="rlt-head-meta">${escHtml(r.created_by_name || '')} · ${escHtml(t('started'))} ${_fmtTs(r.started_at)}</span>
      </div>
      ${reason}
      <div class="rlt-waves">${_waveProgress(r)}</div>
      <div class="rlt-gate">
        <span class="rlt-gate-text">
          ${escHtml(t('gate: halt above {pct}% · min {min} results', { pct: r.failure_threshold_pct, min: r.min_results_before_halt }))}
          ${r.resumed_by_name ? ` · ${escHtml(t('resumed by {name}', { name: r.resumed_by_name }))}` : ''}
        </span>
        ${actions}
      </div>
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
    showLoadFailure(box, 'rollouts', e, () => refreshRollouts());
  }
  _scheduleRolloutPolling();
}

// Search filters what is already loaded rather than refetching — the list is
// small and a round-trip per keystroke would fight the 5s poll.
function _filterRollouts() {
  const q = (/** @type {HTMLInputElement} */ (document.getElementById('rollout-search'))?.value || '').trim().toLowerCase();
  if (!q) return _rolloutState.items;
  return _rolloutState.items.filter(r =>
    (r.target_name || r.definition_name || r.playbook_name || '').toLowerCase().includes(q) ||
    (r.state || '').toLowerCase().includes(q) ||
    (r.current_wave_name || '').toLowerCase().includes(q) ||
    (r.waves || []).some(w => (w.name || '').toLowerCase().includes(q) ||
                              (w.tags || []).some(t => (t || '').toLowerCase().includes(q))));
}

// Rollouts started together share a batch id, so they are one entry in the
// list — a card with a lane per group, `1 2 | 1 2 | 1 2 3`. Members are taken
// from the unfiltered list: a search that matches one lane shows the whole
// batch, because one lane alone would hide what that group ran beside.
function _groupRollouts(items) {
  const members = new Map();
  for (const r of _rolloutState.items) {
    if (!r.batch) continue;
    if (!members.has(r.batch)) members.set(r.batch, []);
    members.get(r.batch).push(r);
  }
  const entries = new Map();
  for (const r of items) {
    if (!r.batch) { entries.set(r.id, [r]); continue; }
    if (!entries.has(r.batch)) entries.set(r.batch, members.get(r.batch));
  }
  return [...entries.values()];
}

// One card, a column per group. The card itself is not openable — a batch has
// N rollouts to open, so each lane head carries its own data-rlt-open.
function _batchCard(rs) {
  const batch = rs[0].batch;
  const anyActive = rs.some(r => RLT_ACTIVE.includes(r.state));
  const overall = anyActive
    ? (rs.find(r => r.state === 'running' || r.state === 'validating') || rs[0]).state
    : (rs.some(r => r.state === 'halted') ? 'halted'
      : (rs.every(r => r.state === 'completed') ? 'completed' : rs[0].state));
  // Same skeleton as _rolloutCard (def-card > rlt-body > head, then content):
  // def-card is a flex row, so anything outside rlt-body sits beside it.
  return `<div class="def-card rlt-card rlt-batch">
    <div class="rlt-body">
      <div class="rlt-head">
        <strong>${escHtml(rs[0].target_name || rs[0].definition_name || rs[0].playbook_name || '(deleted)')}</strong>${rs[0].action_kind === 'playbook' ? ` <span class="chip">${escHtml(t('playbook'))}</span>` : ''}
        <span class="chip chip-muted">${escHtml(tn('parallel · {n} group', 'parallel · {n} groups', rs.length))}</span>
        <span class="rlt-state" data-css="color:${ROLLOUT_STATE_COLOR[overall] || 'var(--text-3)'};">${escHtml(overall)}</span>
        <span class="rlt-head-meta">${escHtml(rs[0].created_by_name || '')} · ${escHtml(t('started'))} ${_fmtTs(rs[0].started_at)}</span>
        ${anyActive ? `<button class="btn btn-ghost btn-xs" data-rlt-batch="${escAttr(batch)}" data-rlt-act="halt-batch" data-stop title="${escAttr(t('Stop every group now'))}">${escHtml(t('Halt all'))}</button>` : ''}
      </div>
      <div class="rlt-lanes">${rs.map(_batchLane).join('')}</div>
    </div>
  </div>`;
}

function _batchLane(r) {
  const actions = r.state === 'halted'
    ? `<button class="btn btn-ghost btn-xs" data-rlt="${escAttr(r.id)}" data-rlt-act="resume" data-stop title="${escAttr(t('Clear the halt and continue from this wave'))}">${escHtml(t('Resume'))}</button>`
    : (RLT_ACTIVE.includes(r.state)
      ? `<button class="btn btn-ghost btn-xs" data-rlt="${escAttr(r.id)}" data-rlt-act="halt" data-stop title="${escAttr(t('Stop this group now'))}">${escHtml(t('Halt'))}</button>`
      : '');
  return `<div class="rlt-lane">
    <div class="rlt-lane-head" data-rlt-open="${escAttr(r.id)}" role="button" tabindex="0">
      <strong>${escHtml(r.wave_group_tag || t('Ungrouped'))}</strong>
      <span class="rlt-state" data-css="color:${ROLLOUT_STATE_COLOR[r.state] || 'var(--text-3)'};">${escHtml(r.state)}</span>
      ${r.current_wave_name ? `<span class="rlt-wave-count">${escHtml(r.current_wave_name)}</span>` : ''}
      ${actions}
    </div>
    <div class="rlt-lane-body">${_waveProgress(r)}</div>
  </div>`;
}

function _renderRollouts() {
  const box = document.getElementById('rollout-list');
  if (!box) return;
  const items = _filterRollouts();
  if (!items.length) {
    box.innerHTML = `<div class="empty-state rlt-empty-compact">
      <div class="empty-state-title">${_rolloutState.items.length ? escHtml(t('No rollouts match that search')) : escHtml(t('No rollouts yet'))}</div>
      <div class="rlt-empty-note">${_rolloutState.items.length ? escHtml(t('Clear the search to see them all.')) : escHtml(t('Start one from a task definition — it fans out wave by wave and halts itself on failure.'))}</div>
    </div>`;
  } else {
    box.innerHTML = _groupRollouts(items)
      .map(group => (group.length > 1 ? _batchCard(group) : _rolloutCard(group[0])))
      .join('');
  }
  _bindWaveHostButtons(box);
}

/* Delegated so it survives every re-render of the list, which happens on a
   5-second poll while the tab is visible. */
function _bindWaveHostButtons(/** @type {HTMLElement} */ box) {
  if (box.dataset.waveHostsBound) return;
  box.dataset.waveHostsBound = '1';
  box.addEventListener('click', (ev) => {
    const btn = /** @type {HTMLButtonElement|null} */ (/** @type {HTMLElement} */ (ev.target).closest('[data-wave-hosts]'));
    if (!btn || !box.contains(btn)) return;
    ev.stopPropagation();
    openWaveHosts(btn.dataset.rollout, btn.dataset.wave);
  });
}

function _rolloutsTabVisible() {
  // Rollouts moved from the Tasks page into Deployments (Playbooks / Automation
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
  if (_rolloutState.pollTimer) { clearPollingInterval(_rolloutState.pollTimer); _rolloutState.pollTimer = null; }
  if (_rolloutsTabVisible() && _anyRolloutActive()) {
    _rolloutState.pollTimer = pollingInterval(refreshRollouts, 5000);
  }
}

/* ── Start modal ─────────────────────────────────────────────────────── */

async function openRolloutStart() {
  const modal = document.getElementById('rollout-start-modal');
  if (!modal) return;
  // Cleared on every open so a previous pick can't be submitted by accident.
  /** @type {HTMLSelectElement} */ (document.getElementById('rollout-start-def')).value = '';
  document.getElementById('rollout-start-def-label').textContent = t('Choose a task or playbook…');
  // The chips need no reset beyond clearing the pick: they are re-fetched and
  // repainted on every open.
  _rolloutState.groups = [];
  await _renderRolloutGroupChips();
  _refreshRolloutGroupHint();
  document.getElementById('rollout-start-overlay').classList.add('open');
  modal.classList.add('open');
}

/* The ladders to pick from, fetched on open so a wave added in the waves tab
   shows up without a reload. A group with no enabled wave cannot start a
   rollout, so it isn't offered. */
async function _renderRolloutGroupChips() {
  const box = document.getElementById('rollout-start-groups');
  if (!box) return;
  _rolloutState.groupChoices = [];
  box.innerHTML = loadingHtml(t('Loading wave groups…'));
  try {
    const data = await apiJson('/api/v1/wave-groups/');
    _rolloutState.groupChoices = (data.groups || []).filter(g => g.enabled_waves > 0);
    _paintRolloutGroupChips();
  } catch (e) {
    showLoadFailure(box, t('wave groups'), e, _renderRolloutGroupChips);
  }
}

function _paintRolloutGroupChips() {
  const box = document.getElementById('rollout-start-groups');
  if (!box) return;
  const groups = _rolloutState.groupChoices || [];
  if (!groups.length) {
    box.innerHTML = `<span class="muted-note">${escHtml(t('No wave groups defined — every enabled wave is one ladder.'))}</span>`;
    return;
  }
  box.innerHTML = groups.map(g => {
    const at = _rolloutState.groups.indexOf(g.tag);
    const waves = tn('{n} wave', '{n} waves', g.enabled_waves);
    return `<button type="button" class="chip rlt-group-chip" data-rlt-group="${escAttr(g.tag)}" aria-pressed="${at >= 0}">
      ${escHtml(g.tag)}<span class="rlt-group-chip-count">${escHtml(waves)}</span>
      ${at >= 0 ? `<span class="rlt-group-chip-order">${at + 1}</span>` : ''}
    </button>`;
  }).join('');
}

// Pick order is the tie-break for a machine tagged into two of the picked
// groups, so a picked chip shows its position.
delegateClick('[data-rlt-group]', (el) => {
  const tag = el.dataset.rltGroup;
  const at = _rolloutState.groups.indexOf(tag);
  if (at >= 0) _rolloutState.groups.splice(at, 1);
  else _rolloutState.groups.push(tag);
  _paintRolloutGroupChips();
  _refreshRolloutGroupHint();
});

function pickRolloutTarget() {
  openPicker({
    type: 'rollout_target',
    title: t('Pick a task or playbook to roll out'),
    allowAdd: false,
    onSelect: (item) => {
      /** @type {HTMLSelectElement} */ (document.getElementById('rollout-start-def')).value = item.key;
      document.getElementById('rollout-start-def-label').textContent = item.name;
      _loadRolloutStartInputs();
    },
  });
}

/* What the picked target asks for, as a form. A playbook's inputs are its
   row's; a task's are the ones its spec declares. Empty (or a target that
   declares nothing) leaves the section hidden, and then the POST sends no
   `inputs` key at all. */
async function _loadRolloutStartInputs() {
  const box = document.getElementById('rollout-start-inputs');
  if (!box) return;
  const key = /** @type {HTMLSelectElement} */ (document.getElementById('rollout-start-def')).value;
  box.hidden = true;
  box.replaceChildren();
  if (!key) return;
  try {
    let inputs = [];
    if (key.startsWith('playbook:')) {
      const row = await apiJson(`/api/v1/playbooks/${key.slice('playbook:'.length)}/`);
      inputs = row.inputs || [];
    } else if (key.startsWith('task:')) {
      const def = await apiJson(`/api/v1/tasks/definitions/${key.slice('task:'.length)}/`);
      inputs = (def.parsed_spec && def.parsed_spec.inputs) || [];
    }
    if (!inputs.length) return;
    inputsFormMount(box, inputs);
    box.hidden = false;
  } catch (e) {
    // The form is a convenience here; starting the rollout without it is the
    // server's call to make, so say what failed and let the pick stand.
    showToast(t("Could not load this target's inputs: {msg}", { msg: e.message || t('Request failed') }), 'error');
  }
}

function closeRolloutStart() {
  document.getElementById('rollout-start-overlay').classList.remove('open');
  document.getElementById('rollout-start-modal').classList.remove('open');
}

async function submitRolloutStart() {
  const sel = /** @type {HTMLSelectElement} */ (document.getElementById('rollout-start-def'));
  const threshold = parseInt(/** @type {HTMLInputElement} */ (document.getElementById('rollout-start-threshold')).value, 10);
  const minResults = parseInt(/** @type {HTMLInputElement} */ (document.getElementById('rollout-start-min')).value, 10);
  const btn = /** @type {HTMLButtonElement} */ (document.getElementById('rollout-start-submit'));
  if (!sel.value) { showToast(t('Pick something to roll out first'), 'error'); return; }
  const inputBox = document.getElementById('rollout-start-inputs');
  const withInputs = !!(inputBox && !inputBox.hidden);
  let values = null;
  if (withInputs) {
    const read = inputsFormRead(inputBox);
    if (read.error) { showToast(read.error, 'error'); return; }
    values = read.values;
  }
  const picked = _rolloutState.groups;
  const totp = await totpPrompt(t('Wave 1 starts as soon as you confirm.'),
    { title: t('Start rollout'), confirmText: t('Start rollout') });
  if (!totp) return;
  btn.disabled = true;
  try {
    const r = await apiJson('/api/v1/rollouts/', {
      method: 'POST',
      // The option value is "task:<id>" or "playbook:<id>"; the API wants
      // exactly one of the two id fields and rejects both or neither.
      // Likewise exactly one of wave_group_tag / wave_group_tags: one group is
      // an ordinary rollout, two or more are a parallel batch.
      body: JSON.stringify({
        ...(sel.value.startsWith('playbook:')
          ? { playbook_id: sel.value.slice('playbook:'.length) }
          : { definition_id: sel.value.replace(/^task:/, '') }),
        failure_threshold_pct: threshold,
        min_results_before_halt: minResults,
        ...(withInputs ? { inputs: values } : {}),
        ...(picked.length === 1 ? { wave_group_tag: picked[0] }
          : picked.length > 1 ? { wave_group_tags: picked.slice() }
          : {}),
        totp,
      }),
    });
    closeRolloutStart();
    if (r && r.batch) {
      showToast(t('Started {n} rollouts in parallel', { n: r.rollouts.length }), 'success');
    } else {
      showToast(t('Rollout started — wave 1 dispatched ({n} host(s))', { n: r.waves?.[0]?.tasks_total ?? 0 }), 'success');
    }
    refreshRollouts();
  } catch (e) {
    const msg = e.message || t('Request failed');
    if (/TOTP|two-factor/i.test(msg)) {
      showToast(t('TOTP code rejected — check your authenticator'), 'error');
    } else {
      showToast(t('Could not start rollout: {msg}', { msg }), 'error');
    }
  } finally {
    btn.disabled = false;
  }
}

/* ── Halt / resume (confirmed with TOTP) ─────────────────────────────── */

async function promptRolloutAction(/** @type {HTMLElement|null} */ btn, /** @type {string} */ kind) {
  const rolloutId = btn ? btn.dataset.rlt : null;
  const batch = btn ? btn.dataset.rltBatch : null;
  const card = btn ? btn.closest('.rlt-batch') : null;
  const lane = btn ? btn.closest('.rlt-lane') : null;
  const r = _rolloutState.items.find(x => x.id === rolloutId);
  // Inside a lane the group's own name reads better than the shared target.
  const name = lane && lane.querySelector('strong')
    ? lane.querySelector('strong').textContent
    : (r ? (r.target_name || r.definition_name || r.playbook_name) : t('the rollout'));
  const lanes = card ? card.querySelectorAll('.rlt-lane').length : 0;
  const batchName = (card && card.querySelector('strong')?.textContent) || t('the rollout');
  const title = kind === 'halt' ? t('Halt rollout')
    : kind === 'resume' ? t('Resume rollout')
    : kind === 'halt-batch' ? t('Halt every group')
    : t('Skip the validation window');
  const message = kind === 'halt'
    ? t('Stop {name} now? The current wave keeps running; nothing new is dispatched.', { name })
    : kind === 'resume'
      ? t('Restart {name} at its current wave? Failed tasks on the wave are re-queued and the gate re-evaluates.', { name })
      : kind === 'halt-batch'
        ? t('Stop all {lanes} groups of {batch} now? Every group keeps its current wave; nothing new is dispatched.', { lanes, batch: batchName })
        : t("End this wave's validation window for {name} and move to the next wave now? The window exists so a slow failure has time to show up, so only do this if you have checked the wave yourself. The failure gate still applies.", { name });
  const totp = await totpPrompt(message, { title, confirmText: title });
  if (!totp) return;
  try {
    let note;
    if (kind === 'halt-batch') {
      // One request, one code: TOTP codes are single-use, so halting a batch
      // group by group would need a fresh code per group.
      const res = await apiJson(`/api/v1/rollouts/batch/${encodeURIComponent(batch)}/halt/`, {
        method: 'POST',
        body: JSON.stringify({ totp }),
      });
      note = t('Halted {n} rollouts', { n: res.halted });
    } else {
      await apiJson(`/api/v1/rollouts/${rolloutId}/${kind}/`, {
        method: 'POST',
        body: JSON.stringify({ totp }),
      });
      note = kind === 'halt' ? t('Rollout halted')
        : kind === 'resume' ? t('Rollout resumed')
        : t('Validation window skipped — moving to the next wave');
    }
    closeRolloutDetail();
    showToast(note, 'success');
    refreshRollouts();
  } catch (e) {
    const msg = e.message || t('Request failed');
    if (/TOTP|two-factor/i.test(msg)) {
      showToast(t('TOTP code rejected — check your authenticator'), 'error');
    } else if (/admin|permission/i.test(msg)) {
      showToast(t('Halting a rollout requires an admin account'), 'error');
    } else {
      showToast(t('Action failed: {msg}', { msg }), 'error');
    }
  }
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

// Refresh when the Rollouts sub-tab opens, and on navigation to Deployments.
// Both live on the playbooks page now — the sidebar entry is labelled
// "Deployments" but its data-page is still `playbooks`.
document.getElementById('rollout-search')?.addEventListener('input', _renderRollouts);
document.getElementById('rollout-start-def-btn')?.addEventListener('click', pickRolloutTarget);

document.querySelectorAll('.sub-tab[data-subtab]').forEach((/** @type {HTMLElement} */ tab) => {
  tab.addEventListener('click', () => {
    if (tab.dataset.subtab === 'rollout-panel') refreshRollouts();
  });
});
const _origNavigateForRollouts = navigateTo;
window.navigateTo = function (pageName) {
  _origNavigateForRollouts(pageName);
  if (pageName === 'playbooks' && _rolloutsTabVisible()) refreshRollouts();
};
document.addEventListener('DOMContentLoaded', () => {
  if (_rolloutsTabVisible()) refreshRollouts();
  // "when the dialog opens with a value" — the pick sets the hidden input, so
  // this is what loads the inputs form for a target already chosen.
  document.getElementById('rollout-start-def')
    ?.addEventListener('change', _loadRolloutStartInputs);
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
  return `<div class="rlt-detail-row">
    <span class="rlt-detail-label">${escHtml(label)}</span>
    <span class="rlt-detail-value">${escHtml(String(value))}</span>
  </div>`;
}

async function openRolloutDetail(rolloutId) {
  const r = _rolloutState.items.find(x => String(x.id) === String(rolloutId));
  if (!r) return;
  const modal = document.getElementById('rollout-detail-modal');
  if (!modal) return;

  document.getElementById('rollout-detail-title').textContent =
    r.target_name || r.definition_name || r.playbook_name || t('Rollout');

  const gate = t('halts above {pct}% failures, once at least {n} have reported', {
    pct: r.failure_threshold_pct,
    n: tn('{n} host', '{n} hosts', r.min_results_before_halt),
  });

  document.getElementById('rollout-detail-body').innerHTML = `
    ${r.halted_reason ? `<div class="rlt-detail-halted">
        <strong>${escHtml(t('Halted:'))}</strong> ${escHtml(r.halted_reason)}</div>` : ''}
    <div class="rlt-detail-waves">${_waveProgress(r)}</div>
    ${_detailRow(t('What is rolling out'), (r.target_name || '') + ` ${r.action_kind === 'playbook' ? t('(playbook)') : t('(task)')}`)}
    ${_detailRow(t('State'), r.state)}
    ${r.batch && r.wave_group_tag ? _detailRow(t('Wave group'), r.wave_group_tag) : ''}
    ${_detailRow(t('Current wave'), r.current_wave_name)}
    ${_detailRow(t('Failure gate'), gate)}
    ${_detailRow(t('Started'), _fmtTs(r.started_at))}
    ${_detailRow(t('This wave started'), _fmtTs(r.wave_started_at))}
    ${_detailRow(t('Finished'), _fmtTs(r.finished_at))}
    ${_detailRow(t('Started by'), r.created_by_name)}
    ${_detailRow(t('Halted by'), r.halted_by_name)}
    ${_detailRow(t('Resumed by'), r.resumed_by_name)}`;

  // Which actions apply depends entirely on state, so build them per open
  // rather than showing greyed buttons that cannot do anything.
  const acts = [];
  if (r.state === 'validating') {
    acts.push(`<button class="btn btn-mint btn-sm" data-rlt="${escAttr(r.id)}"
      data-rlt-act="skip-validation">${escHtml(t('Skip validation, continue now'))}</button>`);
  }
  if (r.state === 'running' || r.state === 'validating') {
    acts.push(`<button class="btn btn-ghost btn-sm rlt-act-halt" data-rlt="${escAttr(r.id)}"
      data-rlt-act="halt">${escHtml(t('Halt now'))}</button>`);
  }
  if (r.state === 'halted') {
    acts.push(`<button class="btn btn-sky btn-sm" data-rlt="${escAttr(r.id)}"
      data-rlt-act="resume">${escHtml(t('Resume from this wave'))}</button>`);
  }
  const hint = r.state === 'validating'
    ? t("Skipping ends this wave's validation window now. The failure gate still applies — a wave that failed will not advance.")
    : r.state === 'halted'
      ? t('Resuming re-queues the failed hosts on this wave and re-checks the gate.')
      : r.state === 'running'
        ? t('This wave is still reporting. Halting stops the rollout where it is.')
        : t('This rollout has finished — nothing left to do.');
  document.getElementById('rollout-detail-actions').innerHTML =
    `<div class="confirm-hint rlt-detail-hint">${escHtml(hint)}</div>` + acts.join(' ');

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


/* The picker is the one place the blast radius is chosen, so it says what the
   pick covers before you start it — and warns that a machine on two of the
   picked ladders goes with the first group you picked. */
async function _refreshRolloutGroupHint() {
  const hint = document.getElementById('rollout-start-group-hint');
  if (!hint) return;
  const picked = _rolloutState.groups;
  if (!picked.length) {
    hint.textContent = t('Every enabled wave, in order.');
    return;
  }
  if (picked.length > 1) {
    hint.textContent = t('{n} groups in parallel — each walks its own waves; nothing waits for another group. A machine on more than one picked group goes with the first one you picked.', { n: picked.length });
    return;
  }
  try {
    const waves = await apiJson(`/api/v1/waves/?group=${encodeURIComponent(picked[0])}`);
    const live = waves.filter(w => w.enabled);
    if (!live.length) {
      hint.innerHTML = `<span class="bad">${t('No enabled wave is tagged {tag} — the rollout would be refused.', { tag: `<strong>${escHtml(picked[0])}</strong>` })}</span>`;
      return;
    }
    const machines = live.reduce((n, w) => n + (w.exclusive_host_count || 0), 0);
    hint.textContent = t('{waves} · {machines}, walked in order. Nothing outside this group is touched.', { waves: tn('{n} wave', '{n} waves', live.length), machines: tn('{n} machine', '{n} machines', machines) });
  } catch (e) {
    hint.textContent = t('Send this to one ladder only.');
  }
}


/* Rollout actions. promptRolloutAction takes the element itself (it reads
   data-rlt off it), so the delegated handler passes the same thing the inline
   attribute used to pass as `this`. data-stop marks the buttons that sit
   inside a clickable card and must not also open it. */
delegateClick('[data-rlt-act]', (el, ev) => {
  if (el.hasAttribute('data-stop')) ev.stopPropagation();
  promptRolloutAction(el, el.dataset.rltAct);
});
delegateClick('[data-rlt-open]', (el, ev) => {
  if (ev.target.closest('button')) return;
  openRolloutDetail(el.dataset.rltOpen);
});
/* The card is role="button" tabindex="0", so the keyboard has to open it too.
   An onkeydown attribute would do it but CSP forbids inline handlers, and a
   listener per card would not survive the 5-second re-render — so one
   delegated listener, keyed off the same attribute the click uses. */
document.addEventListener('keydown', (ev) => {
  const card = /** @type {HTMLElement|null} */ (/** @type {HTMLElement} */ (ev.target).closest && /** @type {HTMLElement} */ (ev.target).closest('[data-rlt-open]'));
  if (!card || (ev.key !== 'Enter' && ev.key !== ' ')) return;
  if (/** @type {HTMLElement} */ (ev.target).closest('button')) return;
  ev.preventDefault();
  openRolloutDetail(card.dataset.rltOpen);
});
