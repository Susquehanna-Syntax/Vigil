// vigil-baselines.js
// Owns: the Baselines page — a sequence editor for baselines (ordered task
// definitions that auto-dispatch on host enrollment and are callable from any
// task via `type: baseline`). Rich cards show each step and its action count;
// the editor builds a sequence you can reorder.
// Depends on: vigil-utils.js (apiJson, confirmModal, showToast, escHtml).

let _baselineDefs = [];   // all task definitions, for the picker (id -> def)
let _editingSteps = [];   // definition ids currently in the editor, in order
let _allBaselines = [];   // cached, for client-side search

async function loadBaselines() {
  const list = document.getElementById('baselines-list');
  if (!list) return;
  list.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    const [baselines, defs] = await Promise.all([
      apiJson('/api/v1/baselines/'),
      apiJson('/api/v1/tasks/definitions/'),
    ]);
    _baselineDefs = Array.isArray(defs) ? defs : (defs.results || []);
    _allBaselines = baselines;
    _renderDefPicker();
    _renderBaselineList(_filterBaselines());
  } catch (e) {
    list.innerHTML = `<div class="empty-block"><h4>Couldn't load baselines</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

function _filterBaselines() {
  const q = (document.getElementById('bl-search')?.value || '').trim().toLowerCase();
  if (!q) return _allBaselines;
  return _allBaselines.filter(b =>
    b.name.toLowerCase().includes(q) ||
    (b.description || '').toLowerCase().includes(q) ||
    (b.target_tags || []).some(t => t.toLowerCase().includes(q)) ||
    b.steps.some(s => s.definition_name.toLowerCase().includes(q)));
}

function _defName(id) {
  const d = _baselineDefs.find(x => String(x.id) === String(id));
  return d ? d.name : id;
}
function _defRisk(id) {
  const d = _baselineDefs.find(x => String(x.id) === String(id));
  return d ? (d.risk_level || d.risk || 'standard') : 'standard';
}
function _defActionCount(id) {
  const d = _baselineDefs.find(x => String(x.id) === String(id));
  const acts = d && d.parsed_spec && d.parsed_spec.actions;
  return Array.isArray(acts) ? acts.length : null;
}

function _renderBaselineList(baselines) {
  const list = document.getElementById('baselines-list');
  if (!baselines.length) {
    list.innerHTML = `<div class="empty-block">
      <h4>No baselines yet</h4>
      <p>A baseline is a named sequence of tasks that runs automatically when a matching host is approved — and can be called from any task with <code class="inline">type: baseline</code>. Create one to standardise how new machines get set up.</p></div>`;
    return;
  }
  list.innerHTML = baselines.map(b => {
    const steps = b.steps.map(s => `
      <div class="bl-seq-step">
        <span class="bl-seq-num">${s.order + 1}</span>
        <span style="flex:1;">${escHtml(s.definition_name)}</span>
        <span class="risk-badge risk-${escHtml(s.risk)}">${escHtml(s.risk)}</span>
      </div>`).join('');
    const tags = (b.target_tags || []).length
      ? (b.target_tags || []).map(t => `<span class="chip">${escHtml(t)}</span>`).join(' ')
      : '<span class="muted-note">every approved host</span>';
    return `<div class="bl-card">
      <div class="bl-card-head">
        <div>
          <span class="bl-name">${escHtml(b.name)}</span>
          <span class="bl-badge ${b.enabled ? 'on' : 'off'}">${b.enabled ? 'auto-enroll on' : 'auto-enroll off'}</span>
          ${b.description ? `<div class="muted-note" style="margin-top:4px;">${escHtml(b.description)}</div>` : ''}
        </div>
        <div class="card-actions">
          <button class="btn btn-outline btn-xs" data-bl-toggle="${b.id}" data-enabled="${b.enabled}">${b.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-outline btn-xs" data-bl-dup="${b.id}">Duplicate</button>
          <button class="btn btn-outline btn-xs" data-bl-edit="${b.id}">Edit</button>
          <button class="btn btn-outline btn-xs" style="color:var(--rose);" data-bl-del="${b.id}">Delete</button>
        </div>
      </div>
      <div class="bl-seq">${steps}</div>
      <div class="bl-meta">
        <span><b>${b.steps.length}</b> step${b.steps.length === 1 ? '' : 's'}</span>
        <span>Runs on: ${tags}</span>
      </div>
      <div class="bl-call">
        <div class="bl-call-label">Call from a task:</div>
        <pre>${yamlToHtml('- type: baseline\n  params: { name: "' + b.name + '" }')}</pre>
      </div>
    </div>`;
  }).join('');
  _wireCards(baselines);
}

function _wireCards(baselines) {
  const list = document.getElementById('baselines-list');
  list.querySelectorAll('[data-bl-del]').forEach(btn => btn.addEventListener('click', async () => {
    if (!(await confirmModal('Delete this baseline? Tasks that call it by name will start failing.', { danger: true, confirmText: 'Delete' }))) return;
    await fetch(`/api/v1/baselines/${btn.dataset.blDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadBaselines();
  }));
  list.querySelectorAll('[data-bl-toggle]').forEach(btn => btn.addEventListener('click', async () => {
    await apiJson(`/api/v1/baselines/${btn.dataset.blToggle}/`, { method: 'PATCH', body: JSON.stringify({ enabled: btn.dataset.enabled !== 'true' }) });
    loadBaselines();
  }));
  list.querySelectorAll('[data-bl-edit]').forEach(btn => btn.addEventListener('click', () => _startEdit(btn.dataset.blEdit)));
  list.querySelectorAll('[data-bl-dup]').forEach(btn => btn.addEventListener('click', () => {
    const b = baselines.find(x => x.id === btn.dataset.blDup);
    _openEditor({ name: b.name + ' (copy)', description: b.description,
      target_tags: b.target_tags, enabled: b.enabled,
      steps: b.steps.map(s => ({ definition_id: s.definition_id })) }, null);
  }));
}

/* ── Editor ──────────────────────────────────────────────────────────── */
function _renderDefPicker() {
  const sel = document.getElementById('bl-def-picker');
  if (!sel) return;
  sel.innerHTML = '<option value="">+ Add a task to the sequence…</option>' +
    _baselineDefs.map(d => `<option value="${d.id}">${escHtml(d.name)} · ${escHtml(d.risk_level || d.risk || 'standard')}</option>`).join('');
}

function _renderEditorSteps() {
  const wrap = document.getElementById('bl-editor-steps');
  if (!_editingSteps.length) {
    wrap.innerHTML = '<div class="muted-note">No steps yet — add task definitions below in the order they should run.</div>';
    return;
  }
  wrap.innerHTML = _editingSteps.map((id, i) => {
    const count = _defActionCount(id);
    return `<div class="bl-editor-step">
      <div class="bl-editor-step-main">
        <span class="bl-editor-step-num">${i + 1}</span>
        <div>
          <div>${escHtml(_defName(id))}</div>
          <div class="bl-hint">${count != null ? count + ' action' + (count === 1 ? '' : 's') : ''} · <span class="risk-badge risk-${escHtml(_defRisk(id))}" style="padding:1px 8px;">${escHtml(_defRisk(id))}</span></div>
        </div>
      </div>
      <span class="bl-editor-step-btns">
        <button class="btn btn-outline btn-xs" data-view="${escHtml(String(id))}" title="View / edit this task">View / edit</button>
        <button class="btn btn-outline btn-xs" data-mv="${i}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn btn-outline btn-xs" data-mv="${i}" data-dir="1" ${i === _editingSteps.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn btn-outline btn-xs" style="color:var(--rose);" data-rm="${i}">Remove</button>
      </span></div>`;
  }).join('');
  wrap.querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => { _editingSteps.splice(+b.dataset.rm, 1); _renderEditorSteps(); }));
  wrap.querySelectorAll('[data-view]').forEach(b => b.addEventListener('click', () => {
    // Open the underlying task definition in the task editor to view/edit it.
    _closeBlEditor();
    if (typeof openDefinitionEditor === 'function') openDefinitionEditor(b.dataset.view);
  }));
  wrap.querySelectorAll('[data-mv]').forEach(b => b.addEventListener('click', () => {
    const i = +b.dataset.mv, j = i + (+b.dataset.dir);
    if (j < 0 || j >= _editingSteps.length) return;
    [_editingSteps[i], _editingSteps[j]] = [_editingSteps[j], _editingSteps[i]];
    _renderEditorSteps();
  }));
}

function _openEditor(data, editingId) {
  const modal = document.getElementById('bl-editor-modal');
  modal.dataset.editing = editingId || '';
  document.getElementById('bl-editor-title').textContent = editingId ? 'Edit baseline' : (data && data.name ? 'Duplicate baseline' : 'New baseline');
  document.getElementById('bl-name').value = data ? (data.name || '') : '';
  document.getElementById('bl-desc').value = data ? (data.description || '') : '';
  document.getElementById('bl-tags').value = data ? (data.target_tags || []).join(', ') : '';
  document.getElementById('bl-enabled').checked = data ? !!data.enabled : true;
  _editingSteps = data && data.steps ? data.steps.map(s => String(s.definition_id)) : [];
  _renderEditorSteps();
  document.getElementById('bl-editor-overlay').classList.add('open');
  modal.classList.add('open');
}

function _closeBlEditor() {
  document.getElementById('bl-editor-overlay').classList.remove('open');
  document.getElementById('bl-editor-modal').classList.remove('open');
}

function _startEdit(id) {
  apiJson(`/api/v1/baselines/${id}/`).then(b => _openEditor(b, id));
}

async function _saveBaseline() {
  const body = {
    name: document.getElementById('bl-name').value.trim(),
    description: document.getElementById('bl-desc').value.trim(),
    target_tags: document.getElementById('bl-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    enabled: document.getElementById('bl-enabled').checked,
    definition_ids: _editingSteps,
  };
  if (!body.name) return showToast('Give the baseline a name', 'error');
  if (!body.definition_ids.length) return showToast('Add at least one task', 'error');
  const editing = document.getElementById('bl-editor-modal').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/baselines/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/baselines/', { method: 'POST', body: JSON.stringify(body) });
    showToast('Baseline saved', 'success');
    _closeBlEditor();
    loadBaselines();
  } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  const picker = document.getElementById('bl-def-picker');
  if (picker) picker.addEventListener('change', () => {
    if (picker.value) { _editingSteps.push(picker.value); picker.value = ''; _renderEditorSteps(); }
  });
  const save = document.getElementById('bl-save-btn');
  if (save) save.addEventListener('click', _saveBaseline);
  const nu = document.getElementById('bl-new-btn');
  if (nu) nu.addEventListener('click', () => _openEditor(null, null));
  document.getElementById('bl-cancel-btn')?.addEventListener('click', _closeBlEditor);
  document.getElementById('bl-cancel-btn-2')?.addEventListener('click', _closeBlEditor);
  document.getElementById('bl-editor-overlay')?.addEventListener('click', _closeBlEditor);
  // Create a brand-new task definition, then it appears in the picker to add.
  document.getElementById('bl-new-task-btn')?.addEventListener('click', () => {
    _closeBlEditor();
    if (typeof openDefinitionEditor === 'function') openDefinitionEditor(null);
  });
  const search = document.getElementById('bl-search');
  if (search) search.addEventListener('input', () => _renderBaselineList(_filterBaselines()));

  // Sub-tab switching (Baselines / Automation)
  document.querySelectorAll('#page-baselines .sub-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#page-baselines .sub-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('#page-baselines .sub-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.subtab).classList.add('active');
      if (tab.dataset.subtab === 'auto-panel' && typeof loadAutomations === 'function') loadAutomations();
    });
  });
});

if (typeof navigateTo === 'function') {
  const _origNavBaselines = navigateTo;
  navigateTo = function (p) { _origNavBaselines(p); if (p === 'baselines') loadBaselines(); };
}
