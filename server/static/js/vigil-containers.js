// vigil-containers.js
// Owns: the Containers page — one host's containers grouped by compose stack,
//   their lifecycle buttons (restart/stop/start, update, roll back, logs), the
//   live log viewer, and the host picker. Vigil-managed stacks on the same
//   page are vigil-stacks.js's (#managed-stacks).
// HTML: templates/pages/_containers.html
// Depends on: vigil-utils.js (escHtml, escAttr, apiJson, showToast, getCsrf,
//   pollingInterval, totpPrompt), vigil-deploy.js (openBuiltinTask,
//   openUpdateContainer), vigil-stacks.js (renderManagedStacks),
//   vigil-host-cards.js (getPins), vigil-monitor.js (monitorHostId, to follow
//   the host already on screen).
// API: GET /api/v1/hosts/{id}/containers/, /api/v1/hosts/{id}/stacks/,
//      POST /api/v1/stacks/adopt/, POST /api/v1/hosts/{id}/containers/{name}/logs/

let containersHostId = null;

/* ── Host selection ──────────────────────────────────────────────────── */
function selectContainersHost(hostId) {
  const picker = document.getElementById('containers-host');
  containersHostId = hostId || null;
  if (picker && picker.value !== (hostId || '')) picker.value = hostId || '';
  document.getElementById('containers-empty').hidden = !!containersHostId;
  document.getElementById('containers-content').hidden = !containersHostId;
  refreshContainers();
}

function refreshContainers() {
  if (containersHostId) renderDockerContainers(containersHostId);
}

// Open the Containers page on a given host — the Monitor page's Containers
// button lands here with the host it was showing.
function openContainersForHost(hostId) {
  navigateTo('containers');
  if (hostId) selectContainersHost(hostId);
}

function openContainersForCurrentMonitor() {
  openContainersForHost(typeof monitorHostId !== 'undefined' ? monitorHostId : null);
}

// A container's state moves on its own (a crash, a restart policy), so the
// page re-reads while it is the one on screen.
pollingInterval(() => {
  if (document.getElementById('page-containers')?.classList.contains('active')) refreshContainers();
}, 30000);

/* ── navigateTo wrapper: on entry, follow the host the Monitor page shows,
    else the first pinned host, else the first host there is. ── */
const _origNavigateContainers = navigateTo;
navigateTo = function(pageName) {
  _origNavigateContainers(pageName);
  if (pageName !== 'containers') return;
  if (containersHostId) { refreshContainers(); return; }
  const picker = document.getElementById('containers-host');
  const known = (id) => id && picker && [...picker.options].some(o => o.value === id);
  const monitored = typeof monitorHostId !== 'undefined' ? monitorHostId : null;
  const pins = typeof getPins === 'function' ? getPins() : [];
  const first = picker && picker.options.length > 1 ? picker.options[1].value : null;
  const pick = [monitored, pins[0] && pins[0].id, first].find(known);
  if (pick) selectContainersHost(pick);
};

/* ── Docker containers ───────────────────────────────────────────────── */
const CTR_STATES = ['running', 'exited', 'dead', 'paused', 'restarting', 'created'];

async function renderDockerContainers(hostId) {
  const wrap = document.getElementById('docker-stacks');
  const countEl = document.getElementById('docker-count');
  if (!wrap) return;
  if (typeof renderManagedStacks === 'function') renderManagedStacks(hostId);

  let containers = [];
  let stacks = [];
  try {
    const [resp, stackResp] = await Promise.all([
      fetch(`/api/v1/hosts/${hostId}/containers/`, { credentials: 'same-origin' }),
      fetch(`/api/v1/hosts/${hostId}/stacks/`, { credentials: 'same-origin' }),
    ]);
    if (resp.ok) containers = await resp.json();
    if (stackResp.ok) stacks = await stackResp.json();
  } catch { containers = []; }
  const stackInfo = Object.fromEntries((stacks || []).map(s => [s.project, s]));

  if (!containers.length) {
    countEl.textContent = '';
    wrap.innerHTML = '<div class="docker-empty">No containers reported for this host.</div>';
    return;
  }
  countEl.textContent = containers.length === 1 ? '1 container' : `${containers.length} containers`;

  // Group by compose stack; ungrouped containers sort last.
  const groups = {};
  for (const c of containers) {
    const key = c.stack || '';
    (groups[key] = groups[key] || []).push(c);
  }
  const stackNames = Object.keys(groups).sort((a, b) => {
    if (a === '') return 1;
    if (b === '') return -1;
    return a.localeCompare(b);
  });

  let html = '';
  for (const stack of stackNames) {
    const rows = groups[stack];
    const label = stack || 'Ungrouped';
    const noun = rows.length === 1 ? 'container' : 'containers';
    const info = stackInfo[stack];
    const stackMeta = info ? `<span class="chip chip-muted apps-src" title="${escAttr((info.config_files || []).join(', '))}">${escHtml(info.ownership)}</span>${info.engine ? `<span class="chip chip-muted apps-src">${escHtml(info.engine)}</span>` : ''}` : '';
    const stackActs = stack ? `<span class="docker-stack-acts">
        <button class="btn btn-xs btn-outline" data-stack-act="Restart stack" data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}">Restart</button>
        <button class="btn btn-xs btn-outline" data-stack-act="Update stack" data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}">Update</button>
        ${info && info.ownership === 'external' ? `<button class="btn btn-xs btn-lav" data-stack-adopt data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}" title="Let Vigil manage this stack where it stands — nothing is recreated">Adopt</button>` : ''}
      </span>` : '';
    html += `<div class="docker-stack">
      <div class="docker-stack-header">
        <span class="docker-stack-name">${escHtml(label)}</span>
        <span class="docker-stack-count">${rows.length} ${noun}</span>
        ${stackMeta}${stackActs}
      </div>
      <table class="ctr-table">
        <colgroup>
          <col class="ctr-c-name"><col class="ctr-c-image"><col class="ctr-c-state">
          <col class="ctr-c-cpu"><col class="ctr-c-mem"><col class="ctr-c-acts">
        </colgroup>
        <thead><tr>
          <th>Name</th><th>Image</th><th>State</th>
          <th class="num">CPU</th><th class="num">Memory</th><th></th>
        </tr></thead>
        <tbody>`;
    for (const c of rows) {
      const state = (c.state || '').toLowerCase();
      const stateClass = CTR_STATES.includes(state) ? state : '';
      const cpu = (c.cpu_percent === null || c.cpu_percent === undefined)
        ? '—' : c.cpu_percent.toFixed(1) + '%';
      let mem = '—';
      if (c.mem_usage_bytes !== null && c.mem_usage_bytes !== undefined) {
        mem = formatBytes(c.mem_usage_bytes);
        if (c.mem_limit_bytes) mem += ' / ' + formatBytes(c.mem_limit_bytes);
      }
      const svc = c.service ? `<div class="ctr-svc">${escHtml(c.service)}</div>` : '';
      html += `<tr>
        <td><div class="ctr-name">${escHtml(c.name || '')}</div>${svc}</td>
        <td class="ctr-image">${escHtml(c.image || '')}</td>
        <td><span class="ctr-state ${stateClass}">${escHtml(state || 'unknown')}</span></td>
        <td class="ctr-stat">${cpu}</td>
        <td class="ctr-stat">${mem}</td>
        <td class="ctr-fix">${c.outdated ? `<button class="btn btn-xs btn-mint" data-ctr-update data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}" title="A newer image is available">Update</button>` : ''}
          ${state === 'running'
            ? `<button class="btn btn-xs btn-outline" data-ctr-act="Restart container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">Restart</button>
               <button class="btn btn-xs btn-outline" data-ctr-act="Stop container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">Stop</button>`
            : `<button class="btn btn-xs btn-outline" data-ctr-act="Start container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">Start</button>`}
          <button class="btn btn-xs btn-outline" data-ctr-logs data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">Logs</button>
          ${c.previous_image && !c.rolled_back ? `<button class="btn btn-xs btn-outline" data-ctr-rollback data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}" data-image="${escAttr(c.previous_image)}" title="Go back to ${escAttr(c.previous_image)}">Roll back</button>` : ''}
          ${c.rolled_back ? `<span class="chip apps-chip-warn" title="Pinned to ${escAttr(c.rolled_back)} — the next update clears it">rolled back</span>` : ''}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }
  wrap.innerHTML = html;
  wrap.querySelectorAll('[data-ctr-update]').forEach(btn => btn.addEventListener('click', () => {
    if (typeof openUpdateContainer === 'function') {
      openUpdateContainer(btn.dataset.host, btn.dataset.name);
    }
  }));
  wrap.querySelectorAll('[data-ctr-act]').forEach(btn => btn.addEventListener('click', () => {
    openBuiltinTask(btn.dataset.ctrAct, btn.dataset.host, { container_name: btn.dataset.name });
  }));
  wrap.querySelectorAll('[data-ctr-rollback]').forEach(btn => btn.addEventListener('click', () => {
    openBuiltinTask('Roll back container', btn.dataset.host,
      { container_name: btn.dataset.name, image: btn.dataset.image });
  }));
  wrap.querySelectorAll('[data-ctr-logs]').forEach(btn => btn.addEventListener('click', () => {
    openContainerLogs(btn.dataset.host, btn.dataset.name);
  }));
  wrap.querySelectorAll('[data-stack-adopt]').forEach(btn => btn.addEventListener('click', async () => {
    const totp = await totpPrompt(`Adopting ${btn.dataset.project} reads its compose file and .env into Vigil.`);
    if (!totp) return;
    try {
      await apiJson('/api/v1/stacks/adopt/', { method: 'POST', body: JSON.stringify(
        { host_id: btn.dataset.host, project: btn.dataset.project, totp: totp.trim() }) });
      showToast(`Adopting ${btn.dataset.project} — it appears under Managed by Vigil after the agent's next check-in`, 'success');
    } catch (e) { showToast(e.message, 'error'); }
  }));
  wrap.querySelectorAll('[data-stack-act]').forEach(btn => btn.addEventListener('click', () => {
    openBuiltinTask(btn.dataset.stackAct, btn.dataset.host, { project: btn.dataset.project });
  }));
}


/* ── Live container logs (M11) ───────────────────────────────────────── */
// Opening logs is a signed task (TOTP). The agent sends the last lines, then
// streams new ones while this view polls; closing it (or 15 s without a poll)
// stops the agent.
const logView = { session: null, after: 0, timer: null, initialShown: false };

function _logEls() {
  let overlay = document.getElementById('ctr-logs-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'ctr-logs-overlay';
    const modal = document.createElement('div');
    modal.className = 'modal modal-wide';
    modal.id = 'ctr-logs-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML = `<div class="modal-title"><span id="ctr-logs-title">Logs</span>
        <button class="modal-close" type="button" id="ctr-logs-close" aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button></div>
      <div class="ctr-logs-state" id="ctr-logs-state">Waiting for the agent…</div>
      <pre class="ctr-logs" id="ctr-logs-body" tabindex="0"></pre>`;
    document.body.append(overlay, modal);
    overlay.addEventListener('click', closeContainerLogs);
    modal.querySelector('#ctr-logs-close').addEventListener('click', closeContainerLogs);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && modal.classList.contains('open')) closeContainerLogs();
    });
  }
  return { overlay, modal: document.getElementById('ctr-logs-modal'),
           body: document.getElementById('ctr-logs-body'), state: document.getElementById('ctr-logs-state') };
}

async function openContainerLogs(hostId, name) {
  const totp = await totpPrompt(`Reading the logs of ${name} shows everything those containers printed.`);
  if (!totp) return;
  let opened;
  try {
    opened = await apiJson(`/api/v1/hosts/${hostId}/containers/${encodeURIComponent(name)}/logs/`,
      { method: 'POST', body: JSON.stringify({ totp: totp.trim(), tail: 200 }) });
  } catch (e) { showToast(e.message, 'error'); return; }
  const els = _logEls();
  Object.assign(logView, { session: opened.session, after: 0, initialShown: false });
  document.getElementById('ctr-logs-title').textContent = `Logs — ${name}`;
  els.body.textContent = '';
  els.state.textContent = 'Waiting for the agent (up to one check-in)…';
  els.overlay.classList.add('open');
  els.modal.classList.add('open');
  els.body.focus();
  clearPollingInterval(logView.timer);
  logView.timer = pollingInterval(_pollContainerLogs, 1500);
  _pollContainerLogs();
}

async function _pollContainerLogs() {
  if (!logView.session) return;
  const els = _logEls();
  let body;
  try {
    body = await apiJson(`/api/v1/hosts/log-tails/${logView.session}/?after=${logView.after}`);
  } catch { return; }
  const atBottom = els.body.scrollTop + els.body.clientHeight >= els.body.scrollHeight - 8;
  if (!logView.initialShown && body.initial) {
    els.body.textContent = body.initial + '\n';
    logView.initialShown = true;
  }
  for (const [seq, line] of body.lines) {
    els.body.textContent += line + '\n';
    logView.after = seq;
  }
  els.state.textContent = body.task_state === 'failed' ? 'The agent could not read the logs.'
    : body.task_state === 'completed' ? (body.live ? 'Live — new lines appear as they are written.' : 'Stopped.')
      : 'Waiting for the agent (up to one check-in)…';
  if (atBottom) els.body.scrollTop = els.body.scrollHeight;
}

function closeContainerLogs() {
  const els = _logEls();
  els.overlay.classList.remove('open');
  els.modal.classList.remove('open');
  clearPollingInterval(logView.timer);
  if (logView.session) {
    fetch(`/api/v1/hosts/log-tails/${logView.session}/`, {
      method: 'DELETE', credentials: 'same-origin', headers: { 'X-CSRFToken': getCsrf() } });
  }
  logView.session = null;
}
