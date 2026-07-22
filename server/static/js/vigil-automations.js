// vigil-automations.js
// Owns: the Automation sub-panel on the Baselines page. Create rules that run
// a task or baseline when an event fires (with severity/tag filters) or on a
// cron schedule. Backed by /api/v1/automations/.
// Depends on: vigil-utils.js (apiJson, confirmModal, showToast, escHtml).

let _autoEvents = {};      // event name -> label
let _autoDefs = [];        // task definitions
let _autoBaselines = [];   // baseline names
let _autoHosts = [];       // selectable hosts
let _autoRules = [];       // alert rules (for specific-event)

async function loadAutomations() {
  const list = document.getElementById('automations-list');
  if (!list) return;
  list.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    const [data, defs, baselines, hosts, rules] = await Promise.all([
      apiJson('/api/v1/automations/'),
      apiJson('/api/v1/tasks/definitions/'),
      apiJson('/api/v1/baselines/'),
      apiJson('/api/v1/status-pages/hosts/'),
      apiJson('/api/v1/alerts/rules/').catch(() => []),
    ]);
    _autoEvents = data.events || {};
    _autoDefs = Array.isArray(defs) ? defs : (defs.results || []);
    _autoBaselines = baselines;
    _autoHosts = hosts;
    _autoRules = rules;
    _fillEditorOptions();
    _renderAutomations(data.automations);
  } catch (e) {
    list.innerHTML = `<div class="empty-block"><h4>Couldn't load automations</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

function _renderAutomations(autos) {
  const list = document.getElementById('automations-list');
  if (!autos.length) {
    list.innerHTML = `<div class="empty-block">
      <h4>No automations yet</h4>
      <p>Run a task or baseline automatically — for example "when a critical alert fires, run the cleanup baseline on that host," or "every night at 2am, run backups on the backup hosts."</p></div>`;
    return;
  }
  list.innerHTML = autos.map(a => {
    const when = a.trigger === 'event'
      ? `<span class="automation-badge event">on event</span> when <b>${escHtml(_autoEvents[a.event] || a.event)}</b>${a.event_rule_name ? ` — <b>${escHtml(a.event_rule_name)}</b>` : ''}${a.min_severity ? ` (≥ ${escHtml(a.min_severity)})` : ''}${(a.event_tags || []).length ? ` on <span class="chip">${a.event_tags.map(escHtml).join('</span> <span class="chip">')}</span>` : ''}`
      : `<span class="automation-badge schedule">scheduled</span> cron <code class="inline">${escHtml(a.cron_display)}</code>`;
    const action = a.action_kind === 'baseline'
      ? `baseline <b>${escHtml(a.baseline_name)}</b>` : `task <b>${escHtml(a.task_name || '?')}</b>`;
    const target = { event_host: 'the event host', tags: 'hosts tagged ' + (a.target_tags || []).join(', '),
                     host: 'a specific host', all: 'all managed hosts' }[a.target] || a.target;
    const last = a.last_run ? new Date(a.last_run).toLocaleString() : 'never';
    return `<div class="bl-card">
      <div class="bl-card-head">
        <div>
          <span class="bl-name">${escHtml(a.name)}</span>
          <span class="bl-badge ${a.enabled ? 'on' : 'off'}">${a.enabled ? 'enabled' : 'off'}</span>
        </div>
        <div class="card-actions">
          <button class="btn btn-outline btn-xs" data-au-run="${a.id}">Run now</button>
          <button class="btn btn-outline btn-xs" data-au-toggle="${a.id}" data-en="${a.enabled}">${a.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-outline btn-xs" data-au-edit="${a.id}">Edit</button>
          <button class="btn btn-outline btn-xs" style="color:var(--rose);" data-au-del="${a.id}">Delete</button>
        </div>
      </div>
      <div class="muted-note" style="margin-bottom:8px;">${when} → run ${action} on ${escHtml(target)}.</div>
      <div class="bl-meta"><span>Ran <b>${a.run_count}</b> time${a.run_count === 1 ? '' : 's'}</span><span>Last run: ${escHtml(last)}</span></div>
    </div>`;
  }).join('');
  _wireAutoCards(autos);
}

function _wireAutoCards(autos) {
  const list = document.getElementById('automations-list');
  list.querySelectorAll('[data-au-del]').forEach(b => b.addEventListener('click', async () => {
    if (!(await confirmModal('Delete this automation?', { danger: true, confirmText: 'Delete' }))) return;
    await fetch(`/api/v1/automations/${b.dataset.auDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadAutomations();
  }));
  list.querySelectorAll('[data-au-toggle]').forEach(b => b.addEventListener('click', async () => {
    await apiJson(`/api/v1/automations/${b.dataset.auToggle}/`, { method: 'PATCH', body: JSON.stringify({ enabled: b.dataset.en !== 'true' }) });
    loadAutomations();
  }));
  list.querySelectorAll('[data-au-run]').forEach(b => b.addEventListener('click', async () => {
    try { const r = await apiJson(`/api/v1/automations/${b.dataset.auRun}/run/`, { method: 'POST', body: '{}' });
      showToast(`Dispatched to ${r.dispatched} host${r.dispatched === 1 ? '' : 's'}`, 'success'); loadAutomations();
    } catch (e) { showToast('Run failed: ' + e.message, 'error'); }
  }));
  list.querySelectorAll('[data-au-edit]').forEach(b => b.addEventListener('click', () =>
    _openAutoEditor(autos.find(a => a.id === b.dataset.auEdit))));
}

/* ── Editor ──────────────────────────────────────────────────────────── */
function _fillEditorOptions() {
  // Only the event trigger is still a native select; task/baseline/host are
  // chosen through the searchable picker modal.
  const ev = document.getElementById('auto-event');
  if (ev) ev.innerHTML = Object.entries(_autoEvents).map(([k, v]) => `<option value="${k}">${escHtml(v)}</option>`).join('');
  const rule = document.getElementById('auto-event-rule');
  if (rule) rule.innerHTML = '<option value="">any alert</option>' +
    _autoRules.map(r => `<option value="${escHtml(String(r.id))}">${escHtml(r.name)} (${escHtml(r.severity)})</option>`).join('');
}

// Open the picker for the current action kind and store the choice.
function _autoPickAction() {
  const kind = document.getElementById('auto-action-kind').value;
  if (kind === 'task') {
    openPicker({ type: 'task', title: 'Pick a task to run', onSelect: (item) => {
      if (item.raw && !_autoDefs.some(d => String(d.id) === String(item.key))) _autoDefs.push(item.raw);
      document.getElementById('auto-action-task').value = item.key;
      _autoRefreshActionLabel();
    } });
  } else {
    openPicker({ type: 'baseline', title: 'Pick a baseline to run', onSelect: (item) => {
      if (!_autoBaselines.some(b => b.name === item.key)) _autoBaselines.push(item.raw);
      document.getElementById('auto-action-baseline').value = item.key;
      _autoRefreshActionLabel();
    } });
  }
}

function _autoPickHost() {
  openPicker({ type: 'machine', title: 'Pick a host', allowAdd: false, onSelect: (item) => {
    if (!_autoHosts.some(h => String(h.id) === String(item.key))) _autoHosts.push(item.raw);
    document.getElementById('auto-target-host').value = item.key;
    _autoRefreshHostLabel();
  } });
}

function _autoSyncVisibility() {
  const trig = document.getElementById('auto-trigger').value;
  document.getElementById('auto-event-fields').style.display = trig === 'event' ? '' : 'none';
  document.getElementById('auto-sched-fields').hidden = trig !== 'schedule';
  document.getElementById('auto-event-tags-wrap').style.display = trig === 'event' ? '' : 'none';
  const ev = document.getElementById('auto-event').value;
  document.getElementById('auto-sev-wrap').style.display = ev === 'alert_fired' ? '' : 'none';
  document.getElementById('auto-rule-wrap').style.display = ev === 'alert_fired' ? '' : 'none';

  _autoRefreshActionLabel();

  const tgt = document.getElementById('auto-target').value;
  document.getElementById('auto-target-tags').hidden = tgt !== 'tags';
  document.getElementById('auto-target-host-btn').hidden = tgt !== 'host';
  // scheduled automations can't target the event host
  const evOpt = document.querySelector('#auto-target option[value="event_host"]');
  if (evOpt) evOpt.disabled = trig === 'schedule';
  if (trig === 'schedule' && tgt === 'event_host') { document.getElementById('auto-target').value = 'all'; _autoSyncVisibility(); }
}

function _autoRefreshActionLabel() {
  const kind = document.getElementById('auto-action-kind').value;
  const label = document.getElementById('auto-action-label');
  if (kind === 'task') {
    const id = document.getElementById('auto-action-task').value;
    const d = _autoDefs.find(x => String(x.id) === String(id));
    label.textContent = d ? d.name : 'Choose a task…';
  } else {
    const name = document.getElementById('auto-action-baseline').value;
    label.textContent = name || 'Choose a baseline…';
  }
}

function _autoRefreshHostLabel() {
  const id = document.getElementById('auto-target-host').value;
  const h = _autoHosts.find(x => String(x.id) === String(id));
  document.getElementById('auto-target-host-label').textContent = h ? h.hostname : 'Choose a host…';
}

function _closeAutoEditor() {
  document.getElementById('auto-editor-overlay').classList.remove('open');
  document.getElementById('auto-editor-modal').classList.remove('open');
}

function _openAutoEditor(a) {
  const modal = document.getElementById('auto-editor-modal');
  modal.dataset.editing = a ? a.id : '';
  document.getElementById('auto-editor-title').textContent = a ? 'Edit automation' : 'New automation';
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  set('auto-name', a ? a.name : '');
  set('auto-trigger', a ? a.trigger : 'event');
  set('auto-event', a ? a.event : (Object.keys(_autoEvents)[0] || ''));
  set('auto-event-rule', a && a.event_rule ? a.event_rule : '');
  set('auto-sev', a ? a.min_severity : '');
  set('auto-event-tags', a ? (a.event_tags || []).join(', ') : '');
  const cron = (a && a.cron) || { minute: '0', hour: '2', dom: '*', month: '*', dow: '*' };
  set('auto-cron-min', cron.minute); set('auto-cron-hour', cron.hour); set('auto-cron-dom', cron.dom);
  set('auto-cron-mon', cron.month); set('auto-cron-dow', cron.dow);
  set('auto-action-kind', a ? a.action_kind : 'task');
  set('auto-action-task', a && a.task_definition ? a.task_definition : '');
  set('auto-action-baseline', a ? a.baseline_name : '');
  set('auto-target', a ? a.target : 'event_host');
  set('auto-target-tags', a ? (a.target_tags || []).join(', ') : '');
  set('auto-target-host', a && a.target_host ? a.target_host : '');
  document.getElementById('auto-enabled').checked = a ? a.enabled : true;
  _autoSyncVisibility();
  _autoRefreshActionLabel();
  _autoRefreshHostLabel();
  document.getElementById('auto-editor-overlay').classList.add('open');
  modal.classList.add('open');
}

// View/edit the referenced task (opens the task editor) or baseline (opens
// its editor). Baselines are edited on the Baselines sub-tab.
function _viewAutoAction() {
  const kind = document.getElementById('auto-action-kind').value;
  if (kind === 'task') {
    const id = document.getElementById('auto-action-task').value;
    if (!id) return showToast('Pick a task first', 'error');
    // Stacked task modal — keeps the automation editor open behind it.
    openTaskModal({ id, onSaved: (def) => {
      const idx = _autoDefs.findIndex(d => String(d.id) === String(def.id));
      if (idx >= 0) _autoDefs[idx] = def; else _autoDefs.push(def);
      _autoRefreshActionLabel();
    } });
  } else {
    const name = document.getElementById('auto-action-baseline').value;
    if (!name) return showToast('Pick a baseline first', 'error');
    const b = _autoBaselines.find(x => x.name === name);
    _closeAutoEditor();
    document.querySelector('#page-baselines .sub-tab[data-subtab="bl-panel"]')?.click();
    if (b && typeof _startEdit === 'function') _startEdit(b.id);
  }
}

async function _saveAutomation() {
  const v = id => document.getElementById(id).value.trim();
  const body = {
    name: v('auto-name'),
    trigger: v('auto-trigger'),
    event: v('auto-event'),
    event_rule: v('auto-event-rule') || null,
    min_severity: v('auto-sev'),
    event_tags: v('auto-event-tags').split(',').map(s => s.trim()).filter(Boolean),
    cron: { minute: v('auto-cron-min'), hour: v('auto-cron-hour'), dom: v('auto-cron-dom'),
            month: v('auto-cron-mon'), dow: v('auto-cron-dow') },
    action_kind: v('auto-action-kind'),
    task_definition: v('auto-action-task') || null,
    baseline_name: v('auto-action-baseline'),
    target: v('auto-target'),
    target_tags: v('auto-target-tags').split(',').map(s => s.trim()).filter(Boolean),
    target_host: v('auto-target-host') || null,
    enabled: document.getElementById('auto-enabled').checked,
  };
  if (!body.name) return showToast('Name the automation', 'error');
  const editing = document.getElementById('auto-editor-modal').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/automations/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/automations/', { method: 'POST', body: JSON.stringify(body) });
    showToast('Automation saved', 'success');
    _closeAutoEditor();
    loadAutomations();
  } catch (e) { showToast(e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  ['auto-trigger', 'auto-event', 'auto-action-kind', 'auto-target'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', _autoSyncVisibility);
  });
  const nb = document.getElementById('auto-new-btn');
  if (nb) nb.addEventListener('click', () => _openAutoEditor(null));
  const save = document.getElementById('auto-save-btn');
  if (save) save.addEventListener('click', _saveAutomation);
  document.getElementById('auto-cancel-btn')?.addEventListener('click', _closeAutoEditor);
  document.getElementById('auto-cancel-btn-2')?.addEventListener('click', _closeAutoEditor);
  document.getElementById('auto-editor-overlay')?.addEventListener('click', _closeAutoEditor);
  document.getElementById('auto-view-action')?.addEventListener('click', _viewAutoAction);
  document.getElementById('auto-action-btn')?.addEventListener('click', _autoPickAction);
  document.getElementById('auto-target-host-btn')?.addEventListener('click', _autoPickHost);
  // Changing task↔baseline clears the previous choice so the label is honest.
  document.getElementById('auto-action-kind')?.addEventListener('change', () => {
    document.getElementById('auto-action-task').value = '';
    document.getElementById('auto-action-baseline').value = '';
    _autoRefreshActionLabel();
  });
});
