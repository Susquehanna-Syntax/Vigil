// vigil-baselines.js
// Owns: the Baselines page — a sequence editor for baselines (ordered task
// definitions that auto-dispatch on host enrollment and are callable from any
// task via `type: baseline`).
// Depends on: vigil-utils.js.

let _baselineDefs = [];   // all task definitions, for the picker
let _editingSteps = [];   // definition ids currently in the editor, in order

async function loadBaselines() {
  const list = document.getElementById('baselines-list');
  if (!list) return;
  try {
    const [baselines, defs] = await Promise.all([
      apiJson('/api/v1/baselines/'),
      apiJson('/api/v1/tasks/definitions/'),
    ]);
    _baselineDefs = Array.isArray(defs) ? defs : (defs.results || []);
    _renderBaselineList(baselines);
    _renderDefPicker();
  } catch (e) {
    list.innerHTML = `<div class="empty-state"><div class="empty-state-title">Couldn't load baselines</div><div class="empty-state-desc">${escHtml(e.message)}</div></div>`;
  }
}

function _renderBaselineList(baselines) {
  const list = document.getElementById('baselines-list');
  if (!baselines.length) {
    list.innerHTML = `<div class="empty-state">
      <div class="empty-state-title">No baselines yet</div>
      <div class="empty-state-desc">A baseline is a named sequence of tasks that runs automatically when a matching host is approved — and can be called from any task with <code>type: baseline</code>.</div></div>`;
    return;
  }
  list.innerHTML = baselines.map(b => {
    const steps = b.steps.map((s, i) =>
      `<span class="bl-step">${i + 1}. ${escHtml(s.definition_name)} <span class="risk-pill risk-${escHtml(s.risk)}">${escHtml(s.risk)}</span></span>`).join('');
    const tags = (b.target_tags || []).map(t => `<span class="tag-chip">${escHtml(t)}</span>`).join('') ||
      '<span class="bl-muted">every approved host</span>';
    return `<div class="bl-card">
      <div class="bl-card-head">
        <div>
          <span class="bl-name">${escHtml(b.name)}</span>
          <span class="bl-badge ${b.enabled ? 'on' : 'off'}">${b.enabled ? 'auto-enroll on' : 'auto-enroll off'}</span>
        </div>
        <div class="bl-card-actions">
          <button class="btn btn-sm btn-outline" data-bl-toggle="${b.id}" data-enabled="${b.enabled}">${b.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-sm btn-outline" data-bl-edit="${b.id}">Edit</button>
          <button class="btn btn-sm btn-outline" style="color:var(--rose);" data-bl-del="${b.id}">Delete</button>
        </div>
      </div>
      <div class="bl-steps">${steps}</div>
      <div class="bl-tags">Runs on: ${tags}</div>
      <div class="bl-call">Call from a task: <code>- type: baseline\n    params: { name: "${escHtml(b.name)}" }</code></div>
    </div>`;
  }).join('');

  list.querySelectorAll('[data-bl-del]').forEach(btn => btn.addEventListener('click', async () => {
    if (!confirm('Delete this baseline?')) return;
    await fetch(`/api/v1/baselines/${btn.dataset.blDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
    loadBaselines();
  }));
  list.querySelectorAll('[data-bl-toggle]').forEach(btn => btn.addEventListener('click', async () => {
    await apiJson(`/api/v1/baselines/${btn.dataset.blToggle}/`, { method: 'PATCH', body: JSON.stringify({ enabled: btn.dataset.enabled !== 'true' }) });
    loadBaselines();
  }));
  list.querySelectorAll('[data-bl-edit]').forEach(btn => btn.addEventListener('click', () => _startEdit(btn.dataset.blEdit)));
}

/* ── Editor ──────────────────────────────────────────────────────────── */
function _renderDefPicker() {
  const sel = document.getElementById('bl-def-picker');
  if (!sel) return;
  sel.innerHTML = '<option value="">Add a task…</option>' +
    _baselineDefs.map(d => `<option value="${d.id}">${escHtml(d.name)} (${escHtml(d.risk_level || d.risk || 'standard')})</option>`).join('');
}

function _renderEditorSteps() {
  const wrap = document.getElementById('bl-editor-steps');
  if (!_editingSteps.length) {
    wrap.innerHTML = '<div class="bl-muted">No steps yet — add task definitions in the order they should run.</div>';
    return;
  }
  wrap.innerHTML = _editingSteps.map((id, i) => {
    const def = _baselineDefs.find(d => d.id === id);
    const name = def ? def.name : id;
    return `<div class="bl-editor-step">
      <span>${i + 1}. ${escHtml(name)}</span>
      <span class="bl-editor-step-btns">
        <button class="btn btn-xs btn-outline" data-mv="${i}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn btn-xs btn-outline" data-mv="${i}" data-dir="1" ${i === _editingSteps.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn btn-xs btn-outline" style="color:var(--rose);" data-rm="${i}">✕</button>
      </span></div>`;
  }).join('');
  wrap.querySelectorAll('[data-rm]').forEach(b => b.addEventListener('click', () => { _editingSteps.splice(+b.dataset.rm, 1); _renderEditorSteps(); }));
  wrap.querySelectorAll('[data-mv]').forEach(b => b.addEventListener('click', () => {
    const i = +b.dataset.mv, j = i + (+b.dataset.dir);
    if (j < 0 || j >= _editingSteps.length) return;
    [_editingSteps[i], _editingSteps[j]] = [_editingSteps[j], _editingSteps[i]];
    _renderEditorSteps();
  }));
}

function _startEdit(id) {
  apiJson(`/api/v1/baselines/${id}/`).then(b => {
    document.getElementById('bl-editor').dataset.editing = id;
    document.getElementById('bl-editor-title').textContent = 'Edit baseline';
    document.getElementById('bl-name').value = b.name;
    document.getElementById('bl-desc').value = b.description || '';
    document.getElementById('bl-tags').value = (b.target_tags || []).join(', ');
    document.getElementById('bl-enabled').checked = b.enabled;
    _editingSteps = b.steps.map(s => s.definition_id);
    _renderEditorSteps();
    document.getElementById('bl-editor').scrollIntoView({ behavior: 'smooth' });
  });
}

function _resetEditor() {
  const ed = document.getElementById('bl-editor');
  delete ed.dataset.editing;
  document.getElementById('bl-editor-title').textContent = 'New baseline';
  document.getElementById('bl-name').value = '';
  document.getElementById('bl-desc').value = '';
  document.getElementById('bl-tags').value = '';
  document.getElementById('bl-enabled').checked = true;
  _editingSteps = [];
  _renderEditorSteps();
}

async function _saveBaseline() {
  const body = {
    name: document.getElementById('bl-name').value.trim(),
    description: document.getElementById('bl-desc').value.trim(),
    target_tags: document.getElementById('bl-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    enabled: document.getElementById('bl-enabled').checked,
    definition_ids: _editingSteps,
  };
  if (!body.name) return showToast('Name the baseline', 'error');
  if (!body.definition_ids.length) return showToast('Add at least one task', 'error');
  const editing = document.getElementById('bl-editor').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/baselines/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/baselines/', { method: 'POST', body: JSON.stringify(body) });
    showToast('Baseline saved', 'success');
    _resetEditor();
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
  const reset = document.getElementById('bl-new-btn');
  if (reset) reset.addEventListener('click', _resetEditor);
});

if (typeof navigateTo === 'function') {
  const _origNavBaselines = navigateTo;
  navigateTo = function (p) { _origNavBaselines(p); if (p === 'baselines') loadBaselines(); };
}
