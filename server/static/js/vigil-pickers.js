// vigil-pickers.js
// Owns: two reusable stacked modals used from the baseline/automation editors
// (and the AI "Use this" flow):
//   openPicker({type, onSelect})  — a searchable list of tasks / baselines /
//                                    machines with Add-new and per-item Edit,
//                                    stacking on top of whatever opened it.
//   openTaskModal({id?, yaml?, onSaved?}) — a lightweight task-definition
//                                    editor (name + YAML + live preview) that
//                                    never navigates away, so editing a task
//                                    from a picker keeps the modal you were in.
// Depends on: vigil-utils.js (mountModal, apiJson, escHtml, showToast,
//             confirmModal, yamlToHtml).

/* ═══ Picker modal ═══════════════════════════════════════════════════════ */
let _pickerState = null;

async function openPicker(opts) {
  // opts: { type: 'task'|'baseline'|'machine', title, onSelect(item), allowAdd }
  _pickerState = opts;
  const m = mountModal('picker', { wide: true });
  m.modal.querySelector('#picker-close') || m.setBody(`
    <div class="modal-title">
      <span id="picker-title"></span>
      <button class="modal-close" id="picker-close" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="picker-toolbar">
      <input type="text" class="form-control" id="picker-search" placeholder="Search…">
      <button class="btn btn-mint btn-sm" id="picker-add" type="button">Add new</button>
    </div>
    <div class="picker-list" id="picker-list"></div>`);
  m.modal.querySelector('#picker-close').onclick = m.close;
  m.modal.querySelector('#picker-title').textContent =
    opts.title || ('Pick a ' + opts.type);
  const addBtn = m.modal.querySelector('#picker-add');
  addBtn.style.display = opts.allowAdd === false ? 'none' : '';
  addBtn.textContent = opts.type === 'baseline' ? 'New baseline'
    : opts.type === 'task' ? 'New task' : 'Add';
  addBtn.onclick = () => _pickerAdd(opts.type);
  const search = m.modal.querySelector('#picker-search');
  search.value = '';
  search.oninput = () => _renderPickerList(search.value);
  m.open();
  await _loadPickerData(opts.type);
  _renderPickerList('');
  search.focus();
}

function closePicker() { const o = document.getElementById('picker-overlay'); if (o) o.classList.remove('open'); document.getElementById('picker-modal')?.classList.remove('open'); }

let _pickerItems = [];
async function _loadPickerData(type) {
  const list = document.getElementById('picker-list');
  list.innerHTML = '<div class="picker-empty">Loading…</div>';
  try {
    if (type === 'task') {
      const defs = await apiJson('/api/v1/tasks/definitions/');
      _pickerItems = (Array.isArray(defs) ? defs : defs.results || []).map(d => ({
        key: d.id, name: d.name, meta: (d.risk_level || 'standard') + ' · ' +
          ((d.parsed_spec && d.parsed_spec.actions ? d.parsed_spec.actions.length : d.action_count || 0) + ' action(s)'),
        risk: d.risk_level, editable: true, raw: d }));
    } else if (type === 'baseline') {
      const bl = await apiJson('/api/v1/baselines/');
      _pickerItems = bl.map(b => ({ key: b.name, name: b.name,
        meta: b.steps.length + ' step(s)' + (b.enabled ? '' : ' · auto-enroll off'),
        editable: true, raw: b }));
    } else {
      const hosts = await apiJson('/api/v1/status-pages/hosts/');
      _pickerItems = hosts.map(h => ({ key: h.id, name: h.hostname,
        meta: h.up ? 'online' : 'offline', editable: false, raw: h }));
    }
  } catch (e) { list.innerHTML = `<div class="picker-empty">${escHtml(e.message)}</div>`; }
}

function _renderPickerList(q) {
  const list = document.getElementById('picker-list');
  if (!list) return;
  q = (q || '').trim().toLowerCase();
  const items = q ? _pickerItems.filter(i => i.name.toLowerCase().includes(q) || (i.meta || '').toLowerCase().includes(q)) : _pickerItems;
  if (!items.length) { list.innerHTML = `<div class="picker-empty">${_pickerItems.length ? 'No matches.' : 'Nothing here yet — use “Add new”.'}</div>`; return; }
  list.innerHTML = items.map((i, idx) => `
    <div class="picker-row" data-key="${escHtml(String(i.key))}">
      <div class="picker-row-main">
        <span class="picker-row-name">${escHtml(i.name)}${i.risk ? ` <span class="risk-badge risk-${escHtml(i.risk)}">${escHtml(i.risk)}</span>` : ''}</span>
        <span class="picker-row-meta">${escHtml(i.meta || '')}</span>
      </div>
      <div class="picker-row-actions">
        ${i.editable ? `<button class="btn btn-outline btn-xs" data-pick-edit="${idx}">Edit</button>` : ''}
        <button class="btn btn-mint btn-xs" data-pick-sel="${idx}">Select</button>
      </div>
    </div>`).join('');
  list.querySelectorAll('[data-pick-sel]').forEach(b => b.addEventListener('click', () => {
    const item = items[+b.dataset.pickSel];
    closePicker();
    _pickerState.onSelect(item);
  }));
  list.querySelectorAll('[data-pick-edit]').forEach(b => b.addEventListener('click', () => {
    const item = items[+b.dataset.pickEdit];
    if (_pickerState.type === 'task') {
      openTaskModal({ id: item.key, onSaved: () => { _loadPickerData('task').then(() => _renderPickerList(document.getElementById('picker-search').value)); } });
    } else if (_pickerState.type === 'baseline') {
      closePicker();
      // Reuse the baselines editor modal for the baseline.
      if (typeof _startEdit === 'function') _startEdit(item.raw.id);
    }
  }));
}

function _pickerAdd(type) {
  if (type === 'task') {
    openTaskModal({ onSaved: (def) => {
      _loadPickerData('task').then(() => _renderPickerList(''));
      showToast('Task created', 'success');
    } });
  } else if (type === 'baseline') {
    closePicker();
    if (typeof _openEditor === 'function') _openEditor(null, null);   // baselines editor
  }
}

/* ═══ In-modal task editor ═══════════════════════════════════════════════ */
let _taskModalSaved = null, _taskModalId = null, _taskValidateTimer = null;

function openTaskModal(opts) {
  opts = opts || {};
  _taskModalSaved = opts.onSaved || null;
  _taskModalId = opts.id || null;
  const m = mountModal('taskedit', { wide: true });
  m.setBody(`
    <div class="modal-title">
      <span id="te-title">${opts.id ? 'Edit task' : 'New task'}</span>
      <button class="modal-close" id="te-close" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="te-grid">
      <div>
        <label class="form-label">Task YAML</label>
        <textarea class="form-control te-yaml" id="te-yaml" spellcheck="false"
          placeholder="name: My task&#10;risk: standard&#10;actions:&#10;  - type: run_command&#10;    params:&#10;      command: uptime"></textarea>
      </div>
      <div>
        <label class="form-label">Preview</label>
        <div class="te-preview" id="te-preview"><div class="muted-note">Start typing to see steps.</div></div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="te-cancel" type="button">Cancel</button>
      <button class="btn btn-mint" id="te-save">Save task</button>
    </div>`);
  m.modal.querySelector('#te-close').onclick = m.close;
  m.modal.querySelector('#te-cancel').onclick = m.close;
  m.overlay.onclick = m.close;
  const ta = m.modal.querySelector('#te-yaml');
  ta.addEventListener('input', () => {
    clearTimeout(_taskValidateTimer);
    _taskValidateTimer = setTimeout(() => _teValidate(ta.value), 400);
  });
  m.modal.querySelector('#te-save').onclick = () => _teSave(ta.value, m.close);
  m.open();

  if (opts.id) {
    apiJson(`/api/v1/tasks/definitions/${opts.id}/`).then(d => { ta.value = d.yaml_source || ''; _teValidate(ta.value); });
  } else if (opts.yaml) {
    ta.value = opts.yaml; _teValidate(opts.yaml);
  }
  setTimeout(() => ta.focus(), 50);
}

async function _teValidate(yaml) {
  const prev = document.getElementById('te-preview');
  if (!prev) return;
  if (!yaml.trim()) { prev.innerHTML = '<div class="muted-note">Start typing to see steps.</div>'; return; }
  try {
    const r = await apiJson('/api/v1/tasks/definitions/validate/', { method: 'POST', body: JSON.stringify({ yaml_source: yaml }) });
    const spec = r.parsed_spec || {};
    const risk = spec.derived_risk || spec.risk || 'standard';
    const steps = (spec.actions || []).map((a, i) => `
      <div class="preview-step task-${escHtml(risk)}">
        <div class="preview-step-num">${i + 1}</div>
        <div class="preview-step-body">
          <div class="preview-step-title">${escHtml(a.id || ('step' + (i + 1)))} — ${escHtml(a.label || a.type)}</div>
          <div class="preview-step-action">${escHtml(a.type)}${Object.keys(a.params || {}).length ? ' · ' + Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join(' ') : ''}</div>
        </div>
      </div>`).join('');
    prev.innerHTML = `<div class="te-preview-name">${escHtml(spec.name || 'Untitled')} <span class="risk-badge risk-${escHtml(risk)}">${escHtml(risk)}</span></div>${steps || '<div class="muted-note">No steps.</div>'}`;
  } catch (e) {
    prev.innerHTML = `<div class="ai-err">${escHtml(e.message)}</div>`;
  }
}

async function _teSave(yaml, close) {
  if (!yaml.trim()) return showToast('Write some task YAML', 'error');
  try {
    let def;
    if (_taskModalId) def = await apiJson(`/api/v1/tasks/definitions/${_taskModalId}/`, { method: 'PUT', body: JSON.stringify({ yaml_source: yaml }) });
    else def = await apiJson('/api/v1/tasks/definitions/', { method: 'POST', body: JSON.stringify({ yaml_source: yaml }) });
    showToast('Task saved', 'success');
    close();
    if (_taskModalSaved) _taskModalSaved(def);
  } catch (e) { showToast(e.message, 'error'); }
}
