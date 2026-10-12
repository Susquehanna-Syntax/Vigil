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
//      /api/v1/hosts/{id}/docker/ (images, volumes, networks, storage — 14.3),
//      /api/v1/hosts/docker-summary/ (the host picker),
//      POST /api/v1/stacks/adopt/, POST /api/v1/hosts/{id}/containers/{name}/logs/

let containersHostId = null;
//: The page's state beyond the host: the open tab, the filter bar, and what
//: was last fetched (so typing in the filter redraws without refetching).
const ctrState = { tab: 'containers', q: '', chip: '', hosts: null, raw: null, docker: null };

/* ── Host selection ──────────────────────────────────────────────────── */
async function _ctrHosts(force) {
  if (!ctrState.hosts || force) {
    try { ctrState.hosts = await apiJson('/api/v1/hosts/docker-summary/'); } catch { ctrState.hosts = ctrState.hosts || []; }
  }
  return ctrState.hosts;
}

/** The hostname for an id, for labels elsewhere (the stack editor's crumbs). */
function containersHostLabel(hostId) {
  const h = (ctrState.hosts || []).find(x => x.id === hostId);
  return h ? h.hostname : '';
}

function selectContainersHost(hostId) {
  const input = document.getElementById('containers-host');
  if (containersHostId !== (hostId || null)) { ctrState.raw = null; ctrState.docker = null; }
  containersHostId = hostId || null;
  if (input) input.value = hostId || '';
  const label = document.getElementById('containers-host-label');
  if (label) label.textContent = containersHostLabel(hostId) || (hostId ? 'Host' : 'Choose a host…');
  document.getElementById('containers-empty').hidden = !!containersHostId;
  document.getElementById('containers-content').hidden = !containersHostId;
  _renderCtrPins();
  refreshContainers();
  if (hostId && !containersHostLabel(hostId)) {
    _ctrHosts(true).then(() => { if (label) label.textContent = containersHostLabel(hostId) || 'Host'; });
  }
}

async function pickContainersHost() {
  const hosts = await _ctrHosts(true);
  openPicker({
    type: 'machine', title: 'Pick a host', allowAdd: false,
    items: hosts.map(h => ({
      key: h.id, name: h.hostname,
      meta: h.containers
        ? `${h.containers} container${h.containers === 1 ? '' : 's'} (${h.running} running)${h.engine ? ' · ' + h.engine : ''}`
        : 'no containers reported',
      raw: h,
    })),
    onSelect: (item) => selectContainersHost(item.key),
  });
}

/** Pinned hosts (the Monitor page's pins) as one-click chips. */
function _renderCtrPins() {
  const wrap = document.getElementById('containers-pins');
  if (!wrap) return;
  const pins = typeof getPins === 'function' ? getPins() : [];
  wrap.innerHTML = pins.slice(0, 8).map(p => `<button type="button" class="chip ctr-pin${p.id === containersHostId ? ' is-on' : ''}"
      data-ctr-host="${escAttr(p.id)}">${escHtml(p.hostname || '')}</button>`).join('');
}

function refreshContainers() {
  if (!containersHostId) return;
  renderDockerContainers(containersHostId);
  loadDockerResources(containersHostId);
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
  _ctrHosts(true).then((hosts) => {
    if (containersHostId) return;   // picked while the list loaded
    const known = (id) => id && hosts.some(h => h.id === id);
    const monitored = typeof monitorHostId !== 'undefined' ? monitorHostId : null;
    const pins = typeof getPins === 'function' ? getPins() : [];
    // The summary is sorted by container count: its first host runs the most.
    const busiest = hosts.length && hosts[0].containers ? hosts[0].id : null;
    const pick = [monitored, pins[0] && pins[0].id, busiest, hosts[0] && hosts[0].id].find(known);
    if (pick) selectContainersHost(pick);
  });
};

/* ── Docker containers ───────────────────────────────────────────────── */
const CTR_STATES = ['running', 'exited', 'dead', 'paused', 'restarting', 'created'];

/* Docker's status text carries the exit code ("Exited (137) 2 hours ago").
   Code 0 is a clean stop; anything else (or "dead") is a crash, and the two
   must not look the same. */
function _ctrExitCode(c) {
  const m = /exited \((-?\d+)\)/i.exec(c.status || '');
  return m ? parseInt(m[1], 10) : null;
}

function _ctrStateBadge(c) {
  const state = (c.state || '').toLowerCase();
  const code = _ctrExitCode(c);
  let text = state || 'unknown';
  let cls = CTR_STATES.includes(state) ? state : '';
  if (state === 'exited' && code !== null) {
    text = code === 0 ? 'stopped (exit 0)' : `crashed (exit ${code})`;
    cls = code === 0 ? 'stopped' : 'exited';
  }
  return `<span class="ctr-state ${cls}" title="${escAttr(c.status || state)}">${escHtml(text)}</span>`;
}

/* One state for a whole stack: running, partly running, stopped or crashed —
   so a stack whose containers all exited still reads as down at a glance. */
function _stackStateBadge(rows) {
  const running = rows.filter(c => (c.state || '').toLowerCase() === 'running').length;
  const crashed = rows.filter(c => (c.state || '').toLowerCase() === 'dead'
    || ((c.state || '').toLowerCase() === 'exited' && (_ctrExitCode(c) || 0) !== 0));
  const codes = [...new Set(crashed.map(_ctrExitCode).filter(x => x !== null))];
  const exitNote = codes.length ? ` (exit ${codes.join(', ')})` : '';
  let text, cls;
  if (running === rows.length) { text = 'running'; cls = 'running'; }
  else if (running === 0) {
    text = crashed.length ? `crashed${exitNote}` : 'stopped (exit 0)';
    cls = crashed.length ? 'exited' : 'stopped';
  } else {
    text = `${running}/${rows.length} running` + (crashed.length ? ` · ${crashed.length} crashed${exitNote}` : '');
    cls = 'partial';
  }
  return `<span class="ctr-state ${cls}">${escHtml(text)}</span>`;
}

/* The Containers tab's quick filters. */
const CTR_CHIPS = {
  containers: [['', 'All'], ['running', 'Running'], ['stopped', 'Stopped'], ['crashed', 'Crashed'],
               ['outdated', 'Update available']],
  images: [['', 'All'], ['used', 'In use'], ['unused', 'Unused'], ['dangling', 'Dangling']],
  volumes: [['', 'All'], ['used', 'In use'], ['unused', 'Unused']],
  networks: [['', 'All'], ['used', 'In use'], ['unused', 'Unused']],
};

function _ctrContainerMatches(c) {
  const q = ctrState.q.toLowerCase();
  if (q && ![c.name, c.image, c.stack, c.service].some(v => (v || '').toLowerCase().includes(q))) return false;
  const state = (c.state || '').toLowerCase();
  const code = _ctrExitCode(c);
  switch (ctrState.tab === 'containers' ? ctrState.chip : '') {
    case 'running': return state === 'running';
    case 'stopped': return state !== 'running' && !(state === 'dead' || (code !== null && code !== 0));
    case 'crashed': return state === 'dead' || (state !== 'running' && code !== null && code !== 0);
    case 'outdated': return !!c.outdated;
    default: return true;
  }
}

async function renderDockerContainers(hostId, fromCache) {
  const wrap = document.getElementById('docker-stacks');
  const countEl = document.getElementById('docker-count');
  if (!wrap) return;
  if (!fromCache || !ctrState.raw || ctrState.raw.hostId !== hostId) {
    const managed = typeof renderManagedStacks === 'function' ? renderManagedStacks(hostId) : null;
    let fetched = [], stackRows = [];
    try {
      const [resp, stackResp] = await Promise.all([
        fetch(`/api/v1/hosts/${hostId}/containers/`, { credentials: 'same-origin' }),
        fetch(`/api/v1/hosts/${hostId}/stacks/`, { credentials: 'same-origin' }),
      ]);
      if (resp.ok) fetched = await resp.json();
      if (stackResp.ok) stackRows = await stackResp.json();
    } catch { fetched = []; }
    await managed;   // the Edit buttons need the managed stacks
    if (hostId !== containersHostId) return;   // the operator moved on
    ctrState.raw = { hostId, containers: fetched, stacks: stackRows };
  }
  const editButton = (name) => (typeof managedStackEditButton === 'function' ? managedStackEditButton(name) : '');
  const allContainers = ctrState.raw.containers;
  const stacks = ctrState.raw.stacks;
  const containers = allContainers.filter(_ctrContainerMatches);
  const filtering = !!(ctrState.q || ctrState.chip);
  const tabCount = document.getElementById('ctr-n-containers');
  if (tabCount) tabCount.textContent = allContainers.length || '';
  const stackInfo = Object.fromEntries((stacks || []).map(s => [s.project, s]));

  // A stack Vigil deployed or adopted keeps its row after `down` or after its
  // containers are removed: it is still there, just down, so it stays listed.
  const allDown = (stacks || []).filter(s => s.ownership !== 'external'
    && !allContainers.some(c => c.stack === s.project));
  if (typeof setListedStacks === 'function') {
    const withContainers = [...new Set(allContainers.map(c => c.stack).filter(Boolean))];
    setListedStacks([...withContainers, ...allDown.map(s => s.project)], withContainers);
  }
  // A down stack has no containers to match a state chip; it shows under
  // All and Stopped, and when the filter text names it.
  const downStacks = allDown.filter(s => (!ctrState.chip || ctrState.chip === 'stopped')
    && (!ctrState.q || s.project.toLowerCase().includes(ctrState.q.toLowerCase())));
  if (!containers.length && !downStacks.length) {
    countEl.textContent = '';
    wrap.innerHTML = `<div class="docker-empty">${filtering && allContainers.length
      ? 'No containers match this filter.' : 'No containers reported for this host.'}</div>`;
    return;
  }
  countEl.textContent = filtering
    ? `${containers.length} of ${allContainers.length} containers`
    : (containers.length === 1 ? '1 container' : `${containers.length} containers`);

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
        ${stack ? editButton(stack) : ''}
        <span class="docker-stack-count">${rows.length} ${noun}</span>
        ${stack ? _stackStateBadge(rows) : ''}
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
        <td>${_ctrStateBadge(c)}</td>
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
  for (const s of downStacks) {
    html += `<div class="docker-stack">
      <div class="docker-stack-header">
        <span class="docker-stack-name">${escHtml(s.project)}</span>
        ${editButton(s.project)}
        <span class="docker-stack-count">0 containers</span>
        <span class="ctr-state stopped">down</span>
        <span class="chip chip-muted apps-src">${escHtml(s.ownership)}</span>
      </div>
      <div class="docker-empty">No containers reported. Just deployed? They appear after the host's next check-in. Otherwise it was taken down or its containers were removed; Edit it to deploy it again.</div>
    </div>`;
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


/* ── Images, volumes, networks (14.3) ────────────────────────────────── */
// One fetch per host (and per refresh) feeds all three tabs. What uses each
// object comes from the containers' own edges; removing anything goes
// through the built-in tasks (signed, TOTP-confirmed), never straight here.

async function loadDockerResources(hostId) {
  let body = null;
  try { body = await apiJson(`/api/v1/hosts/${encodeURIComponent(hostId)}/docker/`); } catch { body = null; }
  if (hostId !== containersHostId) return;
  ctrState.docker = body;
  for (const kind of ['images', 'volumes', 'networks']) {
    const el = document.getElementById(`ctr-n-${kind}`);
    if (el) el.textContent = body && body[kind].length ? body[kind].length : '';
  }
  if (ctrState.tab !== 'containers') _renderCtrPanel();
}

function _ctrUsed(kind, item) {
  if (kind === 'images') return item.used_by.length > 0;
  if (kind === 'volumes') return item.used_by.length > 0;
  return item.containers.length > 0;
}

function _ctrResourceMatches(kind, item) {
  const q = ctrState.q.toLowerCase();
  const text = kind === 'images' ? [item.short_id, ...item.tags, ...item.used_by]
    : kind === 'volumes' ? [item.name, item.stack, ...item.used_by.map(u => u.container)]
      : [item.name, item.stack, item.driver, ...item.subnets, ...item.containers.map(c => c.container)];
  if (q && !text.some(v => (v || '').toLowerCase().includes(q))) return false;
  switch (ctrState.chip) {
    case 'used': return _ctrUsed(kind, item);
    case 'unused': return !_ctrUsed(kind, item) && !(kind === 'networks' && item.builtin);
    case 'dangling': return kind === 'images' && item.dangling;
    default: return true;
  }
}

function _bytes(n) { return n === null || n === undefined ? '—' : formatBytes(n); }

function _renderStorageStrip() {
  const strip = document.getElementById('ctr-storage');
  const show = ctrState.tab === 'images' || ctrState.tab === 'volumes';
  strip.hidden = !show;
  if (!show) return;
  const u = ctrState.docker && ctrState.docker.usage;
  const measure = `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Measure container storage">${u ? 'Measure again' : 'Measure storage'}</button>`;
  if (!u) {
    strip.innerHTML = `<span class="ctr-storage-note">Volume sizes and what pruning would free need a measurement:
      the engine walks every volume, so it runs when you ask.</span>${measure}`;
    return;
  }
  const cell = (label, size, free) => `<div class="ctr-storage-cell"><span class="ctr-storage-label">${label}</span>
      <span class="ctr-storage-size">${_bytes(size)}</span>
      ${free ? `<span class="ctr-storage-free">${_bytes(free)} reclaimable</span>` : ''}</div>`;
  strip.innerHTML = cell('Images', u.images_size, u.images_reclaimable)
    + cell('Volumes', u.volumes_size, u.volumes_reclaimable)
    + cell('Containers', u.containers_size, 0)
    + cell('Build cache', u.build_cache_size, u.build_cache_reclaimable)
    + `<span class="ctr-storage-note">measured ${escHtml(timeAgo(u.read_at) || '')}</span>${measure}`;
}

function _renderCtrChips() {
  const wrap = document.getElementById('ctr-filter-chips');
  wrap.innerHTML = CTR_CHIPS[ctrState.tab].map(([v, label]) => `<button type="button"
      class="chip ctr-chip${ctrState.chip === v ? ' is-on' : ' chip-muted'}" data-ctr-chip="${escAttr(v)}"
      aria-pressed="${ctrState.chip === v}">${escHtml(label)}</button>`).join('');
  const acts = {
    containers: '',
    images: `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune dangling images">Prune dangling</button>
             <button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune unused images">Prune unused</button>`,
    volumes: `<button class="btn btn-outline btn-xs ctr-danger" type="button" data-ctr-task="Prune unused volumes">Prune unused…</button>`,
    networks: `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune unused networks">Prune unused</button>`,
  };
  document.getElementById('ctr-tab-actions').innerHTML = acts[ctrState.tab];
  document.getElementById('ctr-filter-q').placeholder = {
    containers: 'Filter by name, image or stack…', images: 'Filter by tag, id or container…',
    volumes: 'Filter by name, stack or container…', networks: 'Filter by name, subnet or container…',
  }[ctrState.tab];
}

function _usedBy(list) {
  return list.length ? list.map(x => `<span class="chip chip-muted">${escHtml(x)}</span>`).join(' ')
    : '<span class="ctr-unused">unused</span>';
}

function _renderImages(items) {
  return `<table class="ctr-table ctr-res-table ctr-img-table"><thead><tr><th>Image</th><th>ID</th><th class="num">Size</th>
      <th>Created</th><th>Used by</th><th></th></tr></thead><tbody>${items.map(i => `<tr>
      <td>${i.tags.length ? i.tags.map(t => `<div class="ctr-name">${escHtml(t)}</div>`).join('') : '<span class="ctr-unused">&lt;none&gt; dangling</span>'}</td>
      <td class="ctr-image">${escHtml(i.short_id)}</td>
      <td class="ctr-stat">${_bytes(i.size)}</td>
      <td class="ctr-image">${escHtml(i.created ? timeAgo(i.created) : '')}</td>
      <td>${_usedBy(i.used_by)}</td>
      <td class="ctr-fix">${i.used_by.length ? '' : `<button class="btn btn-xs btn-outline" type="button"
          data-ctr-task="Remove image" data-ctr-input="image" data-ctr-value="${escAttr(i.tags[0] || i.id)}">Remove</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderVolumes(items) {
  return `<table class="ctr-table ctr-res-table ctr-vol-table"><thead><tr><th>Volume</th><th>Stack</th><th class="num">Size</th>
      <th>Mounted by</th><th>Driver</th><th></th></tr></thead><tbody>${items.map(v => `<tr>
      <td><div class="ctr-name">${escHtml(v.name)}</div><div class="ctr-svc">${escHtml(v.mountpoint)}</div></td>
      <td>${escHtml(v.stack || '')}</td>
      <td class="ctr-stat">${_bytes(v.size)}</td>
      <td>${v.used_by.length ? v.used_by.map(u => `<span class="chip chip-muted" title="${escAttr(u.destination)}${u.rw ? '' : ' (read-only)'}">${escHtml(u.container)}</span>`).join(' ') : '<span class="ctr-unused">unused</span>'}</td>
      <td class="ctr-image">${escHtml(v.driver)}</td>
      <td class="ctr-fix">${v.used_by.length ? '' : `<button class="btn btn-xs btn-outline ctr-danger" type="button"
          data-ctr-task="Remove volume" data-ctr-input="volume" data-ctr-value="${escAttr(v.name)}">Remove…</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderNetworks(items) {
  return `<table class="ctr-table ctr-res-table ctr-net-table"><thead><tr><th>Network</th><th>Driver</th><th>Subnet</th>
      <th>Containers</th><th>Stack</th><th></th></tr></thead><tbody>${items.map(n => `<tr>
      <td><div class="ctr-name">${escHtml(n.name)}</div>${n.internal ? '<div class="ctr-svc">internal</div>' : ''}</td>
      <td class="ctr-image">${escHtml(n.driver)}${n.scope && n.scope !== 'local' ? ' · ' + escHtml(n.scope) : ''}</td>
      <td class="ctr-image ctr-subnets">${n.subnets.map(x => `<div title="${escAttr(x)}">${escHtml(x)}</div>`).join('')}</td>
      <td>${n.containers.length ? n.containers.map(c => `<span class="chip chip-muted">${escHtml(c.container)}${c.ip ? ' <span class="ctr-ip">' + escHtml(c.ip) + '</span>' : ''}</span>`).join(' ') : '<span class="ctr-unused">none</span>'}</td>
      <td>${escHtml(n.stack || '')}</td>
      <td class="ctr-fix">${n.containers.length || n.builtin ? (n.builtin ? '<span class="ctr-unused">engine</span>' : '')
        : `<button class="btn btn-xs btn-outline" type="button" data-ctr-task="Remove network" data-ctr-input="network"
             data-ctr-value="${escAttr(n.name)}">Remove</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderCtrPanel() {
  if (!containersHostId) return;   // nothing chosen yet: the empty state shows
  _renderStorageStrip();
  const kind = ctrState.tab;
  if (kind === 'containers') { renderDockerContainers(containersHostId, true); return; }
  const el = document.getElementById(`ctr-${kind}`);
  const body = ctrState.docker;
  if (!body) { el.innerHTML = '<div class="docker-empty">Loading…</div>'; return; }
  const all = body[kind];
  const items = all.filter(item => _ctrResourceMatches(kind, item));
  const countEl = document.getElementById('docker-count');
  countEl.textContent = items.length === all.length ? `${all.length} ${kind}` : `${items.length} of ${all.length} ${kind}`;
  if (!all.length) {
    el.innerHTML = `<div class="docker-empty">No ${kind} reported yet. The host sends them with its next check-in
      (agent 2026.14.3 or newer).</div>`;
    return;
  }
  el.innerHTML = items.length
    ? (kind === 'images' ? _renderImages(items) : kind === 'volumes' ? _renderVolumes(items) : _renderNetworks(items))
    : `<div class="docker-empty">No ${kind} match this filter.</div>`;
}

function setCtrTab(tab) {
  ctrState.tab = tab;
  ctrState.chip = '';
  document.querySelectorAll('[data-ctr-tab]').forEach((t) => {
    const on = t.dataset.ctrTab === tab;
    t.classList.toggle('is-on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('[data-ctr-panel]').forEach((p) => { p.hidden = p.dataset.ctrPanel !== tab; });
  _renderCtrChips();
  _renderCtrPanel();
  try { sessionStorage.setItem('vigil-ctr-tab', tab); } catch (e) { /* private window */ }
}

delegateClick('[data-ctr-pick-host]', () => pickContainersHost());
delegateClick('[data-ctr-host]', (el) => selectContainersHost(el.dataset.ctrHost));
delegateClick('[data-ctr-tab]', (el) => setCtrTab(el.dataset.ctrTab));
delegateClick('[data-ctr-chip]', (el) => {
  ctrState.chip = el.dataset.ctrChip;
  _renderCtrChips();
  _renderCtrPanel();
});
delegateClick('[data-ctr-task]', (el) => {
  if (!containersHostId) return;
  const inputs = el.dataset.ctrInput ? { [el.dataset.ctrInput]: el.dataset.ctrValue } : {};
  openBuiltinTask(el.dataset.ctrTask, containersHostId, inputs);
});

let _ctrFilterTimer = null;
document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'ctr-filter-q') return;
  clearTimeout(_ctrFilterTimer);
  _ctrFilterTimer = setTimeout(() => { ctrState.q = ev.target.value.trim(); _renderCtrPanel(); }, 120);
});

document.addEventListener('DOMContentLoaded', () => {
  let tab = 'containers';
  try { tab = sessionStorage.getItem('vigil-ctr-tab') || 'containers'; } catch (e) { /* private window */ }
  if (CTR_CHIPS[tab]) setCtrTab(tab);
});
