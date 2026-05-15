// vigil-deploy.js
// Owns: Full deploy modal (templates/base.html) — YAML preview, options,
//   schedule/retry/success-criteria policy editors, host & tag pickers,
//   fleet cache. Also the "deploy from host card" entry point and the
//   publish / unpublish definition actions.
// HTML: deploy modal in templates/base.html (modal-xwide).
// Depends on: vigil-utils.js (apiJson, showToast),
//   vigil-tasks.js (riskBadgeHtml, refreshTaskLibrary called after publish),
//   vigil-nav.js (navigateTo for openDeployForHost handoff).
// Reads: window.VIGIL_CONFIG.timezone (set in templates/base.html).
// API: GET /api/v1/hosts/, GET /api/v1/hosts/tags/,
//   GET /api/v1/tasks/definitions/{id}/, POST .../deploy/,
//   POST .../{publish,unpublish}/

let deployState = {
  definitionId: null,
  spec: null,
  availableHosts: [],
  selectedHosts: new Set(),
  targetMode: 'hosts',          // 'hosts' or 'tags'
  availableTags: [],
  selectedTags: new Set(),
};

/* ── Entry point invoked from a host card: jump to Tasks, preselect host */
function openDeployForHost(hostId) {
  // Switch to the Tasks page and let the user pick a definition;
  // pre-seed the deploy modal so this host is preselected.
  navigateTo('tasks');
  window._pendingDeployPreselectHost = hostId;
  showToast('Pick a task to deploy to this host', 'info');
}

/* ── Modal close + YAML highlighter ──────────────────────────────────── */
function closeDeployModal() {
  document.getElementById('deploy-overlay').classList.remove('open');
  document.getElementById('deploy-modal').classList.remove('open');
}

/* Tiny YAML highlighter — just enough to colorize keys, strings, numbers,
   booleans, comments, and dashes. Returns escaped HTML. */
function highlightYaml(src) {
  const lines = (src || '').split('\n');
  const out = [];
  for (const raw of lines) {
    // Comments take the rest of the line.
    const hashIdx = (() => {
      let inStr = null;
      for (let i = 0; i < raw.length; i++) {
        const c = raw[i];
        if (inStr) { if (c === inStr && raw[i-1] !== '\\') inStr = null; continue; }
        if (c === '"' || c === "'") inStr = c;
        else if (c === '#' && (i === 0 || /\s/.test(raw[i-1]))) return i;
      }
      return -1;
    })();

    let body = raw, comment = '';
    if (hashIdx >= 0) { body = raw.slice(0, hashIdx); comment = raw.slice(hashIdx); }

    // Match: leading whitespace, optional dash, then either `key:` or a scalar.
    const m = body.match(/^(\s*)(- )?(.*)$/);
    const indent = m[1] || '';
    const dash = m[2] || '';
    let rest = m[3] || '';

    let html = escHtml(indent);
    if (dash) html += '<span class="yh-dash">- </span>';

    const kvMatch = rest.match(/^([A-Za-z_][\w.-]*)(\s*:)(\s*)(.*)$/);
    if (kvMatch) {
      html += '<span class="yh-key">' + escHtml(kvMatch[1]) + '</span>';
      html += escHtml(kvMatch[2] + kvMatch[3]);
      html += highlightYamlValue(kvMatch[4]);
    } else if (rest) {
      html += highlightYamlValue(rest);
    }

    if (comment) html += '<span class="yh-comment">' + escHtml(comment) + '</span>';
    out.push(html);
  }
  return out.join('\n');
}

function highlightYamlValue(v) {
  if (!v) return '';
  // String literal
  const strM = v.match(/^("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')(.*)$/);
  if (strM) return '<span class="yh-string">' + escHtml(strM[1]) + '</span>' + highlightYamlValue(strM[2]);
  // Boolean / null
  if (/^(true|false|yes|no|on|off)\b/i.test(v)) {
    const b = v.match(/^(true|false|yes|no|on|off)(.*)$/i);
    return '<span class="yh-bool">' + escHtml(b[1]) + '</span>' + highlightYamlValue(b[2]);
  }
  if (/^(null|~)\b/i.test(v)) {
    const n = v.match(/^(null|~)(.*)$/i);
    return '<span class="yh-null">' + escHtml(n[1]) + '</span>' + highlightYamlValue(n[2]);
  }
  // Number
  const numM = v.match(/^(-?\d+(?:\.\d+)?)(.*)$/);
  if (numM && /^[\s,\]\}]?/.test(numM[2])) {
    return '<span class="yh-number">' + escHtml(numM[1]) + '</span>' + highlightYamlValue(numM[2]);
  }
  // Inline mapping/sequence delimiters and the rest
  return escHtml(v);
}

const DEPLOY_DAY_LABELS = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const VIGIL_TIMEZONE = window.VIGIL_CONFIG.timezone;

/* ── Tabs, target mode, policy editors, host/tag pickers, submit ────── */
function setDeployTab(tab) {
  for (const t of document.querySelectorAll('.deploy-tab[data-tab]')) {
    t.classList.toggle('active', t.dataset.tab === tab);
  }
  for (const p of document.querySelectorAll('.deploy-panel')) {
    p.classList.toggle('active', p.dataset.panel === tab);
  }
}

function setDeployTargetMode(mode) {
  deployState.targetMode = (mode === 'tags') ? 'tags' : 'hosts';
  for (const t of document.querySelectorAll('.deploy-tab[data-target-mode]')) {
    t.classList.toggle('active', t.dataset.targetMode === deployState.targetMode);
  }
  document.getElementById('deploy-target-hosts').style.display =
    deployState.targetMode === 'hosts' ? '' : 'none';
  document.getElementById('deploy-target-tags').style.display =
    deployState.targetMode === 'tags' ? '' : 'none';
  updateDeployHostSummary();
}

function _renderDeployTagChips() {
  const wrap = document.getElementById('deploy-tag-chips');
  if (!wrap) return;
  wrap.replaceChildren();
  if (!deployState.availableTags.length) {
    const empty = document.createElement('div');
    empty.className = 'deploy-policy-help';
    empty.textContent = 'No tags exist yet. Tag agents in the host detail panel or via agent.yml.';
    wrap.appendChild(empty);
    return;
  }
  for (const t of deployState.availableTags) {
    const chip = document.createElement('span');
    chip.className = 'deploy-day-chip' + (deployState.selectedTags.has(t.tag) ? ' on' : '');
    chip.textContent = `${t.tag} · ${t.host_count}`;
    chip.dataset.tag = t.tag;
    chip.addEventListener('click', () => {
      if (deployState.selectedTags.has(t.tag)) deployState.selectedTags.delete(t.tag);
      else deployState.selectedTags.add(t.tag);
      chip.classList.toggle('on');
      updateDeployHostSummary();
    });
    wrap.appendChild(chip);
  }
}

function _populateDeployDayChips(activeDays) {
  const wrap = document.getElementById('deploy-day-chips');
  if (!wrap) return;
  const set = new Set(activeDays || [0,1,2,3,4,5,6]);
  wrap.replaceChildren();
  DEPLOY_DAY_LABELS.forEach((label, idx) => {
    const chip = document.createElement('span');
    chip.className = 'deploy-day-chip' + (set.has(idx) ? ' on' : '');
    chip.textContent = label;
    chip.dataset.day = String(idx);
    chip.addEventListener('click', () => chip.classList.toggle('on'));
    wrap.appendChild(chip);
  });
}

function _readDeployDays() {
  const out = [];
  for (const chip of document.querySelectorAll('#deploy-day-chips .deploy-day-chip.on')) {
    out.push(Number(chip.dataset.day));
  }
  return out.sort((a,b) => a-b);
}

function _toTimeValue(hour, minute) {
  return String(hour ?? 0).padStart(2,'0') + ':' + String(minute ?? 0).padStart(2,'0');
}

function _onRunNowToggle() {
  // Grey out the start/end/day inputs when "Run now" is checked so the form
  // can't visually suggest a window that won't actually be sent.
  const runNow = document.getElementById('deploy-schedule-run-now');
  const fields = document.getElementById('deploy-schedule-fields');
  const start  = document.getElementById('deploy-schedule-start');
  const end    = document.getElementById('deploy-schedule-end');
  const chips  = document.getElementById('deploy-day-chips');
  const disabled = !!(runNow && runNow.checked);
  if (start) start.disabled = disabled;
  if (end)   end.disabled   = disabled;
  if (fields) fields.style.opacity = disabled ? '0.4' : '1';
  if (chips)  chips.style.pointerEvents = disabled ? 'none' : 'auto';
}
window._onRunNowToggle = _onRunNowToggle;

function _populateDeployPolicy(spec) {
  // Schedule defaults from spec; otherwise "Run now" — no window at all.
  // The Run now checkbox is the source of truth: when checked, _readDeployPolicy
  // returns schedule=null so the server stores schedule={} and the dispatcher
  // doesn't gate the task.
  const sched = (spec && spec.schedule && spec.schedule.window) || null;
  const runNow = document.getElementById('deploy-schedule-run-now');
  if (runNow) {
    runNow.checked = !sched;
  }
  document.getElementById('deploy-schedule-start').value = sched
    ? _toTimeValue(sched.start_hour, sched.start_minute) : '08:00';
  document.getElementById('deploy-schedule-end').value = sched
    ? _toTimeValue(sched.end_hour, sched.end_minute) : '17:00';
  const tzLabel = document.getElementById('deploy-tz-label');
  if (tzLabel) tzLabel.textContent = VIGIL_TIMEZONE;
  _populateDeployDayChips(sched ? sched.days : [0,1,2,3,4,5,6]);
  _onRunNowToggle();

  // Retry defaults
  const retry = (spec && spec.on_failure && spec.on_failure.retry) || null;
  document.getElementById('deploy-retry-attempts').value = retry ? retry.attempts ?? 0 : 0;
  document.getElementById('deploy-retry-delay').value    = retry ? retry.delay_seconds ?? 30 : 30;

  // Success criteria defaults
  const crit = (spec && spec.success_criteria) || null;
  document.getElementById('deploy-criteria-exit').value     = crit && crit.exit_code !== undefined ? crit.exit_code : 0;
  document.getElementById('deploy-criteria-contains').value = (crit && crit.output_contains) || '';
  document.getElementById('deploy-criteria-regex').value    = (crit && crit.output_regex) || '';
}

function _parseTimeInput(id, defaultHour, defaultMinute = 0) {
  const val = (document.getElementById(id)?.value || '').trim();
  if (!val) return [defaultHour, defaultMinute];
  const [hStr, mStr] = val.split(':');
  return [parseInt(hStr, 10) || 0, parseInt(mStr, 10) || 0];
}

function _readDeployPolicy() {
  // "Run now" wins over the form fields. Returning schedule=null here makes
  // the deploy endpoint store schedule={} on the Task row, which is what
  // schedule_window_active() treats as "always eligible".
  const runNow = document.getElementById('deploy-schedule-run-now');
  let schedule = null;
  if (!runNow || !runNow.checked) {
    const [startH, startM] = _parseTimeInput('deploy-schedule-start', 8, 0);
    const [endH,   endM]   = _parseTimeInput('deploy-schedule-end',   17, 0);
    const days = _readDeployDays();
    schedule = { window: {
      start_hour: startH, start_minute: startM,
      end_hour: endH,     end_minute: endM,
      days: days.length ? days : [0,1,2,3,4,5,6],
    } };
  }

  const attempts = Number(document.getElementById('deploy-retry-attempts').value || 0);
  const delay    = Number(document.getElementById('deploy-retry-delay').value || 0);
  const on_failure = attempts > 0 ? { retry: { attempts, delay_seconds: delay } } : null;

  const exit_code = Number(document.getElementById('deploy-criteria-exit').value || 0);
  const contains  = document.getElementById('deploy-criteria-contains').value.trim();
  const regex     = document.getElementById('deploy-criteria-regex').value.trim();
  const success_criteria = { exit_code };
  if (contains) success_criteria.output_contains = contains;
  if (regex)    success_criteria.output_regex = regex;

  return { schedule, on_failure, success_criteria };
}

function _renderDeployInputs(inputs) {
  const wrap = document.getElementById('deploy-inputs-wrap');
  const list = document.getElementById('deploy-inputs');
  const tab  = document.getElementById('deploy-tab-options');
  if (!inputs || !inputs.length) {
    wrap.style.display = 'none';
    list.replaceChildren();
    if (tab) tab.style.display = 'none';
    return;
  }
  wrap.style.display = 'block';
  if (tab) tab.style.display = '';
  list.replaceChildren();
  for (const inp of inputs) {
    const row = document.createElement('div');
    row.className = 'deploy-input-row' + (inp.type === 'boolean' ? ' bool' : '');
    row.dataset.inputId = inp.id;
    row.dataset.inputType = inp.type;

    const label = document.createElement('label');
    label.className = 'deploy-input-label';
    label.textContent = inp.label || inp.id;

    let control;
    if (inp.type === 'choice') {
      control = document.createElement('select');
      control.className = 'form-control';
      for (const c of (inp.choices || [])) {
        const opt = document.createElement('option');
        opt.value = c.value;
        opt.textContent = c.label || c.value;
        if (c.value === inp.default) opt.selected = true;
        control.appendChild(opt);
      }
    } else if (inp.type === 'boolean') {
      control = document.createElement('input');
      control.type = 'checkbox';
      control.checked = !!inp.default;
    } else if (inp.type === 'number') {
      control = document.createElement('input');
      control.type = 'number';
      control.className = 'form-control';
      control.value = inp.default ?? '';
    } else {
      control = document.createElement('input');
      control.type = 'text';
      control.className = 'form-control';
      control.value = inp.default ?? '';
      if (inp.required) control.required = true;
    }
    control.classList.add('deploy-input-control');

    if (inp.type === 'boolean') {
      row.appendChild(control);
      row.appendChild(label);
    } else {
      row.appendChild(label);
      if (inp.description) {
        const d = document.createElement('div');
        d.className = 'deploy-input-desc';
        d.textContent = inp.description;
        row.appendChild(d);
      }
      row.appendChild(control);
    }
    list.appendChild(row);
  }
}

function _readDeployInputs() {
  const out = {};
  for (const row of document.querySelectorAll('#deploy-inputs .deploy-input-row')) {
    const id = row.dataset.inputId;
    const type = row.dataset.inputType;
    const ctrl = row.querySelector('.deploy-input-control');
    if (!ctrl) continue;
    if (type === 'boolean') out[id] = !!ctrl.checked;
    else if (type === 'number') out[id] = ctrl.value === '' ? null : Number(ctrl.value);
    else out[id] = ctrl.value;
  }
  return out;
}

function _renderDeployHostRows() {
  const rowsEl = document.getElementById('deploy-host-rows');
  const all = deployState.availableHosts || [];
  const q = (document.getElementById('deploy-host-search')?.value || '').trim().toLowerCase();
  let visible = all;
  if (q) {
    visible = all.filter(h =>
      (h.hostname || '').toLowerCase().includes(q)
      || (h.ip_address || '').toLowerCase().includes(q)
      || (h.mode || '').toLowerCase().includes(q)
    );
  }
  if (!all.length) {
    rowsEl.replaceChildren();
    const empty = document.createElement('div');
    empty.className = 'host-pick-empty';
    empty.textContent = 'No online, executable hosts available.';
    rowsEl.appendChild(empty);
    return;
  }
  rowsEl.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'host-pick-empty';
    empty.textContent = 'No hosts match your search.';
    rowsEl.appendChild(empty);
    return;
  }
  for (const h of visible) {
    const row = document.createElement('label');
    row.className = 'deploy-host-row';
    if (deployState.selectedHosts.has(h.id)) row.classList.add('selected');

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = h.id;
    cb.checked = deployState.selectedHosts.has(h.id);
    cb.addEventListener('change', () => {
      if (cb.checked) deployState.selectedHosts.add(h.id);
      else deployState.selectedHosts.delete(h.id);
      row.classList.toggle('selected', cb.checked);
      updateDeployHostSummary();
      _syncSelectAllCheckbox();
    });

    const nameWrap = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'dh-name';
    name.textContent = h.hostname;
    const meta = document.createElement('div');
    meta.className = 'dh-meta';
    meta.textContent = h.ip_address || '';
    nameWrap.appendChild(name);
    if (h.ip_address) nameWrap.appendChild(meta);

    const mode = document.createElement('div');
    mode.className = 'dh-meta';
    mode.textContent = h.mode;

    row.appendChild(cb);
    row.appendChild(nameWrap);
    row.appendChild(mode);
    rowsEl.appendChild(row);
  }
  _syncSelectAllCheckbox();
}

function _syncSelectAllCheckbox() {
  const all = deployState.availableHosts || [];
  const checkbox = document.getElementById('deploy-host-all');
  if (!checkbox) return;
  if (!all.length) { checkbox.checked = false; checkbox.indeterminate = false; return; }
  const selected = all.filter(h => deployState.selectedHosts.has(h.id)).length;
  checkbox.checked = selected === all.length && all.length > 0;
  checkbox.indeterminate = selected > 0 && selected < all.length;
}

function toggleAllDeployHosts(checked) {
  const all = deployState.availableHosts || [];
  if (checked) all.forEach(h => deployState.selectedHosts.add(h.id));
  else deployState.selectedHosts.clear();
  _renderDeployHostRows();
  updateDeployHostSummary();
}

function filterDeployHosts() { _renderDeployHostRows(); }

// Cached fleet data — refreshed lazily (max once per 60s) so the deploy
// modal opens instantly on repeat uses without waiting for API round-trips.
let _deployHostCache = null;
let _deployTagCache  = null;
let _deployFleetFetchedAt = 0;

async function _ensureFleetCache(force) {
  const age = Date.now() - _deployFleetFetchedAt;
  if (!force && _deployHostCache && age < 60_000) return;
  const [hosts, tags] = await Promise.all([
    apiJson('/api/v1/hosts/'),
    apiJson('/api/v1/hosts/tags/').catch(() => []),
  ]);
  _deployHostCache = hosts.filter(h => h.status === 'online' && h.mode !== 'monitor');
  _deployTagCache  = Array.isArray(tags) ? tags : [];
  _deployFleetFetchedAt = Date.now();
}

async function openDeployModal(definitionId) {
  // Show modal skeleton immediately so the UI feels instant.
  document.getElementById('deploy-modal-title').textContent = 'Loading…';
  document.getElementById('deploy-risk-label').innerHTML = '';
  document.getElementById('deploy-yaml-view').innerHTML = '';
  document.getElementById('deploy-totp').value = '';
  document.getElementById('deploy-host-search').value = '';
  deployState.definitionId = definitionId;
  deployState.selectedHosts = new Set();
  deployState.selectedTags = new Set();
  document.getElementById('deploy-overlay').classList.add('open');
  document.getElementById('deploy-modal').classList.add('open');
  setDeployTab('yaml');

  try {
    // Fetch definition + fleet in parallel, fleet may come from cache.
    const [def] = await Promise.all([
      apiJson(`/api/v1/tasks/definitions/${definitionId}/`),
      _ensureFleetCache(),
    ]);

    deployState.spec = def.parsed_spec;
    setDeployTargetMode('hosts');
    document.getElementById('deploy-modal-title').textContent = `Deploy — ${def.name}`;
    document.getElementById('deploy-risk-label').innerHTML = riskBadgeHtml(def.risk_level || 'standard');

    // YAML highlighting deferred one frame so the modal paints first.
    requestAnimationFrame(() => {
      document.getElementById('deploy-yaml-view').innerHTML = highlightYaml(def.yaml_source || '');
    });

    _renderDeployInputs((def.parsed_spec && def.parsed_spec.inputs) || []);
    _populateDeployPolicy(def.parsed_spec || {});

    deployState.availableHosts = _deployHostCache || [];
    deployState.availableTags  = _deployTagCache  || [];
    _renderDeployTagChips();

    if (window._pendingDeployPreselectHost) {
      const preId = window._pendingDeployPreselectHost;
      if (deployState.availableHosts.some(h => h.id === preId)) {
        deployState.selectedHosts.add(preId);
      }
      window._pendingDeployPreselectHost = null;
    }
    _renderDeployHostRows();
    updateDeployHostSummary();
  } catch (e) {
    document.getElementById('deploy-overlay').classList.remove('open');
    document.getElementById('deploy-modal').classList.remove('open');
    showToast('Failed to open deploy modal: ' + e.message, 'error');
  }
}

function updateDeployHostSummary() {
  const stepCount = (deployState.spec && deployState.spec.actions && deployState.spec.actions.length) || 0;

  if (deployState.targetMode === 'tags') {
    const tagSummary = document.getElementById('deploy-tag-summary');
    const selectedTagList = [...deployState.selectedTags];
    if (!selectedTagList.length) {
      tagSummary.textContent = 'No tags selected.';
      return;
    }
    // Estimate matched hosts using the available host list, since the
    // server makes the final decision (this is just a preview).
    const matched = (deployState.availableHosts || []).filter(h =>
      (h.tags || []).some(t => deployState.selectedTags.has(t))
    );
    const totalSteps = stepCount * matched.length;
    tagSummary.textContent = `${selectedTagList.length} tag${selectedTagList.length === 1 ? '' : 's'} → ${matched.length} matched host${matched.length === 1 ? '' : 's'} · ${totalSteps} step${totalSteps === 1 ? '' : 's'} will run`;
    return;
  }

  const total = (deployState.availableHosts || []).length;
  const count = deployState.selectedHosts ? deployState.selectedHosts.size : 0;
  const summary = document.getElementById('deploy-host-summary');
  if (!total) { summary.textContent = 'No online hosts available.'; return; }
  if (!count) { summary.textContent = `Select one or more of ${total} host${total === 1 ? '' : 's'}.`; return; }
  const totalSteps = stepCount * count;
  summary.textContent = `${count} host${count === 1 ? '' : 's'} selected · ${totalSteps} step${totalSteps === 1 ? '' : 's'} will run in order`;
}

async function submitDeploy(event) {
  event.preventDefault();

  const totp = document.getElementById('deploy-totp').value.trim();
  if (!totp) { showToast('Enter your TOTP code', 'error'); return; }

  const policy = _readDeployPolicy();
  const body = {
    totp,
    inputs: _readDeployInputs(),
    schedule: policy.schedule,
    on_failure: policy.on_failure,
    success_criteria: policy.success_criteria,
  };

  if (deployState.targetMode === 'tags') {
    const tags = [...deployState.selectedTags];
    if (!tags.length) { showToast('Select at least one tag', 'error'); return; }
    body.tags = tags;
  } else {
    const host_ids = [...(deployState.selectedHosts || [])];
    if (!host_ids.length) { showToast('Select at least one host', 'error'); return; }
    body.host_ids = host_ids;
  }

  const btn = document.getElementById('deploy-submit-btn');
  btn.disabled = true; btn.style.opacity = '0.6';
  try {
    const run = await apiJson(`/api/v1/tasks/definitions/${deployState.definitionId}/deploy/`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    showToast(`Deployed to ${run.host_count} host${run.host_count === 1 ? '' : 's'}`, 'success');
    closeDeployModal();
  } catch (e) {
    showToast('Deploy failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDeployModal();
});

