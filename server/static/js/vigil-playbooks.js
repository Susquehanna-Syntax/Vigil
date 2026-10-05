// vigil-playbooks.js
// Owns: the Playbooks page — a sequence editor for playbooks (ordered task
// definitions that auto-dispatch on host enrollment and are callable from any
// task via `type: playbook`). Rich cards show each step and its action count;
// the editor builds a sequence you can reorder.
// Depends on: vigil-utils.js (apiJson, confirmModal, showToast, escHtml),
//             vigil-playbook-flow.js (the flow tree: editor, read-only view, analysis).

let _playbookDefs = [];   // all task definitions, for the picker (id -> def)
let _editingTree = [];    // the playbook being edited: a flow tree (see vigil-playbook-flow.js)
let _allPlaybooks = [];   // cached, for client-side search

async function loadPlaybooks() {
  const list = document.getElementById('playbooks-list');
  if (!list) return;
  list.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    const [playbooks, defs] = await Promise.all([
      apiJson('/api/v1/playbooks/'),
      apiJson('/api/v1/tasks/definitions/'),
    ]);
    _playbookDefs = Array.isArray(defs) ? defs : (defs.results || []);
    _allPlaybooks = playbooks;
    _renderDefPicker();
    _renderPlaybookList(_filterPlaybooks());
  } catch (e) {
    list.innerHTML = `<div class="empty-block"><h4>Couldn't load playbooks</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

function _filterPlaybooks() {
  const q = (document.getElementById('bl-search')?.value || '').trim().toLowerCase();
  if (!q) return _allPlaybooks;
  return _allPlaybooks.filter(b =>
    b.name.toLowerCase().includes(q) ||
    (b.description || '').toLowerCase().includes(q) ||
    (b.target_tags || []).some(t => t.toLowerCase().includes(q)) ||
    b.steps.some(s => s.definition_name.toLowerCase().includes(q)));
}

// Whether the playbook being edited ALREADY had the flag on when it was
// opened. A TOTP code is only spent turning it on, never re-confirming a
// playbook that was already authorized.
let _blHighRiskWasOn = false;

// The TOTP prompt appears only while the box is being ticked for the first
// time. Re-rendering the steps too: a high-risk step is ineligible or not
// depending on this box, and the list has to say so as soon as it changes.
function _blSyncHighRiskWarning() {
  const warn = document.getElementById('bl-highrisk-warn');
  if (warn) {
    warn.style.display = (_blAllowsHighRisk() && !_blHighRiskWasOn) ? '' : 'none';
  }
  _renderEditorSteps();
}

// Whether the playbook being edited has opted in to high-risk steps. Read
// from the checkbox rather than passed around, so the picker and the save
// path cannot disagree about it.
function _blAllowsHighRisk() {
  const box = document.getElementById('bl-allow-high-risk');
  return !!(box && box.checked);
}

function _blIneligible(def) {
  // Mirrors apps/playbooks/models.py eligible(): playbooks auto-run without
  // per-dispatch 2FA, so update_agent always stays out, and high-risk stays
  // out unless this playbook opted in — which costs a TOTP code to turn on.
  if (!def) return null;
  const risk = def.risk_level || def.risk || 'standard';
  if (risk === 'high' && !_blAllowsHighRisk()) {
    return 'high risk — tick “Allow high-risk steps” to use it here';
  }
  const acts = (def.parsed_spec && def.parsed_spec.actions) || [];
  if (acts.some(a => a.type === 'update_agent')) return 'update_agent — can’t run in a playbook';
  return null;
}

function _defName(id) {
  const d = _playbookDefs.find(x => String(x.id) === String(id));
  return d ? d.name : id;
}
function _defRisk(id) {
  const d = _playbookDefs.find(x => String(x.id) === String(id));
  return d ? (d.risk_level || d.risk || 'standard') : 'standard';
}
function _defActionCount(id) {
  const d = _playbookDefs.find(x => String(x.id) === String(id));
  const acts = d && d.parsed_spec && d.parsed_spec.actions;
  return Array.isArray(acts) ? acts.length : null;
}

function _renderPlaybookList(playbooks) {
  const list = document.getElementById('playbooks-list');
  if (!playbooks.length) {
    list.innerHTML = `<div class="empty-block">
      <h4>No playbooks yet</h4>
      <p>A playbook is a named sequence of tasks that runs automatically when a matching host is approved — and can be called from any task with <code class="inline">type: playbook</code>. Create one to standardise how new machines get set up.</p></div>`;
    return;
  }
  list.innerHTML = playbooks.map(b => {
    const steps = flowReadOnlyHtml(flowTreeFromRow(b));
    const tags = (b.target_tags || []).length
      ? (b.target_tags || []).map(t => `<span class="chip">${escHtml(t)}</span>`).join(' ')
      : '<span class="muted-note">every approved host</span>';
    return `<div class="bl-card">
      <div class="bl-card-head">
        <div>
          <span class="bl-name">${escHtml(b.name)}</span>
          <span class="bl-badge ${b.auto_enroll ? 'on' : 'off'}">${b.auto_enroll ? 'auto-enroll on' : 'auto-enroll off'}</span>
          ${b.completion_tag ? `<span class="chip">done: ${escHtml(b.completion_tag)}</span>` : ''}
          ${b.failing_hosts ? `<span class="chip chip-rose">held back on ${b.failing_hosts} host${b.failing_hosts === 1 ? '' : 's'}</span>` : ''}
          ${b.description ? `<div class="muted-note" style="margin-top:4px;">${escHtml(b.description)}</div>` : ''}
        </div>
        <div class="card-actions">
          <button class="btn btn-${b.auto_enroll ? 'lemon' : 'mint'} btn-xs" data-bl-toggle="${b.id}" data-enabled="${b.auto_enroll}" data-name="${escAttr(b.name)}" data-tags="${escAttr((b.target_tags || []).join(', '))}" data-done="${escAttr(b.completion_tag || '')}">${b.auto_enroll ? 'Turn auto-enroll off' : 'Turn auto-enroll on'}</button>
          ${b.failing_hosts ? `<button class="btn btn-rose btn-xs" data-bl-failures="${b.id}" data-name="${escAttr(b.name)}">Review failures</button>` : ''}
          <button class="btn btn-peach btn-xs" data-bl-dup="${b.id}">Duplicate</button>
          <button class="btn btn-sky btn-xs" data-bl-edit="${b.id}">Edit</button>
          <button class="btn btn-lemon btn-xs" data-bl-archive="${b.id}">Archive</button>
          <button class="btn btn-rose btn-xs" data-bl-del="${b.id}">Delete</button>
        </div>
      </div>
      <div class="bl-seq">${steps}</div>
      <div class="bl-meta">
        <span><b>${b.steps.length}</b> step${b.steps.length === 1 ? '' : 's'}</span>
        <span>Runs on: ${tags}</span>
      </div>
      <div class="bl-call">
        <div class="bl-call-label">Call from a task:</div>
        <pre>${yamlToHtml('- type: playbook\n  params: { name: "' + b.name + '" }')}</pre>
      </div>
    </div>`;
  }).join('');
  _wireCards(playbooks);
}

function _wireCards(playbooks) {
  const list = document.getElementById('playbooks-list');
  list.querySelectorAll('[data-bl-del]').forEach(btn => btn.addEventListener('click', async () => {
    if (!(await confirmModal('Delete this playbook? Tasks that call it by name will start failing.', { danger: true, confirmText: 'Delete' }))) return;
    await fetch(`/api/v1/playbooks/${btn.dataset.blDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadPlaybooks();
  }));
  list.querySelectorAll('[data-bl-toggle]').forEach(btn => btn.addEventListener('click', async () => {
    const turningOn = btn.dataset.enabled !== 'true';
    if (turningOn && !(await _confirmAutoEnroll(btn.dataset))) return;
    try {
      await apiJson(`/api/v1/playbooks/${btn.dataset.blToggle}/`, {
        method: 'PATCH', body: JSON.stringify({ auto_enroll: turningOn }) });
    } catch (e) {
      showToast(e.message || 'Could not change auto-enroll', 'error');
      return;
    }
    loadPlaybooks();
  }));
  list.querySelectorAll('[data-bl-archive]').forEach(btn => btn.addEventListener('click', async () => {
    if (!(await confirmModal(
      'Archive this playbook? It stops auto-enrolling and leaves the list. ' +
      'Anything already referencing it keeps working, and its run history stays.',
      { confirmText: 'Archive', danger: false }))) return;
    await apiJson(`/api/v1/playbooks/${btn.dataset.blArchive}/archive/`, {
      method: 'POST', body: JSON.stringify({}) });
    loadPlaybooks();
  }));
  list.querySelectorAll('[data-bl-failures]').forEach(btn => btn.addEventListener(
    'click', () => _openFailures(btn.dataset.blFailures, btn.dataset.name)));
  list.querySelectorAll('[data-bl-edit]').forEach(btn => btn.addEventListener('click', () => _startEdit(btn.dataset.blEdit)));
  list.querySelectorAll('[data-bl-dup]').forEach(btn => btn.addEventListener('click', () => {
    const b = playbooks.find(x => x.id === btn.dataset.blDup);
    _openEditor({ name: b.name + ' (copy)', description: b.description,
      target_tags: b.target_tags, completion_tag: b.completion_tag,
      steps: b.steps, flow: b.flow }, null);
  }));
}

/* ── Held-back hosts ─────────────────────────────────────────────────── */
/* Auto-enrolment stops on a host whose last run of the playbook failed. That
   is the whole point — a broken playbook used to redispatch to the same
   machine every five minutes for good — but a quarantine nobody can see is
   just a playbook that silently stopped working, so it gets a screen of its
   own with the output that explains each one and the retry that clears it. */
async function _openFailures(playbookId, name) {
  const m = mountModal('bl-failures', { wide: true });
  const head = `<div class="modal-title">
      <span>Held back — ${escHtml(name || 'playbook')}</span>
      <button class="modal-close" data-blf-close aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>`;
  m.setBody(head + '<p class="muted-note">Loading…</p>');
  m.open();

  let rows = [];
  try {
    rows = await apiJson(`/api/v1/playbooks/${playbookId}/failures/`);
  } catch (e) {
    m.setBody(head + `<p class="muted-note">Could not read the failures: ${escHtml(String(e.message || e))}</p>`);
    m.modal.querySelectorAll('[data-blf-close]').forEach(b => b.addEventListener('click', m.close));
    return;
  }

  const body = rows.length
    ? rows.map(r => `<div class="blf-row">
        <div class="blf-head">
          <span class="blf-host">${escHtml(r.hostname)}</span>
          <span class="chip chip-muted">${escHtml(r.state || 'failed')}</span>
          ${r.at ? `<span class="muted-note">${escHtml(new Date(r.at).toLocaleString())}</span>` : ''}
          <button class="btn btn-mint btn-xs" data-blf-retry="${escAttr(r.host_id)}">Retry here</button>
        </div>
        ${r.output ? `<pre class="blf-output">${escHtml(r.output)}</pre>`
                   : '<p class="muted-note">The agent reported no output.</p>'}
      </div>`).join('')
    : '<p class="muted-note">Nothing is held back any more.</p>';

  m.setBody(head
    + '<p class="muted-note">Auto-enrolment stops on a host when its last run of this '
    + 'playbook failed there, so one broken playbook cannot redispatch to the same machine '
    + 'every five minutes. Retrying dispatches it again — that new run is what clears this.</p>'
    + `<div class="blf-list">${body}</div>`
    + `<div class="modal-actions">
         ${rows.length ? `<button class="btn btn-mint" data-blf-retry-all>Retry all ${rows.length}</button>` : ''}
         <button class="btn btn-outline" data-blf-close>Close</button>
       </div>`);

  const retry = async (payload) => {
    await apiJson(`/api/v1/playbooks/${playbookId}/retry/`, {
      method: 'POST', body: JSON.stringify(payload) });
    m.close();
    loadPlaybooks();
  };
  m.modal.querySelectorAll('[data-blf-close]').forEach(b => b.addEventListener('click', m.close));
  m.modal.querySelectorAll('[data-blf-retry]').forEach(b => b.addEventListener(
    'click', () => retry({ host: b.dataset.blfRetry })));
  m.modal.querySelector('[data-blf-retry-all]')?.addEventListener('click', () => retry({}));
}

/* ── Editor ──────────────────────────────────────────────────────────── */
function _renderDefPicker() {
  const sel = document.getElementById('bl-def-picker');
  if (!sel) return;
  sel.innerHTML = '<option value="">+ Add a task to the sequence…</option>' +
    _playbookDefs.map(d => `<option value="${d.id}">${escHtml(d.name)} · ${escHtml(d.risk_level || d.risk || 'standard')}</option>`).join('');
}

function _blDefOutputs(id) {
  const d = _playbookDefs.find(x => String(x.id) === String(id));
  const names = new Set();
  ((d && d.parsed_spec && d.parsed_spec.actions) || []).forEach(a => (a.outputs || []).forEach(o => names.add(o)));
  return [...names];
}

const _blFlowCtx = {
  defName: _defName,
  defRisk: _defRisk,
  defOutputs: _blDefOutputs,
  ineligible: id => _blIneligible(_playbookDefs.find(d => String(d.id) === String(id))),
};

// "<lane>|<index>" → the node and the list it sits in.
function _blAt(at) {
  const [lane, i] = at.split('|');
  const list = flowLane(_editingTree, lane);
  return { list, i: +i, node: list[+i] };
}

let _blSel = null;         // "<lane>|<index>" of the node the inspector shows
let _blView = null;        // the editor canvas's pan / zoom, kept across re-renders

function _renderEditorSteps() {
  const wrap = document.getElementById('bl-editor-steps');
  if (!wrap) return;
  if (_blSel && !_blAt(_blSel).node) _blSel = null;
  wrap.innerHTML = flowEditorHtml(_editingTree, _blFlowCtx, _blSel);
  _blView = flowCanvasMount(wrap.querySelector('.fcv'), _blView);
  _wireFlowCanvas(wrap);
  _renderInspector();
  _refreshAnalysis();
}

function _refreshAnalysis() {
  const analysis = document.getElementById('bl-flow-analysis');
  if (!analysis) return;
  analysis.innerHTML = flowAnalysisHtml(_editingTree, _blFlowCtx);
  flowWirePathHighlight(analysis, document.getElementById('bl-editor-steps'));
}

function _blSelect(at) {
  _blSel = at;
  document.querySelectorAll('#bl-editor-steps [data-flow-sel]').forEach(n =>
    n.classList.toggle('is-sel', n.dataset.flowSel === at));
  _renderInspector();
}

// Put a new node at "<lane>|<index>" and select it.
function _blInsert(at, node) {
  const [lane, i] = at.split('|');
  flowLane(_editingTree, lane).splice(+i, 0, node);
  _blSel = `${lane}|${i}`;
  _renderEditorSteps();
}

function _blAddTask(at) {
  openPicker({ type: 'task', title: 'Add a task to the playbook',
    ineligible: (item) => _blIneligible(item.raw),
    onSelect: (item) => {
      // The task may have been created inside the picker, after the page's defs were loaded.
      if (item.raw && !_playbookDefs.some(d => String(d.id) === String(item.key))) _playbookDefs.push(item.raw);
      _blInsert(at, { kind: 'step', def: String(item.key), ov: {},
        ona: 'stop', onf: 'stop', sid: flowNextSid(_editingTree), color: '' });
    } });
}

// The small menu a "+" on an edge opens: a task or an if / else goes there.
function _blInsertMenu(btn) {
  document.querySelectorAll('.fce-menu').forEach(m => m.remove());
  const vp = btn.closest('.fcv');
  const menu = document.createElement('div');
  menu.className = 'fce-menu';
  menu.setAttribute('role', 'menu');
  menu.innerHTML = `<button type="button" role="menuitem" class="btn btn-mint btn-xs" data-m="task">Task</button>
    <button type="button" role="menuitem" class="btn btn-lav btn-xs" data-m="if">If / else</button>`;
  const vr = vp.getBoundingClientRect();
  const br = btn.getBoundingClientRect();
  menu.style.left = `${br.left - vr.left + br.width / 2}px`;
  menu.style.top = `${br.bottom - vr.top + 6}px`;
  vp.appendChild(menu);
  const at = btn.dataset.flowInsert;
  const close = () => { menu.remove(); document.removeEventListener('pointerdown', outside, true); };
  const outside = (e) => { if (!menu.contains(e.target)) close(); };
  document.addEventListener('pointerdown', outside, true);
  menu.querySelector('[data-m="task"]').addEventListener('click', () => { close(); _blAddTask(at); });
  menu.querySelector('[data-m="if"]').addEventListener('click', () => {
    close();
    _blInsert(at, { kind: 'if', cond: '', then: [], else: [] });
  });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') { close(); btn.focus(); } });
  menu.querySelector('button').focus();
}

function _wireFlowCanvas(wrap) {
  wrap.querySelectorAll('[data-flow-sel]').forEach(el => {
    el.addEventListener('click', () => _blSelect(el.dataset.flowSel));
    el.addEventListener('keydown', (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return;
      e.preventDefault();
      _blRemove(el.dataset.flowSel);
    });
  });
  wrap.querySelectorAll('[data-flow-insert]').forEach(el => el.addEventListener('click', () => _blInsertMenu(el)));
}

function _blRemove(at) {
  const { list, i, node } = _blAt(at);
  const inside = node && node.kind === 'if' ? flowSteps(node.then).length + flowSteps(node.else).length : 0;
  if (inside && !confirm(`Remove this if and the ${inside} step${inside === 1 ? '' : 's'} inside it?`)) return;
  list.splice(i, 1);
  _blSel = null;
  _renderEditorSteps();
}

function _renderInspector() {
  const box = document.getElementById('bl-inspector');
  if (!box) return;
  box.innerHTML = flowInspectorHtml(_editingTree, _blSel, _blFlowCtx);
  const on = (sel, ev, fn) => box.querySelectorAll(sel).forEach(el => el.addEventListener(ev, () => fn(el)));
  on('[data-flow-rm]', 'click', el => _blRemove(el.dataset.flowRm));
  on('[data-flow-mv]', 'click', el => {
    const { list, i } = _blAt(el.dataset.flowMv);
    const j = i + (+el.dataset.dir);
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    _blSel = `${el.dataset.flowMv.split('|')[0]}|${j}`;
    _renderEditorSteps();
  });
  on('[data-flow-color]', 'click', el => { _blAt(el.dataset.flowColor).node.color = el.dataset.c; _renderEditorSteps(); });
  on('[data-flow-ona]', 'change', el => { _blAt(el.dataset.flowOna).node.ona = el.value; _renderEditorSteps(); });
  on('[data-flow-onf]', 'change', el => { _blAt(el.dataset.flowOnf).node.onf = el.value; _renderEditorSteps(); });
  // Typing redraws the canvas only when the field loses focus, so it keeps the caret.
  const redrawCanvas = () => {
    const wrap = document.getElementById('bl-editor-steps');
    wrap.innerHTML = flowEditorHtml(_editingTree, _blFlowCtx, _blSel);
    _blView = flowCanvasMount(wrap.querySelector('.fcv'), _blView);
    _wireFlowCanvas(wrap);
    _refreshAnalysis();
  };
  on('[data-flow-sid]', 'input', el => { _blAt(el.dataset.flowSid).node.sid = el.value.trim(); _refreshAnalysis(); });
  on('[data-flow-sid]', 'change', redrawCanvas);
  on('[data-flow-cond]', 'input', el => {
    _blAt(el.dataset.flowCond).node.cond = el.value;
    const words = box.querySelector('[data-flow-words]');
    if (words) words.textContent = flowCondWords(el.value);
    _refreshAnalysis();
  });
  on('[data-flow-cond]', 'change', redrawCanvas);
  on('[data-flow-outcome]', 'input', el => { _blAt(el.dataset.flowOutcome).node.outcome = el.value; });
  on('[data-flow-outcome]', 'change', redrawCanvas);
  on('[data-flow-inputs]', 'click', el => {
    const { node } = _blAt(el.dataset.flowInputs);
    const def = _playbookDefs.find(d => String(d.id) === node.def);
    if (!def) return showToast('Task not loaded yet', 'error');
    openInputsModal({ def, override: node.ov, onSave: (ov) => { node.ov = ov; _renderEditorSteps(); } });
  });
  on('[data-flow-view]', 'click', el => {
    // Edit the task in a stacked modal — keeps the playbook editor open behind.
    openTaskModal({ id: el.dataset.flowView, onSaved: (def) => {
      const idx = _playbookDefs.findIndex(d => String(d.id) === String(def.id));
      if (idx >= 0) _playbookDefs[idx] = def; else _playbookDefs.push(def);
      _renderEditorSteps();
    } });
  });
  const builder = box.querySelector('[data-flow-builder]');
  if (builder && builder.querySelector('[data-fb-step]')) _blSetupBuilder(builder, builder.dataset.flowBuilder);
}

// The guided condition builder: pick an earlier step, what to test and how.
function _blSetupBuilder(box, at) {
  const stepSel = box.querySelector('[data-fb-step]');
  const whatSel = box.querySelector('[data-fb-what]');
  const opSel = box.querySelector('[data-fb-op]');
  const valueWrap = box.querySelector('[data-fb-value-wrap]');
  const stepBySid = sid => flowSteps(_editingTree).find(s => s.sid === sid);
  const fillWhat = () => {
    const step = stepBySid(stepSel.value);
    const outputs = step ? _blDefOutputs(step.def) : [];
    whatSel.innerHTML = '<option value="status">its result (ok, failed, …)</option>' +
      outputs.map(o => `<option value="${escAttr(o)}">output: ${escHtml(o)}</option>`).join('');
    fillOp();
  };
  const fillOp = () => {
    const status = whatSel.value === 'status';
    opSel.innerHTML = (status ? ['==', '!='] : ['==', '!=', '>', '>=', '<', '<='])
      .map(o => `<option value="${o}">${escHtml({ '==': 'is', '!=': 'is not', '>': '>', '>=': '≥', '<': '<', '<=': '≤' }[o])}</option>`).join('');
    valueWrap.innerHTML = status
      ? `<select data-fb-value>${FLOW_STATUS.map(v => `<option value="${v}">${escHtml(v.replace('_', ' '))}</option>`).join('')}</select>`
      : '<input data-fb-value placeholder="0" size="8">';
  };
  stepSel.onchange = fillWhat;
  whatSel.onchange = fillOp;
  // Start from the step just before the if: usually the one being tested.
  stepSel.selectedIndex = stepSel.options.length - 1;
  fillWhat();
  box.querySelector('[data-fb-use]').onclick = () => {
    const sid = stepSel.value;
    if (!sid) return;
    const raw = (valueWrap.querySelector('[data-fb-value]').value || '').trim();
    let literal;
    if (whatSel.value === 'status' || !/^(-?\d+(\.\d+)?|true|false|True|False)$/.test(raw)) {
      literal = JSON.stringify(raw);
    } else {
      literal = /^(true|True)$/.test(raw) ? 'True' : /^(false|False)$/.test(raw) ? 'False' : raw;
    }
    const lhs = whatSel.value === 'status' ? `steps.${sid}.status` : `steps.${sid}.result.${whatSel.value}`;
    _blAt(at).node.cond = `${lhs} ${opSel.value} ${literal}`;
    _renderEditorSteps();
  };
}

function _openEditor(data, editingId) {
  const modal = document.getElementById('bl-editor-modal');
  modal.dataset.editing = editingId || '';
  document.getElementById('bl-editor-title').textContent = editingId ? 'Edit playbook' : (data && data.name ? 'Duplicate playbook' : 'New playbook');
  document.getElementById('bl-name').value = data ? (data.name || '') : '';
  document.getElementById('bl-desc').value = data ? (data.description || '') : '';
  document.getElementById('bl-tags').value = data ? (data.target_tags || []).join(', ') : '';
  document.getElementById('bl-completion-tag').value = data ? (data.completion_tag || '') : '';
  // Duplicating a playbook does not inherit the authorization: editingId is
  // null there, so the flag starts off and has to be re-confirmed. The
  // original's TOTP authorized that playbook, not a copy of it.
  _blHighRiskWasOn = !!(editingId && data && data.allow_high_risk);
  const allowBox = document.getElementById('bl-allow-high-risk');
  if (allowBox) allowBox.checked = _blHighRiskWasOn;
  const totpInput = document.getElementById('bl-highrisk-totp');
  if (totpInput) totpInput.value = '';
  _blSyncHighRiskWarning();
  _editingTree = data ? flowTreeFromRow(data) : [];
  _blSel = null;
  _blView = null;
  document.getElementById('bl-editor-overlay').classList.add('open');
  modal.classList.add('open');
  _renderEditorSteps();
}

function _closeBlEditor() {
  document.getElementById('bl-editor-overlay').classList.remove('open');
  document.getElementById('bl-editor-modal').classList.remove('open');
}

function _startEdit(id) {
  apiJson(`/api/v1/playbooks/${id}/`).then(b => _openEditor(b, id));
}

async function _savePlaybook() {
  const allowHighRisk = _blAllowsHighRisk();
  const body = {
    name: document.getElementById('bl-name').value.trim(),
    description: document.getElementById('bl-desc').value.trim(),
    target_tags: document.getElementById('bl-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    completion_tag: document.getElementById('bl-completion-tag').value.trim(),
    allow_high_risk: allowHighRisk,
    flow_steps: flowSerialize(_editingTree),
  };
  // Only sent when the flag is being turned on — the server ignores it
  // otherwise, and a code spent on a no-op change is a code burned for
  // nothing (they are single-use within their validity window).
  if (allowHighRisk && !_blHighRiskWasOn) {
    const totp = (document.getElementById('bl-highrisk-totp')?.value || '').trim();
    if (!totp) return showToast('Enter your TOTP code to allow high-risk steps', 'error');
    body.totp = totp;
  }
  if (!body.name) return showToast('Give the playbook a name', 'error');
  const allSteps = flowSteps(_editingTree);
  if (!allSteps.length) return showToast('Add at least one task', 'error');
  for (const s of allSteps) {
    const why = _blIneligible(_playbookDefs.find(d => String(d.id) === String(s.def)));
    if (why) return showToast(`Fix ineligible steps first: ${_defName(s.def)} — ${why}`, 'error');
  }
  const blocking = flowFindings(_editingTree).filter(f => f.text.startsWith('A branch has no condition'));
  if (blocking.length) return showToast('Give every branch a condition before saving', 'error');
  const editing = document.getElementById('bl-editor-modal').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/playbooks/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/playbooks/', { method: 'POST', body: JSON.stringify(body) });
    showToast('Playbook saved', 'success');
    _closeBlEditor();
    loadPlaybooks();
  } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('bl-allow-high-risk')
    ?.addEventListener('change', _blSyncHighRiskWarning);
  const save = document.getElementById('bl-save-btn');
  if (save) save.addEventListener('click', _savePlaybook);
  const nu = document.getElementById('bl-new-btn');
  if (nu) nu.addEventListener('click', () => _openEditor(null, null));
  document.getElementById('bl-cancel-btn')?.addEventListener('click', _closeBlEditor);
  document.getElementById('bl-cancel-btn-2')?.addEventListener('click', _closeBlEditor);
  document.getElementById('bl-editor-overlay')?.addEventListener('click', _closeBlEditor);
  const search = document.getElementById('bl-search');
  if (search) search.addEventListener('input', () => _renderPlaybookList(_filterPlaybooks()));

  // Sub-tab switching (Playbooks / Automation)
  document.querySelectorAll('#page-playbooks .sub-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#page-playbooks .sub-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('#page-playbooks .sub-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.subtab).classList.add('active');
      if (tab.dataset.subtab === 'auto-panel' && typeof loadAutomations === 'function') loadAutomations();
    });
  });
});

if (typeof navigateTo === 'function') {
  const _origNavPlaybooks = navigateTo;
  navigateTo = function (p) { _origNavPlaybooks(p); if (p === 'playbooks') loadPlaybooks(); };
}


/* Turning auto-enrol on is the one action here that reaches every machine at
   once, without anyone watching, so it says out loud what it is about to do
   and how many hosts that is. */
async function _confirmAutoEnroll(data) {
  const tags = (data.tags || '').trim();
  const target = tags
    ? `every host tagged <strong>${escHtml(tags)}</strong>`
    : '<strong>EVERY approved host</strong>';
  if (!(data.done || '').trim()) {
    showToast('Set a completion tag first — without one the playbook would '
              + 'run again on every pass.', 'error');
    return false;
  }
  let count = '';
  try {
    const hosts = await apiJson('/api/v1/hosts/');
    const wanted = tags.split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
    const done = (data.done || '').trim().toLowerCase();
    const matching = (hosts.results || hosts || []).filter((h) => {
      const ht = (h.tags || []).map(t => String(t).toLowerCase());
      if (ht.includes(done)) return false;
      return wanted.length ? wanted.some(t => ht.includes(t)) : true;
    });
    count = `<p><strong>${matching.length}</strong> host${matching.length === 1 ? '' : 's'} `
          + `match right now and would start running it within five minutes.</p>`;
  } catch (e) { /* the warning stands without a count */ }

  return confirmModal(
    `<p><strong>${escHtml(data.name)}</strong> will run unattended on ${target}.</p>`
    + count
    + `<p>Each host is tagged <code>${escHtml(data.done)}</code> when it finishes, `
    + `and is not run again while it carries that tag.</p>`
    + `<p class="muted-note">Rollouts are the staged alternative: they cover the `
    + `same machines a wave at a time and stop at the first wave that fails.</p>`,
    { title: 'Run this on every matching host?',
      confirmText: 'Yes, turn auto-enroll on', danger: true, html: true });
}
