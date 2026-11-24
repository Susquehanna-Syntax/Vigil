// @ts-check
// vigil-automations.js
// Owns: the Automation sub-panel on the Playbooks page. Create rules that run
// a task or playbook when an event fires (with severity/tag filters) or on a
// cron schedule. Backed by /api/v1/automations/.
// Depends on: vigil-utils.js (apiJson, confirmModal, showToast, escHtml).

/** The five cron fields as `views._row` renders them — each a string. */
/** @typedef {{minute: string, hour: string, dom: string, month: string, dow: string}} AutomationCron */
/** One automation as `apps/automations/views.py:_row` renders it and as the
 *  editor reads it back. Every id in that JSON is already `str(...)`-ed, so all
 *  of them — and the ones a dataset attribute or a hash link supplies — are
 *  strings here; that is also why rows compare with String() rather than ===.
 *  `auto_schedule_display` is the annotation `_index` adds on top of `_row`,
 *  and an automation opened from `#open/automation/<id>` before the index
 *  arrives carries only its id. */
/** @typedef {{id: string, name?: string, enabled?: boolean, allow_high_risk?: boolean,
 *  site_id?: string|null, trigger?: string, event?: string, min_severity?: string,
 *  event_tags?: string[], event_rule?: string|null, event_rule_name?: string|null,
 *  event_host?: string|null, event_host_name?: string|null, match_text?: string,
 *  match_field?: string, match_mode?: string, condition_logic?: string,
 *  conditions?: {field: string, op: string, value?: any}[], cron?: AutomationCron,
 *  cron_display?: string, auto_schedule_display?: string|null, action_kind?: string,
 *  dispatch_mode?: string, task_definition?: string|null, task_auto_sync?: boolean,
 *  task_stale?: boolean, task_name?: string|null, playbook_name?: string,
 *  params_override?: Record<string, any>, inputs?: Record<string, any>,
 *  target?: string, target_tags?: string[], target_host?: string|null,
 *  last_run?: string|null, run_count?: number} & Record<string, any>} AutomationRow */
/** A task definition row / a playbook row / a host row, as far as this panel reads
 *  them: an id and a name, plus the declared `inputs` the Inputs form mounts. */
/** @typedef {{id: string|number, name: string, hostname?: string, inputs?: any[],
 *  parsed_spec?: {inputs?: any[], actions?: {type: string, params?: Record<string, any>}[]}}} AutoPickRow */
/** A task definition detail row, as far as the Inputs form and `openInputsModal`
 *  read it: a name plus the parsed spec's declared `inputs` and per-action list.
 *  The API is the only writer of this cache, so both are always present; the
 *  modal reads `parsed_spec.actions` and requires a `name`. */
/** @typedef {{id: string|number, name: string, inputs?: any[],
 *  parsed_spec: {inputs?: any[], actions?: {type: string, params?: Record<string, any>}[]}}} AutoTaskDef */

/** One condition field of /api/v1/automations/condition-meta/ — `kind` is
 *  'severity' | 'number' | 'string' | 'bool', and `operators` holds the operator
 *  names this field accepts. */
/** @typedef {{name: string, label: string, kind: string, help: string, operators: string[]}} AutoCondField */
/** One operator of the same payload. */
/** @typedef {{name: string, label: string}} AutoCondOperator */
/** What /api/v1/automations/condition-meta/ returns. */
/** @typedef {{fields: AutoCondField[], operators: AutoCondOperator[], valueless: string[]}} AutoCondMeta */

/** event name -> label */
/** @type {Record<string, string>} */
let _autoEvents = {};      // event name -> label
/** @type {AutoCondMeta} */
let _autoCondMeta = { fields: [], operators: [], valueless: [] };
/** @type {AutoPickRow[]} task definitions */
let _autoDefs = [];        // task definitions
/** @type {AutoPickRow[]} playbook names */
let _autoPlaybooks = [];   // playbook names
/** @type {AutoPickRow[]} selectable hosts */
let _autoHosts = [];       // selectable hosts
/** @type {{id: string|number, name: string, severity?: string}[]} alert rules (for specific-event) */
let _autoRules = [];       // alert rules (for specific-event)
/** task input overrides for the automation being edited */
/** @type {Record<string, Record<string, any>>} */
let _autoParamsOverride = {};  // task input overrides for the automation being edited
/** values for the picked playbook's or task's declared inputs */
/** @type {Record<string, any>} */
let _autoInputs = {};          // values for the picked playbook's or task's declared inputs
/** definition id → its detail row, so the Inputs button is instant */
/** @type {Record<string, AutoTaskDef>} */
let _autoDefCache = {};        // definition id → its detail row, so the Inputs button is instant
/** playbook id → its detail row, for the same reason on playbooks */
/** @type {Record<string|number, {inputs?: any[]}>} */
let _autoPlaybookCache = {};   // playbook id → its detail row, for the same reason on playbooks
/** unfiltered, so the search box can re-filter without refetching */
/** @type {AutomationRow[]} */
let _allAutomations = [];  // unfiltered, so the search box can re-filter without refetching

async function loadAutomations() {
  const list = document.getElementById('automations-list');
  if (!list) return;
  list.innerHTML = loadingHtml();
  try {
    const [data, defs, playbooks, hosts, rules] = await Promise.all([
      apiJson('/api/v1/automations/'),
      apiJson('/api/v1/tasks/definitions/'),
      apiJson('/api/v1/playbooks/'),
      apiJson('/api/v1/status-pages/hosts/'),
      apiJson('/api/v1/alerts/rules/').catch(() => []), /* state-ok: alert-rule names are an extra on the trigger row; the automation still loads and works without them */
    ]);
    _autoEvents = data.events || {};
    if (data.condition_meta) _autoCondMeta = data.condition_meta;
    _autoDefs = Array.isArray(defs) ? defs : (defs.results || []);
    _autoPlaybooks = playbooks;
    _autoHosts = hosts;
    _autoRules = rules;
    _fillEditorOptions();
    _allAutomations = data.automations;
    _renderAutomations(_filterAutomations());
    // The #open/automation/<id> deep link needs the row it is named after,
    // and this is the fetch that has it.
    return _allAutomations;
  } catch (e) {
    showLoadFailure(list, t('automations'), e, loadAutomations);
  }
}

function _filterAutomations() {
  const q = (/** @type {HTMLInputElement|null} */ (document.getElementById('auto-search'))?.value || '').trim().toLowerCase();
  if (!q) return _allAutomations;
  return _allAutomations.filter(a =>
    (a.name || '').toLowerCase().includes(q) ||
    (a.trigger || '').toLowerCase().includes(q) ||
    (a.event || '').toLowerCase().includes(q) ||
    (a.playbook_name || '').toLowerCase().includes(q) ||
    (a.task_name || '').toLowerCase().includes(q) ||
    (a.target_tags || []).some(tag => tag.toLowerCase().includes(q)));
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('auto-search')?.addEventListener(
    'input', () => _renderAutomations(_filterAutomations()));
});

function _renderAutomations(autos) {
  const list = document.getElementById('automations-list');
  if (!autos.length) {
    list.innerHTML = `<div class="empty-block">
      <h4>${escHtml(t('No automations yet'))}</h4>
      <p>${escHtml(t('Run a task or playbook automatically — for example "when a critical alert fires, run the cleanup playbook on that host," or "every night at 2am, run backups on the backup hosts."'))}</p></div>`;
    return;
  }
  list.innerHTML = autos.map(a => {
    const when = a.trigger === 'event'
      // Each sentence is trusted catalog text; its values carry their own escaped markup.
      ? `<span class="automation-badge event">${escHtml(t('on event'))}</span> `
        + t('when {event}', { event: `<b>${escHtml(_autoEvents[a.event] || a.event)}</b>` })
        + `${a.event_rule_name ? ` — <b>${escHtml(a.event_rule_name)}</b>` : ''}`
        + `${a.event_host_name ? ` ${t('on {host}', { host: `<b>${escHtml(a.event_host_name)}</b>` })}` : ''}`
        + `${_autoCondSummary(a)}`
      : `<span class="automation-badge schedule">${escHtml(t('scheduled'))}</span> `
        + t('cron {expression}', { expression: `<code class="inline">${escHtml(a.cron_display)}</code>` });
    const action = a.action_kind === 'playbook'
      ? t('playbook {name}', { name: `<b>${escHtml(a.playbook_name)}</b>` })
      : t('task {name}', { name: `<b>${escHtml(a.task_name || '?')}</b>` });
    const target = { event_host: t('the event host'), tags: t('hosts tagged {tags}', { tags: (a.target_tags || []).join(', ') }),
                     host: t('a specific host'), all: t('all managed hosts') }[a.target] || a.target;
    const last = a.last_run ? new Date(a.last_run).toLocaleString() : t('never');
    return `<div class="bl-card">
      <div class="bl-card-head">
        <div>
          <span class="bl-name">${escHtml(a.name)}</span>
          <span class="bl-badge ${a.enabled ? 'on' : 'off'}">${a.enabled ? escHtml(t('enabled')) : escHtml(t('off'))}</span>
        </div>
        <div class="card-actions">
          <button class="btn btn-sky btn-xs" data-au-run="${a.id}">${escHtml(t('Run now'))}</button>
          <button class="btn btn-${a.enabled ? 'lemon' : 'mint'} btn-xs" data-au-toggle="${a.id}" data-en="${a.enabled}">${a.enabled ? escHtml(t('Disable')) : escHtml(t('Enable'))}</button>
          <button class="btn btn-sky btn-xs" data-au-edit="${a.id}">${escHtml(t('Edit'))}</button>
          <button class="btn btn-rose btn-xs" data-au-del="${a.id}">${escHtml(t('Delete'))}</button>
        </div>
      </div>
      <div class="muted-note auto-card-note">${t('{when} → run {action} on {target}.', { when, action, target: escHtml(target) })}</div>
      <div class="bl-meta"><span>${tn('Ran {n} time', 'Ran {n} times', a.run_count, { n: `<b>${a.run_count}</b>` })}</span><span>${escHtml(t('Last run:'))} ${escHtml(last)}</span></div>
    </div>`;
  }).join('');
  _wireAutoCards(autos);
}

function _wireAutoCards(autos) {
  const list = document.getElementById('automations-list');
  list.querySelectorAll('[data-au-del]').forEach((/** @type {HTMLElement & {dataset: AutomationCardDataset}} */ b) => b.addEventListener('click', async () => {
    if (!(await confirmModal(t('Delete this automation?'), { danger: true, confirmText: t('Delete') }))) return;
    await fetch(`/api/v1/automations/${b.dataset.auDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadAutomations();
  }));
  list.querySelectorAll('[data-au-toggle]').forEach((/** @type {HTMLElement & {dataset: AutomationCardDataset}} */ b) => b.addEventListener('click', async () => {
    await apiJson(`/api/v1/automations/${b.dataset.auToggle}/`, { method: 'PATCH', body: JSON.stringify({ enabled: b.dataset.en !== 'true' }) });
    loadAutomations();
  }));
  list.querySelectorAll('[data-au-run]').forEach((/** @type {HTMLElement & {dataset: AutomationCardDataset}} */ b) => b.addEventListener('click', async () => {
    try { const r = await apiJson(`/api/v1/automations/${b.dataset.auRun}/run/`, { method: 'POST', body: '{}' });
      if (!r.dispatched && r.reason) showToast(t('Nothing dispatched. {reason}', { reason: r.reason }), 'error');
      else showToast(tn('Dispatched to {n} host', 'Dispatched to {n} hosts', r.dispatched), 'success');
      loadAutomations();
    } catch (e) { showToast(t('Run failed: {message}', { message: e.message }), 'error'); }
  }));
  list.querySelectorAll('[data-au-edit]').forEach((/** @type {HTMLElement & {dataset: AutomationCardDataset}} */ b) => b.addEventListener('click', () =>
    _openAutoEditor(autos.find(a => a.id === b.dataset.auEdit))));
}

/** @typedef {{auDel?: string, auToggle?: string, auRun?: string, auEdit?: string, en?: string}} AutomationCardDataset */

/* ── Editor ──────────────────────────────────────────────────────────── */
function _fillEditorOptions() {
  // Only the event trigger is still a native select; task/playbook/host are
  // chosen through the searchable picker modal.
  // One select for the whole trigger. "schedule" is a value alongside the
  // events rather than a separate kind, because picking a kind and then an
  // event was two choices to express one thing.
  const ev = /** @type {HTMLSelectElement} */ (document.getElementById('auto-trigger'));
  if (ev) ev.innerHTML =
    Object.entries(_autoEvents).map(([k, v]) => `<option value="${escAttr(k)}">${escHtml(v)}</option>`).join('')
    + `<option value="schedule">${escHtml(t('On a schedule'))}</option>`;
  const rule = /** @type {HTMLSelectElement} */ (document.getElementById('auto-event-rule'));
  if (rule) rule.innerHTML = `<option value="">${escHtml(t('any alert'))}</option>` +
    _autoRules.map(r => `<option value="${escAttr(String(r.id))}">${escHtml(r.name)} (${escHtml(r.severity)})</option>`).join('');
  // Scopes which alerts fire this automation, not which hosts it runs on.
  // The host itself is chosen through the picker, so there is no <option>
  // list to rebuild here — only the button's label needs refreshing.
  _autoRefreshEventHostLabel();
}

// Open the picker for the current action kind and store the choice.
function _autoPickAction() {
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  if (kind === 'task') {
    openPicker({ type: 'task', title: t('Pick a task to run'), onSelect: (item) => {
      if (item.raw && !_autoDefs.some(d => String(d.id) === String(item.key))) _autoDefs.push(item.raw);
      /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value = item.key;
      _autoParamsOverride = {};   // new task — old inputs are meaningless
      _autoInputs = {};           // ditto for its declared inputs
      _autoRefreshActionLabel();
    } });
  } else {
    openPicker({ type: 'playbook', title: t('Pick a playbook to run'), onSelect: (item) => {
      if (!_autoPlaybooks.some(b => b.name === item.key)) _autoPlaybooks.push(item.raw);
      /** @type {HTMLInputElement} */ (document.getElementById('auto-action-playbook')).value = item.key;
      _autoInputs = {};
      _autoRefreshActionLabel();
    } });
  }
}

function _autoPickHost() {
  openPicker({ type: 'machine', title: t('Pick a host'), allowAdd: false, onSelect: (item) => {
    if (!_autoHosts.some(h => String(h.id) === String(item.key))) _autoHosts.push(item.raw);
    /** @type {HTMLInputElement} */ (document.getElementById('auto-target-host')).value = item.key;
    _autoRefreshHostLabel();
  } });
}

// Inputs for the action the editor has picked: a playbook's declared inputs,
// or the task's. The playbook list row carries them; a task's are in its
// definition detail, fetched once per editor session.
async function _autoActionInputs() {
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  if (kind === 'playbook') {
    const name = (/** @type {HTMLInputElement} */ (document.getElementById('auto-action-playbook')).value || '').trim().toLowerCase();
    if (!name) return [];
    const row = _autoPlaybooks.find(p => (p.name || '').toLowerCase() === name);
    if (!row) return [];
    if (row.inputs) return row.inputs;
    if (!_autoPlaybookCache[row.id]) {
      _autoPlaybookCache[row.id] = await apiJson(`/api/v1/playbooks/${row.id}/`)
        .catch(() => ({ inputs: [] }));
    }
    return _autoPlaybookCache[row.id].inputs || [];
  }
  const id = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value;
  if (!id) return [];
  if (!_autoDefCache[id]) {
    _autoDefCache[id] = /** @type {AutoTaskDef} */ (await apiJson(`/api/v1/tasks/definitions/${id}/`)
      .catch(() => ({ id: id, name: '', parsed_spec: {} })));
  }
  return (_autoDefCache[id].parsed_spec && _autoDefCache[id].parsed_spec.inputs) || [];
}

function _autoInputsNote() {
  const note = /** @type {HTMLElement} */ (document.getElementById('auto-inputs-note'));
  if (!note) return;
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind'))?.value;
  const n = Object.keys(_autoInputs || {}).length;
  if (n && (kind === 'task' || kind === 'playbook')) {
    note.hidden = false;
    note.textContent = tn('{n} input set — Inputs', '{n} inputs set — Inputs', n);
  } else {
    note.hidden = true;
    note.textContent = '';
  }
}

// The Inputs button. A target that declares inputs gets the same form as the
// deploy dialog; a task that declares none keeps the old per-action override
// editor, and a playbook with none says so — there is nothing to fill.
async function _openAutoInputs() {
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  /** @type {any[]} */
  let inputs;
  try {
    inputs = await _autoActionInputs();
  } catch (e) {
    return showToast(t("Could not load this target's inputs: {message}", { message: e.message || t('Request failed') }), 'error');
  }
  if (!inputs.length) {
    if (kind === 'playbook') return showToast(t('This playbook has no inputs'), 'error');
    return _openAutoParamOverride();
  }
  const m = mountModal('auto-inputs', { variant: 'm-pop' });
  m.setBody(`<div class="modal-title">
      <span>${escHtml(t('Inputs'))}</span>
      <button class="modal-close" data-ai-close aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="muted-note auto-inputs-note">${escHtml(t('The automation runs with these values, whenever it fires — nobody is asked when it does.'))}</div>
    <div class="inputs-form" id="ai-form"></div>
    <div class="confirm-actions">
      ${kind === 'task' ? `<button class="btn btn-outline btn-sm" data-ai-adv>${escHtml(t('Advanced: override action params'))}</button>` : ''}
      <button class="btn btn-outline btn-sm" data-ai-close>${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" data-ai-save>${escHtml(t('Save'))}</button>
    </div>`);
  inputsFormMount(/** @type {HTMLElement} */ (m.modal.querySelector('#ai-form')), inputs, _autoInputs);
  m.modal.querySelectorAll('[data-ai-close]').forEach(b => b.addEventListener('click', m.close));
  m.modal.querySelector('[data-ai-adv]')?.addEventListener('click', () => {
    m.close();
    _openAutoParamOverride();
  });
  m.modal.querySelector('[data-ai-save]').addEventListener('click', () => {
    const read = inputsFormRead(/** @type {HTMLElement} */ (m.modal.querySelector('#ai-form')));
    if (read.error) return showToast(read.error, 'error');
    _autoInputs = read.values;
    _autoInputsNote();
    m.close();
  });
  m.open();
}

function _openAutoParamOverride() {
  const id = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value;
  // A picker row is the same /api/v1/tasks/definitions/ row the Playbooks page
  // types as TaskDef, and openInputsModal reads that shape (`name`,
  // `parsed_spec.actions`).
  const def = _autoDefs.find(d => String(d.id) === id);
  if (!def) return showToast(t('Pick a task first'), 'error');
  openInputsModal({ def, override: _autoParamsOverride,
    onSave: (ov) => { _autoParamsOverride = ov; } });
}

function _autoSyncVisibility() {
  const ev = /** @type {HTMLSelectElement} */ (document.getElementById('auto-trigger')).value;
  const isSchedule = ev === 'schedule';
  const trig = isSchedule ? 'schedule' : 'event';
  /** @type {HTMLElement} */ (document.getElementById('auto-event-fields')).style.display = isSchedule ? 'none' : '';
  document.getElementById('auto-sched-fields').hidden = !isSchedule;
  // The rule picker only means anything for an alert event, and there is more
  // than one of those now.
  const isAlert = !isSchedule && ev.startsWith('alert_');
  /** @type {HTMLElement} */ (document.getElementById('auto-rule-wrap')).style.display = isAlert ? '' : 'none';
  // A host scope makes sense for any event that carries one.
  const hostWrap = document.getElementById('auto-event-host-wrap');
  if (hostWrap) hostWrap.style.display = isSchedule ? 'none' : '';
  // Conditions read an event payload, so they mean nothing on a schedule.
  const condWrap = document.getElementById('auto-cond-wrap');
  if (condWrap) condWrap.style.display = isSchedule ? 'none' : '';

  _autoRefreshActionLabel();

  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  const inputsBtn = document.getElementById('auto-inputs-btn');
  if (inputsBtn) inputsBtn.hidden = false;
  _autoInputsNote();

  const tgt = /** @type {HTMLSelectElement} */ (document.getElementById('auto-target')).value;
  document.getElementById('auto-target-tags').hidden = tgt !== 'tags';
  document.getElementById('auto-target-host-btn').hidden = tgt !== 'host';
  // scheduled automations can't target the event host
  const evOpt = /** @type {HTMLOptionElement} */ (document.querySelector('#auto-target option[value="event_host"]'));
  if (evOpt) evOpt.disabled = trig === 'schedule';
  if (trig === 'schedule' && tgt === 'event_host') { /** @type {HTMLSelectElement} */ (document.getElementById('auto-target')).value = 'all'; _autoSyncVisibility(); }
}

function _autoRefreshActionLabel() {
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  const label = document.getElementById('auto-action-label');
  if (kind === 'task') {
    const id = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value;
    const d = _autoDefs.find(x => String(x.id) === String(id));
    label.textContent = d ? d.name : t('Choose a task…');
  } else {
    const name = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-playbook')).value;
    label.textContent = name || t('Choose a playbook…');
  }
}

function _autoRefreshHostLabel() {
  const id = /** @type {HTMLInputElement} */ (document.getElementById('auto-target-host')).value;
  const h = _autoHosts.find(x => String(x.id) === String(id));
  /** @type {HTMLElement} */ (document.getElementById('auto-target-host-label')).textContent = h ? h.hostname : t('Choose a host…');
}

// Whether the automation was already allowed high-risk steps when the editor
// opened: a TOTP code is asked only when the box is newly ticked.
let _autoHighRiskWasOn = false;

// The warning shows while the box is ticked and the authorization is new.

// The warning shows while the box is ticked and the authorization is new.
function _autoSyncHighRiskWarning() {
  const box = /** @type {HTMLInputElement} */ (document.getElementById('auto-allow-high-risk'));
  const warn = /** @type {HTMLElement} */ (document.getElementById('auto-highrisk-warn'));
  if (box && warn) warn.style.display = box.checked && !_autoHighRiskWasOn ? '' : 'none';
}

function _closeAutoEditor() {
  document.getElementById('auto-editor-overlay').classList.remove('open');
  document.getElementById('auto-editor-modal').classList.remove('open');
}

/** @param {AutomationRow|null} a @returns {void} */
function _openAutoEditor(a) {
  const modal = /** @type {HTMLElement} */ (document.getElementById('auto-editor-modal'));
  // dataset.editing is read back at save time to pick PATCH vs POST; a dataset
  // value is a string on the way in and on the way out.
  modal.dataset.editing = a ? a.id : '';
  modal.dataset.popout = a && a.id ? `#open/automation/${a.id}` : '';
  /** @type {HTMLElement} */ (document.getElementById('auto-editor-title')).textContent = a ? t('Edit automation') : t('New automation');
  /** @param {string} id @param {any} v @returns {void} */
  const set = (id, v) => { const el = /** @type {HTMLInputElement} */ (document.getElementById(id)); if (el) el.value = v; };
  set('auto-name', a ? a.name : '');
  sitePickFill(/** @type {HTMLSelectElement} */ (document.getElementById('auto-site')), /** @type {HTMLElement|null} */ (document.getElementById('auto-site-field')),
    a ? a.site_id : null, { editing: !!a });
  set('auto-trigger', a
    ? (a.trigger === 'schedule' ? 'schedule' : a.event)
    : (Object.keys(_autoEvents)[0] || 'schedule'));
  set('auto-event-rule', a && a.event_rule ? a.event_rule : '');
  set('auto-event-host', a && a.event_host ? a.event_host : '');
  set('auto-cond-logic', (a && a.condition_logic) || 'all');
  _autoConds = a && Array.isArray(a.conditions) ? a.conditions.map(c => ({ ...c })) : [];
  _renderAutoConditions();
  const cron = (a && a.cron) || { minute: '0', hour: '2', dom: '*', month: '*', dow: '*' };
  set('auto-cron-min', cron.minute); set('auto-cron-hour', cron.hour); set('auto-cron-dom', cron.dom);
  set('auto-cron-mon', cron.month); set('auto-cron-dow', cron.dow);
  set('auto-action-kind', a ? a.action_kind : 'task');
  set('auto-action-task', a && a.task_definition ? a.task_definition : '');
  set('auto-action-playbook', a ? a.playbook_name : '');
  set('auto-target', a ? a.target : 'event_host');
  set('auto-dispatch-mode', a ? (a.dispatch_mode || 'direct') : 'direct');
  set('auto-target-tags', a ? (a.target_tags || []).join(', ') : '');
  set('auto-target-host', a && a.target_host ? a.target_host : '');
  _autoParamsOverride = (a && a.params_override) || {};
  _autoInputs = (a && a.inputs) || {};
  _autoDefCache = {};
  _autoPlaybookCache = {};
  /** @type {HTMLInputElement} */ (document.getElementById('auto-enabled')).checked = a ? a.enabled : true;
  _autoHighRiskWasOn = !!(a && a.id && a.allow_high_risk);
  /** @type {HTMLInputElement} */ (document.getElementById('auto-allow-high-risk')).checked = _autoHighRiskWasOn;
  _autoSyncHighRiskWarning();
  _autoSyncVisibility();
  _autoRefreshActionLabel();
  _autoRefreshHostLabel();
  _autoInputsNote();
  document.getElementById('auto-editor-overlay').classList.add('open');
  modal.classList.add('open');
}

// View/edit the referenced task (opens the task editor) or playbook (opens
// its editor). Playbooks are edited on the Playbooks sub-tab.
function _viewAutoAction() {
  const kind = /** @type {HTMLSelectElement} */ (document.getElementById('auto-action-kind')).value;
  if (kind === 'task') {
    const id = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value;
    if (!id) return showToast(t('Pick a task first'), 'error');
    // Stacked task modal — keeps the automation editor open behind it.
    openTaskModal({ id, onSaved: (/** @type {AutoPickRow} */ def) => {
      const idx = _autoDefs.findIndex(d => String(d.id) === String(def.id));
      if (idx >= 0) _autoDefs[idx] = def; else _autoDefs.push(def);
      _autoRefreshActionLabel();
    } });
  } else {
    const name = /** @type {HTMLInputElement} */ (document.getElementById('auto-action-playbook')).value;
    if (!name) return showToast(t('Pick a playbook first'), 'error');
    const b = _autoPlaybooks.find(x => x.name === name);
    _closeAutoEditor();
    /** @type {HTMLElement} */ (document.querySelector('#page-playbooks .sub-tab[data-subtab="bl-panel"]'))?.click();
    if (b && typeof _startEdit === 'function') _startEdit(b.id);
  }
}

async function _saveAutomation() {
  /** @param {string} id @returns {string} */
  const v = id => /** @type {HTMLInputElement} */ (document.getElementById(id)).value.trim();
  // One select carries both: "schedule" is the kind, anything else is the
  // event and implies the kind.
  const when = v('auto-trigger');
  const isSchedule = when === 'schedule';
  const body = {
    name: v('auto-name'),
    trigger: isSchedule ? 'schedule' : 'event',
    event: isSchedule ? '' : when,
    event_rule: v('auto-event-rule') || null,
    event_host: v('auto-event-host') || null,
    condition_logic: v('auto-cond-logic'),
    conditions: _readAutoConditions(),
    cron: { minute: v('auto-cron-min'), hour: v('auto-cron-hour'), dom: v('auto-cron-dom'),
            month: v('auto-cron-mon'), dow: v('auto-cron-dow') },
    action_kind: v('auto-action-kind'),
    task_definition: v('auto-action-task') || null,
    playbook_name: v('auto-action-playbook'),
    target: v('auto-target'),
    dispatch_mode: v('auto-dispatch-mode'),
    target_tags: v('auto-target-tags').split(',').map(s => s.trim()).filter(Boolean),
    target_host: v('auto-target-host') || null,
    params_override: _autoParamsOverride,
    inputs: _autoInputs,
    enabled: /** @type {HTMLInputElement} */ (document.getElementById('auto-enabled')).checked,
    allow_high_risk: /** @type {HTMLInputElement} */ (document.getElementById('auto-allow-high-risk')).checked,
  };
  const siteId = sitePickValue(/** @type {HTMLSelectElement} */ (document.getElementById('auto-site')), /** @type {HTMLElement|null} */ (document.getElementById('auto-site-field')));
  if (siteId !== undefined) body.site_id = siteId;
  if (!body.name) return showToast(t('Name the automation'), 'error');
  // Only asked when the flag is being turned on: codes are single-use, so
  // one spent on a no-op change is burned for nothing.
  if (body.allow_high_risk && !_autoHighRiskWasOn) {
    const totp = await totpPrompt(t('High-risk steps will run unattended every time this automation fires.'),
      { title: t('Allow high-risk steps'), confirmText: t('Authorize and save') });
    if (!totp) return;
    body.totp = totp;
  }
  const editing = document.getElementById('auto-editor-modal').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/automations/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/automations/', { method: 'POST', body: JSON.stringify(body) });
    showToast(t('Automation saved'), 'success');
    _closeAutoEditor();
    loadAutomations();
  } catch (e) { showToast(e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  ['auto-trigger', 'auto-action-kind', 'auto-target'].forEach(id => {
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
  // Changing task↔playbook clears the previous choice so the label is honest.
  document.getElementById('auto-action-kind')?.addEventListener('change', () => {
    /** @type {HTMLInputElement} */ (document.getElementById('auto-action-task')).value = '';
    /** @type {HTMLInputElement} */ (document.getElementById('auto-action-playbook')).value = '';
    _autoParamsOverride = {};
    _autoInputs = {};
    _autoRefreshActionLabel();
  });
  document.getElementById('auto-inputs-btn')?.addEventListener('click', _openAutoInputs);
  document.getElementById('auto-allow-high-risk')?.addEventListener('change', _autoSyncHighRiskWarning);
});


document.addEventListener('DOMContentLoaded', () => {
  const mode = /** @type {HTMLSelectElement} */ (document.getElementById('auto-dispatch-mode'));
  const note = document.getElementById('auto-dispatch-hint');
  if (!mode || !note) return;
  const sync = () => { note.hidden = mode.value !== 'rollout'; };
  mode.addEventListener('change', sync);
  sync();
});


/* ── Event-host picker (which alerts fire this, not which hosts it runs on) ── */

function _autoRefreshEventHostLabel() {
  const hidden = /** @type {HTMLInputElement} */ (document.getElementById('auto-event-host'));
  const label = document.getElementById('auto-event-host-label');
  if (!hidden || !label) return;
  const host = _autoHosts.find(h => String(h.id) === String(hidden.value));
  label.textContent = hidden.value ? (host ? (host.hostname || hidden.value) : hidden.value) : t('any host');
}

function _autoPickEventHost() {
  openPicker({ type: 'machine', title: t('Only fire for events on this host'), allowAdd: false,
    onSelect: (item) => {
      if (!_autoHosts.some(h => String(h.id) === String(item.key))) _autoHosts.push(item.raw);
      /** @type {HTMLInputElement} */ (document.getElementById('auto-event-host')).value = item.key;
      _autoRefreshEventHostLabel();
    } });
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('auto-event-host-btn')?.addEventListener('click', _autoPickEventHost);
  document.getElementById('auto-event-host-clear')?.addEventListener('click', () => {
    /** @type {HTMLInputElement} */ (document.getElementById('auto-event-host')).value = '';
    _autoRefreshEventHostLabel();
  });
});


/* Conditions in words, for the card. The list used to print the severity and
   tag filters; those are conditions now, so this is what replaced them. */
function _autoCondSummary(a) {
  const items = a.conditions || [];
  if (!items.length) return '';
  const meta = _autoCondMeta.fields || [];
  const label = (n) => (meta.find(f => f.name === n) || {}).label || n;
  const opLabel = (n) => ((_autoCondMeta.operators || []).find(o => o.name === n) || {}).label || n;
  const parts = items.map(c =>
    `${label(c.field)} ${opLabel(c.op)}${c.value ? ' ' + c.value : ''}`);
  const joiner = a.condition_logic === 'any' ? t('or') : t('and');
  return ` <span class="muted">— ${parts.map(p => escHtml(p)).join(' ' + escHtml(joiner) + ' ')}</span>`;
}

/* ── Condition rows ───────────────────────────────────────────────────────
   Built from the field/operator list the server advertises, so the two can
   never drift: a field added in apps/automations/conditions.py appears here
   with no JS change, and an operator the server would refuse is not offered.
   Rows live in _autoConds and are read back on save. */
let _autoConds = [];

function _autoFieldMeta(name) {
  return _autoCondMeta.fields.find(f => f.name === name) || _autoCondMeta.fields[0];
}

/** @param {string} name @returns {string} */
function _autoOpLabel(name) {
  const op = (_autoCondMeta.operators || []).find(o => o.name === name);
  return op ? op.label : name;
}

function _renderAutoConditions() {
  const host = document.getElementById('auto-cond-rows');
  if (!host) return;
  if (!_autoConds.length) {
    host.innerHTML = `<div class="auto-cond-empty">${escHtml(t('No conditions.'))}</div>`;
    return;
  }
  host.innerHTML = _autoConds.map((c, i) => {
    const meta = _autoFieldMeta(c.field) || { operators: [] };
    const fields = _autoCondMeta.fields.map(f =>
      `<option value="${escAttr(f.name)}"${f.name === c.field ? ' selected' : ''}>${escHtml(f.label)}</option>`).join('');
    const ops = (meta.operators || []).map(o =>
      `<option value="${escAttr(o)}"${o === c.op ? ' selected' : ''}>${escHtml(_autoOpLabel(o))}</option>`).join('');
    const needsValue = !(_autoCondMeta.valueless || []).includes(c.op);
    const value = needsValue
      ? `<input type="text" class="form-control auto-cond-value" data-i="${i}" value="${escAttr(c.value || '')}" placeholder="${escAttr(/** @type {AutoCondField} */ (meta).help || t('value'))}" maxlength="500">`
      : '';
    return `<div class="auto-cond-row">`
      + `<select class="form-control auto-cond-field" data-i="${i}">${fields}</select>`
      + `<select class="form-control auto-cond-op" data-i="${i}">${ops}</select>`
      + value
      + `<button type="button" class="btn btn-ghost btn-sm" data-act="removeAutoCondition" data-a1="${i}">${escHtml(t('Remove'))}</button>`
    + `</div>`;
  }).join('');

  host.querySelectorAll('.auto-cond-field').forEach((/** @type {HTMLSelectElement} */ el) => el.addEventListener('change', () => {
    const i = Number(el.dataset.i);
    _autoConds[i].field = el.value;
    // The operator list changes with the field; keep the current operator
    // only when the new field actually supports it.
    const allowed = (_autoFieldMeta(el.value) || { operators: [] }).operators || [];
    if (!allowed.includes(_autoConds[i].op)) _autoConds[i].op = allowed[0] || '';
    _renderAutoConditions();
  }));
  host.querySelectorAll('.auto-cond-op').forEach((/** @type {HTMLSelectElement} */ el) => el.addEventListener('change', () => {
    _autoConds[Number(el.dataset.i)].op = el.value;
    _renderAutoConditions();
  }));
  host.querySelectorAll('.auto-cond-value').forEach((/** @type {HTMLInputElement} */ el) => el.addEventListener('input', () => {
    _autoConds[Number(el.dataset.i)].value = el.value;
  }));
}

function addAutoCondition() {
  const field = _autoCondMeta.fields[0];
  if (!field) return;
  _autoConds.push({ field: field.name, op: (field.operators || [])[0] || '', value: '' });
  _renderAutoConditions();
}

function removeAutoCondition(index) {
  _autoConds.splice(Number(index), 1);
  _renderAutoConditions();
}

function _readAutoConditions() {
  return _autoConds
    .filter(c => c.field && c.op)
    .map(c => ({ field: c.field, op: c.op, value: c.value || '' }));
}
