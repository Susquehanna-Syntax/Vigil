// vigil-tasks.js
// Owns: Tasks page — library grid, community grid, dispatch modal,
//   task definition editor (YAML + preview), publish/unpublish/fork.
// HTML: templates/pages/_tasks.html, templates/pages/_community.html,
//   templates/pages/_task_editor.html, dispatch modal in templates/base.html.
// Depends on: vigil-utils.js (apiJson, showToast, escHtml),
//   vigil-deploy.js (openDeployModal triggered from def cards;
//   publishDefinition / unpublishDefinition live in deploy.js as they
//   share its task-definition API surface).
// API: /api/v1/tasks/, /api/v1/tasks/definitions/{,validate/,fork/,...}

/* ── Task dispatch modal (simple per-host quick action) ──────────────── */
const ACTION_PARAMS = {
  restart_service:    [{ name: 'service_name',   label: 'Service Name',   type: 'text',     placeholder: 'e.g. nginx, postgresql, sshd' }],
  restart_container:  [{ name: 'container_name', label: 'Container Name', type: 'text',     placeholder: 'e.g. my-app' }],
  stop_container:     [{ name: 'container_name', label: 'Container Name', type: 'text',     placeholder: 'e.g. my-app' }],
  start_container:    [{ name: 'container_name', label: 'Container Name', type: 'text',     placeholder: 'e.g. my-app' }],
  clear_temp_files:   [],
  clear_docker_logs:  [],
  run_package_updates: [],
  execute_script:     [{ name: 'script_content', label: 'Script Content', type: 'textarea', placeholder: '#!/bin/bash\n# Your script here' }],
  reboot:             [],
};

const ACTION_RISK = {
  start_container: 'low', clear_temp_files: 'low', clear_docker_logs: 'low',
  restart_service: 'standard', restart_container: 'standard', stop_container: 'standard', run_package_updates: 'standard',
  execute_script: 'high', reboot: 'high',
};

function openDispatchModal() {
  document.getElementById('dispatch-form').reset();
  document.getElementById('dispatch-params').innerHTML = '';
  document.getElementById('dispatch-risk-label').innerHTML = '';
  document.getElementById('dispatch-overlay').classList.add('open');
  document.getElementById('dispatch-modal').classList.add('open');
}

function closeDispatchModal() {
  document.getElementById('dispatch-overlay').classList.remove('open');
  document.getElementById('dispatch-modal').classList.remove('open');
}

function updateDispatchParams() {
  const action = document.getElementById('dispatch-action').value;
  const paramsContainer = document.getElementById('dispatch-params');
  const riskEl = document.getElementById('dispatch-risk-label');

  if (!action) { paramsContainer.innerHTML = ''; riskEl.innerHTML = ''; return; }

  const paramDefs = ACTION_PARAMS[action] || [];
  const risk = ACTION_RISK[action] || 'standard';

  paramsContainer.innerHTML = paramDefs.map(p => {
    const inputHtml = p.type === 'textarea'
      ? `<textarea class="form-control" id="param-${p.name}" name="${p.name}" placeholder="${escHtml(p.placeholder || '')}" required></textarea>`
      : `<input class="form-control" id="param-${p.name}" name="${p.name}" type="text" placeholder="${escHtml(p.placeholder || '')}" required>`;
    return `<div class="form-group"><label class="form-label">${escHtml(p.label)}</label>${inputHtml}</div>`;
  }).join('');

  const riskLabels = { low: 'Low risk', standard: 'Standard risk', high: 'High risk' };
  riskEl.innerHTML = `<span class="risk-badge risk-${risk}">${riskLabels[risk]}</span>`;
}

async function submitDispatch(event) {
  event.preventDefault();
  const hostId = document.getElementById('dispatch-host').value;
  const action = document.getElementById('dispatch-action').value;
  if (!hostId || !action) return;

  const params = {};
  (ACTION_PARAMS[action] || []).forEach(p => {
    const el = document.getElementById('param-' + p.name);
    if (el) params[p.name] = el.value;
  });

  const btn = document.getElementById('dispatch-submit-btn');
  btn.disabled = true; btn.style.opacity = '0.6';

  try {
    const resp = await fetch('/api/v1/tasks/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
      body: JSON.stringify({ host: hostId, action, params }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || 'Dispatch failed');
    }
    showToast('Task dispatched — agent picks it up on next check-in', 'success');
    closeDispatchModal();
    setTimeout(() => location.reload(), 1400);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDispatchModal();
});

/* ── Editor: default YAML + canned templates ─────────────────────────── */
const DEFAULT_YAML_TEMPLATE = `name: New Task
description: What this task does and when to run it.
relevance: "e.g. web servers, database hosts"
risk: standard

# Optional: restrict when tasks are dispatched (server timezone).
# schedule:
#   window:
#     start_hour: 8      # 0-23
#     start_minute: 0    # 0-59 (default 0)
#     end_hour: 17       # inclusive through end_hour:59
#     end_minute: 0      # 0-59 (default 0)
#     days: [mon, tue, wed, thu, fri]

# Optional: retry on failure
# on_failure:
#   retry:
#     attempts: 3
#     delay_seconds: 60

# Optional: require specific output (supports {{ inputs.x }} variables)
# success_criteria:
#   exit_code: 0
#   output_contains: "restarted"
#   output_regex: "^OK"

actions:
  - id: step1
    type: restart_service
    params:
      service_name: nginx
`;

/* Pre-canned task templates exposed in the editor's "Start from template…"
   dropdown. Each entry's YAML is a complete, valid TaskDefinition. */
const EDITOR_TEMPLATES = [
  {
    id: 'update-package',
    label: 'Update a package',
    yaml: `name: Update package
description: Refresh the package index and upgrade a single package to its latest version.
risk: standard

inputs:
  - id: pkg
    label: Package name
    type: text
    default: hashcat
    required: true

actions:
  - id: refresh-index
    type: run_package_updates
  - id: upgrade
    type: update_package
    params:
      package_name: "{{ inputs.pkg }}"
`
  },
  {
    id: 'reinstall-package',
    label: 'Reinstall a package from scratch',
    yaml: `name: Reinstall package from scratch
description: Remove the package and any leftover config, then install it fresh.
risk: standard

inputs:
  - id: pkg
    label: Package name
    type: text
    default: hashcat
    required: true

actions:
  - id: remove
    type: remove_package
    params:
      package_name: "{{ inputs.pkg }}"
  - id: refresh-index
    type: run_package_updates
  - id: install
    type: install_package
    params:
      package_name: "{{ inputs.pkg }}"
`
  },
  {
    id: 'update-or-reinstall',
    label: 'Update OR reinstall (user picks at deploy time)',
    yaml: `name: Update or reinstall a package
description: |
  Lets the operator pick whether to do a fast in-place update or a clean
  remove + reinstall when deploying.
risk: standard

inputs:
  - id: pkg
    label: Package name
    type: text
    default: hashcat
    required: true
  - id: mode
    label: How should we handle it
    type: choice
    choices:
      - { value: update,    label: "Update in place" }
      - { value: reinstall, label: "Reinstall from scratch" }
    default: update

actions:
  # NOTE: Only one of these branches will produce a meaningful change for
  # any given run, but both are always executed. The agent's package
  # manager treats a no-op install as a fast no-op.
  - id: refresh-index
    type: run_package_updates
  - id: upgrade-only
    type: update_package
    params:
      package_name: "{{ inputs.pkg }}"
`
  },
  {
    id: 'restart-service',
    label: 'Restart a systemd service',
    yaml: `name: Restart service
description: Bounce a single systemd service.
risk: standard

inputs:
  - id: service
    label: Service name
    type: text
    default: nginx
    required: true

actions:
  - id: restart
    type: restart_service
    params:
      service_name: "{{ inputs.service }}"
`
  },
  {
    id: 'clear-tmp',
    label: 'Clear /tmp older than N days',
    yaml: `name: Clear /tmp
description: Delete files in /tmp older than the chosen age.
risk: low

inputs:
  - id: days
    label: Older than (days)
    type: number
    default: 7

actions:
  - id: clean
    type: clear_temp_files
    params:
      older_than_days: "{{ inputs.days }}"
`
  },
  {
    id: 'wait-then-act',
    label: 'Wait until user offline, then act (full_control)',
    yaml: `name: Wait until user offline, then restart
description: |
  Polls 'who' until no users are logged in, then restarts the named service.
  Requires the agent to be in full_control mode because run_command is used.
relevance: "interactive workstations where you don't want to disrupt active users"
risk: high

inputs:
  - id: service
    label: Service to restart once nobody is logged in
    type: text
    default: nginx
    required: true
  - id: max_wait_minutes
    label: Maximum minutes to wait for sessions to clear
    type: number
    default: 60

actions:
  - id: wait-for-idle
    type: run_command
    params:
      command: "sh -c 'for i in $(seq 1 {{ inputs.max_wait_minutes }}); do who | grep -q . || exit 0; sleep 60; done; exit 1'"
      timeout: 7200
  - id: restart-service
    type: restart_service
    params:
      service_name: "{{ inputs.service }}"
`
  },
  {
    id: 'reboot-host',
    label: 'Reboot host (with delay)',
    yaml: `name: Reboot host
description: Schedule a delayed reboot.
risk: high

inputs:
  - id: delay
    label: Delay before reboot (seconds)
    type: number
    default: 60

actions:
  - id: reboot
    type: reboot
    params:
      delay_seconds: "{{ inputs.delay }}"
`
  },
];

function _populateTemplatePicker() {
  const sel = document.getElementById('editor-template-picker');
  if (!sel || sel.children.length > 1) return;
  for (const tpl of EDITOR_TEMPLATES) {
    const opt = document.createElement('option');
    opt.value = tpl.id;
    opt.textContent = tpl.label;
    sel.appendChild(opt);
  }
}

function loadEditorTemplate(id) {
  if (!id) return;
  const tpl = EDITOR_TEMPLATES.find(t => t.id === id);
  if (!tpl) return;
  const ta = document.getElementById('editor-yaml');
  const current = (ta.value || '').trim();
  const isPristine = !current || current === DEFAULT_YAML_TEMPLATE.trim();
  if (!isPristine && !confirm('Replace the current YAML with the selected template?')) {
    document.getElementById('editor-template-picker').value = '';
    return;
  }
  ta.value = tpl.yaml;
  document.getElementById('editor-template-picker').value = '';
  onEditorInput();
}

let editorState = {
  definitionId: null,   // null when creating
  lastParsedSpec: null,
  validateTimer: null,
};

/* ── Definition card rendering ───────────────────────────────────────── */
function riskBadgeHtml(risk) {
  const label = { low: 'Low', standard: 'Standard', high: 'High' }[risk] || 'Standard';
  return `<span class="risk-badge risk-${risk}">${label} risk</span>`;
}

function defCardHtml(def, opts) {
  const actions = def.parsed_spec && def.parsed_spec.actions ? def.parsed_spec.actions.length : def.action_count || 0;
  const updated = def.updated_at ? new Date(def.updated_at).toLocaleDateString() : '';
  const ownerLine = opts.showOwner && def.owner_username ? ` · by ${escHtml(def.owner_username)}` : '';
  const isPublished = def.visibility === 'community';
  const publishBadge = isPublished
    ? `<span class="dot-sep">·</span><span style="color:var(--lavender);">published</span>`
    : '';
  const buttons = opts.mode === 'community'
    ? `<button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); forkDefinition('${def.id}')">Fork</button>`
    : `${isPublished
        ? `<button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); unpublishDefinition('${def.id}')">Unpublish</button>`
        : `<button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); publishDefinition('${def.id}')">Publish</button>`}
       <button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); openDefinitionEditor('${def.id}')">Edit</button>
       <button class="btn btn-sky btn-sm" onclick="event.stopPropagation(); openDeployModal('${def.id}')">Deploy</button>`;
  return `
    <div class="def-card" onclick="openDefinitionEditor('${def.id}')">
      <div class="def-card-body">
        <div class="def-card-title">${escHtml(def.name || 'Untitled')}</div>
        <div class="def-card-desc">${escHtml(def.description || 'No description provided.')}</div>
        <div class="def-card-meta">
          ${riskBadgeHtml(def.risk_level || 'standard')}
          <span class="dot-sep">·</span>
          <span>${actions} action${actions === 1 ? '' : 's'}</span>
          ${def.relevance ? `<span class="dot-sep">·</span><span>${escHtml(def.relevance)}</span>` : ''}
          ${updated ? `<span class="dot-sep">·</span><span>${escHtml(updated)}${ownerLine}</span>` : ''}
          ${publishBadge}
        </div>
      </div>
      <div class="def-card-footer">${buttons}</div>
    </div>`;
}

const LIBRARY_EMPTY_HTML = `
  <div class="empty-state">
    <div class="empty-state-icon">
      <svg viewBox="0 0 24 24"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
    </div>
    <div class="empty-state-title">No tasks yet</div>
    <div class="empty-state-desc">Create your first task — describe what it does and list the actions that run in order.</div>
  </div>`;

const COMMUNITY_EMPTY_HTML = `
  <div class="empty-state">
    <div class="empty-state-title">No community templates</div>
    <div class="empty-state-desc">Publish one of your own tasks to share it here.</div>
  </div>`;

// Cached unfiltered lists so search is purely client-side and instant.
const taskGridCache = { library: [], community: [] };

function _renderTaskGrid(scope) {
  const cfg = scope === 'community'
    ? { gridId: 'task-community-grid', searchId: 'task-community-search', mode: 'community', empty: COMMUNITY_EMPTY_HTML, showOwner: true }
    : { gridId: 'task-library-grid',   searchId: 'task-library-search',   mode: 'library',   empty: LIBRARY_EMPTY_HTML,   showOwner: false };
  const grid = document.getElementById(cfg.gridId);
  if (!grid) return;
  const all = taskGridCache[scope] || [];
  const q = (document.getElementById(cfg.searchId)?.value || '').trim().toLowerCase();
  let visible = all;
  if (q) {
    visible = all.filter(d => {
      const haystack = [
        d.name, d.description, d.relevance,
        ...(d.parsed_spec?.actions || []).map(a => `${a.type} ${a.label || ''}`)
      ].join(' ').toLowerCase();
      return haystack.includes(q);
    });
  }
  if (!all.length) { grid.innerHTML = cfg.empty; return; }
  if (!visible.length) {
    const safeQ = escHtml(q);
    grid.innerHTML = '<div class="empty-state"><div class="empty-state-title">No matches</div><div class="empty-state-desc">No tasks match \u201c' + safeQ + '\u201d.</div></div>';
    return;
  }
  grid.innerHTML = visible.map(d => defCardHtml(d, { mode: cfg.mode, showOwner: cfg.showOwner })).join('');
}

function filterTaskGrid(scope) { _renderTaskGrid(scope); }

async function refreshTaskLibrary() {
  try {
    taskGridCache.library = await apiJson('/api/v1/tasks/definitions/?scope=mine');
    _renderTaskGrid('library');
  } catch (e) {
    showToast('Failed to load library: ' + e.message, 'error');
  }
}

async function refreshTaskCommunity() {
  try {
    taskGridCache.community = await apiJson('/api/v1/tasks/definitions/?scope=community');
    _renderTaskGrid('community');
  } catch (e) {
    showToast('Failed to load community: ' + e.message, 'error');
  }
}

async function forkDefinition(id) {
  try {
    await apiJson(`/api/v1/tasks/definitions/${id}/fork/`, { method: 'POST' });
    showToast('Forked into your library', 'success');
    refreshTaskLibrary();
    const libTab = document.querySelector('[data-tab="tasks-library"]');
    if (libTab) libTab.click();
  } catch (e) {
    showToast('Fork failed: ' + e.message, 'error');
  }
}

async function openDefinitionEditor(definitionId) {
  editorState.definitionId = definitionId || null;
  document.getElementById('editor-error').classList.remove('show');
  document.getElementById('editor-page-title').innerHTML = (definitionId ? 'Edit Task' : 'New Task') + '<span>.</span>';
  _populateTemplatePicker();
  document.getElementById('editor-template-picker').value = '';

  if (definitionId) {
    try {
      const def = await apiJson(`/api/v1/tasks/definitions/${definitionId}/`);
      document.getElementById('editor-yaml').value = def.yaml_source || DEFAULT_YAML_TEMPLATE;
    } catch (e) {
      showToast('Failed to load task: ' + e.message, 'error');
      return;
    }
  } else {
    document.getElementById('editor-yaml').value = DEFAULT_YAML_TEMPLATE;
  }

  navigateTo('task-editor');
  onEditorInput();
}

function onEditorInput() {
  clearTimeout(editorState.validateTimer);
  editorState.validateTimer = setTimeout(validateEditor, 280);
}

async function validateEditor() {
  const yaml = document.getElementById('editor-yaml').value;
  const errBox = document.getElementById('editor-error');
  try {
    const body = await apiJson('/api/v1/tasks/definitions/validate/', {
      method: 'POST',
      body: JSON.stringify({ yaml_source: yaml }),
    });
    editorState.lastParsedSpec = body.parsed_spec;
    errBox.classList.remove('show');
    renderEditorPreview(body.parsed_spec);
  } catch (e) {
    editorState.lastParsedSpec = null;
    errBox.textContent = e.message;
    errBox.classList.add('show');
  }
}

function renderEditorPreview(spec) {
  const el = document.getElementById('editor-preview');
  if (!spec) { el.innerHTML = '<div class="preview-heading">—</div>'; return; }
  const actionsHtml = spec.actions.map((a, i) => `
    <div class="preview-step risk-${a.risk}">
      <div class="preview-step-num">${i + 1}</div>
      <div class="preview-step-body">
        <div class="preview-step-title">${escHtml(a.id)} — ${escHtml(a.label || a.type)}</div>
        <div class="preview-step-action">${escHtml(a.type)}${Object.keys(a.params || {}).length ? ' · ' + Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join(' ') : ''}</div>
      </div>
    </div>`).join('');
  const inputs = spec.inputs || [];
  let inputsHtml = '';
  if (inputs.length) {
    const rows = inputs.map(i => {
      const sample = i.type === 'choice'
        ? (i.choices || []).map(c => c.value).join(' | ')
        : (i.default !== undefined && i.default !== '' ? String(i.default) : '');
      return `<div style="display:flex;gap:10px;font-size:12px;padding:6px 10px;border-bottom:1px solid var(--border);">
        <span class="mono" style="color:var(--sky);min-width:120px;">${escHtml(i.id)}</span>
        <span style="color:var(--text-3);min-width:60px;">${escHtml(i.type)}</span>
        <span style="color:var(--text-2);flex:1;">${escHtml(i.label || '')}</span>
        <span class="mono" style="color:var(--text-3);">${escHtml(sample)}</span>
      </div>`;
    }).join('');
    inputsHtml = `
      <div style="margin-top:18px;">
        <div class="preview-heading" style="font-size:14px;">Inputs (asked at deploy time)</div>
        <div style="background:var(--s2);border-radius:var(--r-sm);margin-top:8px;border:1px solid var(--border);">${rows}</div>
      </div>`;
  }
  el.innerHTML = `
    <div class="preview-heading">${escHtml(spec.name)}</div>
    <div class="preview-sub">${escHtml(spec.description || 'No description.')}</div>
    <div class="preview-meta">
      ${riskBadgeHtml(spec.risk)}
      ${spec.relevance ? `<span>${escHtml(spec.relevance)}</span>` : ''}
      <span>${spec.actions.length} step${spec.actions.length === 1 ? '' : 's'}</span>
      ${inputs.length ? `<span>${inputs.length} input${inputs.length === 1 ? '' : 's'}</span>` : ''}
    </div>
    ${inputsHtml}
    <div class="preview-actions">${actionsHtml}</div>`;
}

async function saveDefinitionFromEditor() {
  const yaml = document.getElementById('editor-yaml').value;
  const btn = document.getElementById('editor-save-btn');
  btn.disabled = true; btn.style.opacity = '0.6';
  try {
    const url = editorState.definitionId
      ? `/api/v1/tasks/definitions/${editorState.definitionId}/`
      : '/api/v1/tasks/definitions/';
    const method = editorState.definitionId ? 'PUT' : 'POST';
    await apiJson(url, { method, body: JSON.stringify({ yaml_source: yaml }) });
    showToast('Task saved', 'success');
    await refreshTaskLibrary();
    navigateTo('tasks');
  } catch (e) {
    const errBox = document.getElementById('editor-error');
    errBox.textContent = e.message;
    errBox.classList.add('show');
    showToast('Save failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
}

/* ── navigateTo wrapper: lazy-load library/community grids ───────────── */
// Load the library / community grids when their respective pages become visible.
const originalNavigateForTasks = navigateTo;
navigateTo = function (pageName) {
  originalNavigateForTasks(pageName);
  if (pageName === 'tasks') refreshTaskLibrary();
  if (pageName === 'community') refreshTaskCommunity();
};

// Hook tab switches inside the tasks page so the library grid refreshes on demand.
document.querySelectorAll('.tab-bar[data-tab-group="tasks"] .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    if (tab.dataset.tab === 'tasks-library') refreshTaskLibrary();
  });
});

/* ── Task run detail modal ───────────────────────────────────────────── */

const _TASK_STATE_COLORS = {
  completed: 'var(--mint)', failed: 'var(--rose)', rejected: 'var(--rose)',
  pending: 'var(--peach)', dispatched: 'var(--peach)', executing: 'var(--sky)', blocked: 'var(--lemon)',
};

function _tdEl(tag, style, text) {
  const el = document.createElement(tag);
  if (style) el.style.cssText = style;
  if (text !== undefined) el.textContent = text;
  return el;
}

async function openTaskDetail(runId) {
  if (!runId || runId === 'None') return;
  const overlay = document.getElementById('task-detail-overlay');
  const modal = document.getElementById('task-detail-modal');
  const titleEl = document.getElementById('task-detail-title');
  const metaEl = document.getElementById('task-detail-meta');
  const stepsEl = document.getElementById('task-detail-steps');

  titleEl.textContent = 'Loading…';
  metaEl.replaceChildren();
  stepsEl.replaceChildren();
  overlay.classList.add('open');
  modal.classList.add('open');

  try {
    const run = await apiJson('/api/v1/tasks/runs/' + runId + '/');

    titleEl.textContent = run.definition_name || run.name_snapshot || 'Task Run';

    const fmt = iso => iso ? new Date(iso).toLocaleString() : '—';
    const sep = () => _tdEl('span', 'color:var(--s3);margin:0 4px;', '·');
    const metaParts = [
      run.requested_by_username ? 'by ' + run.requested_by_username : null,
      'started ' + fmt(run.created_at),
      run.finished_at ? 'finished ' + fmt(run.finished_at) : null,
      run.host_count + ' host' + (run.host_count !== 1 ? 's' : '') +
        ', ' + run.step_count + ' step' + (run.step_count !== 1 ? 's' : ''),
    ].filter(Boolean);
    metaParts.forEach((p, i) => {
      metaEl.appendChild(_tdEl('span', null, p));
      if (i < metaParts.length - 1) metaEl.appendChild(sep());
    });

    if (!run.tasks || run.tasks.length === 0) {
      stepsEl.appendChild(_tdEl('div', 'color:var(--text-3);text-align:center;padding:24px;', 'No step records found.'));
      return;
    }

    for (const task of run.tasks) {
      const color = _TASK_STATE_COLORS[task.state] || 'var(--text-3)';
      const output = (task.result_output || '').trim();

      const card = _tdEl('div', 'background:var(--s1);border-radius:var(--r-md);padding:14px 16px;');

      const hdr = _tdEl('div', 'display:flex;align-items:center;gap:10px;margin-bottom:6px;');
      hdr.appendChild(_tdEl('span', 'font-size:13px;font-weight:600;color:var(--text-1);', task.step_label || task.action));
      if (task.step_label && task.step_label !== task.action) {
        hdr.appendChild(_tdEl('span', "font-size:11px;font-family:'IBM Plex Mono',monospace;color:var(--text-3);", task.action));
      }
      hdr.appendChild(_tdEl('span', 'margin-left:auto;font-size:11px;font-weight:600;color:' + color + ';', task.state));
      card.appendChild(hdr);

      const hostLine = _tdEl('div', 'font-size:11px;color:var(--text-3);margin-bottom:' + (output ? '8' : '0') + 'px;');
      hostLine.textContent = (task.host_hostname || String(task.host)) +
        (task.completed_at ? ' · ' + new Date(task.completed_at).toLocaleString() : '');
      card.appendChild(hostLine);

      if (output) {
        const pre = _tdEl('pre', "margin:0;padding:10px 12px;background:var(--s0);border-radius:var(--r-sm);" +
          "font-size:11px;font-family:'IBM Plex Mono',monospace;color:var(--text-2);" +
          'white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;');
        pre.textContent = output;
        card.appendChild(pre);
      } else if (task.state === 'completed' || task.state === 'failed') {
        card.appendChild(_tdEl('div', 'font-size:11px;color:var(--text-3);font-style:italic;', 'No output captured.'));
      }

      stepsEl.appendChild(card);
    }
  } catch (e) {
    const errEl = _tdEl('div', 'color:var(--rose);padding:16px;');
    errEl.textContent = 'Failed to load run details: ' + e.message;
    stepsEl.appendChild(errEl);
  }
}

function closeTaskDetail() {
  document.getElementById('task-detail-overlay').classList.remove('open');
  document.getElementById('task-detail-modal').classList.remove('open');
}
