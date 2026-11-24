// @ts-check
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
/** The page's state beyond the host: the open tab, the filter bar, and what
 *  was last fetched (so typing in the filter redraws without refetching).
 *  `containers` and `stacks` are the raw rows of /hosts/{id}/containers/ and
 *  /hosts/{id}/stacks/ for the selected host; `docker` is the whole
 *  /hosts/{id}/docker/ payload. */
/** @type {{tab: string, q: string, chip: string,
 *  hosts: {id: string, hostname: string, containers: number|null, running: number|null, engine?: string}[]|null,
 *  raw: {hostId: any, containers: any[], stacks: any[]}|null,
 *  docker: any}} */
const ctrState = { tab: 'containers', q: '', chip: '', hosts: null, raw: null, docker: null };

/* ── Host selection ──────────────────────────────────────────────────── */
/** @param {boolean=} force @returns {Promise<{id: string, hostname: string, containers: number|null, running: number|null, engine?: string}[]>} */
async function _ctrHosts(force) {
  if (!ctrState.hosts || force) {
    try { ctrState.hosts = await apiJson('/api/v1/hosts/docker-summary/'); } catch (e) {
      // Said out loud: an empty picker alone would read as "no Docker hosts".
      showToast(t('Could not load the Docker hosts: {message}', { message: e.message }), 'error');
      ctrState.hosts = ctrState.hosts || [];
    }
  }
  return ctrState.hosts;
}

/** The hostname for an id, for labels elsewhere (the stack editor's crumbs). */
/** @param {*=} hostId @returns {string} */
function containersHostLabel(hostId) {
  const h = (ctrState.hosts || []).find(x => x.id === hostId);
  return h ? h.hostname : '';
}

/** `hostId` is whatever the page's data attribute holds: an id, or '' for none. */
/** @param {*=} hostId @returns {void} */
function selectContainersHost(hostId) {
  const input = /** @type {HTMLInputElement} */ (document.getElementById('containers-host'));
  if (containersHostId !== (hostId || null)) { ctrState.raw = null; ctrState.docker = null; }
  containersHostId = hostId || null;
  if (input) input.value = hostId || '';
  const label = /** @type {HTMLElement|null} */ (document.getElementById('containers-host-label'));
  if (label) label.textContent = containersHostLabel(hostId) || (hostId ? t('Host') : t('Choose a host…'));
  /** @type {HTMLElement} */ (document.getElementById('containers-empty')).hidden = !!containersHostId;
  /** @type {HTMLElement} */ (document.getElementById('containers-content')).hidden = !containersHostId;
  _renderCtrPins();
  refreshContainers();
  if (hostId && !containersHostLabel(hostId)) {
    _ctrHosts(true).then(() => { if (label) label.textContent = containersHostLabel(hostId) || t('Host'); });
  }
}

/** @returns {Promise<void>} */
async function pickContainersHost() {
  const hosts = await _ctrHosts(true);
  openPicker({
    type: 'machine', title: t('Pick a host'), allowAdd: false,
    items: hosts.map(h => ({
      key: h.id, name: h.hostname,
      meta: h.containers
        ? `${tn('{n} container', '{n} containers', h.containers)} (${t('{n} running', { n: h.running })})${h.engine ? ' · ' + h.engine : ''}`
        : t('no containers reported'),
      raw: h,
    })),
    onSelect: (item) => selectContainersHost(item.key),
  });
}

/** Pinned hosts (the Monitor page's pins) as one-click chips. */
/** @returns {void} */
function _renderCtrPins() {
  const wrap = /** @type {HTMLElement|null} */ (document.getElementById('containers-pins'));
  if (!wrap) return;
  const pins = typeof getPins === 'function' ? getPins() : [];
  wrap.innerHTML = pins.slice(0, 8).map(p => `<button type="button" class="chip ctr-pin${p.id === containersHostId ? ' is-on' : ''}"
      data-ctr-host="${escAttr(p.id)}">${escHtml(p.hostname || '')}</button>`).join('');
}

/** @returns {void} */
function refreshContainers() {
  if (!containersHostId) return;
  renderDockerContainers(containersHostId);
  loadDockerResources(containersHostId);
}

// Open the Containers page on a given host — the Monitor page's Containers
// button lands here with the host it was showing.
/** @param {*=} hostId @returns {void} */
function openContainersForHost(hostId) {
  navigateTo('containers');
  if (hostId) selectContainersHost(hostId);
}

/** @returns {void} */
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
window.navigateTo = function(pageName) {
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
//: The agent's own state words. The page shows them as it receives them; the
//: CSS colours each one by this same id.
const CTR_STATES = ['running', 'exited', 'dead', 'paused', 'restarting', 'created'];  // i18n-ignore

/* Docker's status text carries the exit code ("Exited (137) 2 hours ago").
   Code 0 is a clean stop; anything else (or "dead") is a crash, and the two
   must not look the same. */
/** @param {{status?: string|null}} c @returns {number|null} */
function _ctrExitCode(c) {
  const m = /exited \((-?\d+)\)/i.exec(c.status || '');
  return m ? parseInt(m[1], 10) : null;
}

/** @param {{state?: string|null, status?: string|null}} c @returns {string} */
function _ctrStateBadge(c) {
  const state = (c.state || '').toLowerCase();
  const code = _ctrExitCode(c);
  // A state the agent reports that we have no chip for: the word is part of
  // the data, so it stands alone; "unknown" is ours to translate.
  let text = state || t('unknown');
  let cls = CTR_STATES.includes(state) ? state : '';  // i18n-ignore
  if (state === 'exited' && code !== null) {
    text = code === 0 ? t('stopped (exit 0)') : t('crashed (exit {code})', { code });
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
  const exitNote = codes.length ? ` (exit ${codes.join(', ')})` : '';  // i18n-ignore: an exit code, not a sentence
  let text, cls;
  if (running === rows.length) {
    // The word a person reads; the class beside it is a state id, not text.
    text = t('running');
    cls = 'running';  // i18n-ignore
  } else if (running === 0) {
    text = crashed.length ? t('crashed{note}', { note: exitNote }) : t('stopped (exit 0)');
    cls = crashed.length ? 'exited' : 'stopped';  // i18n-ignore
  } else {
    text = tn('{n}/{total} running', '{n}/{total} running', running, { total: rows.length })
      + (crashed.length ? ` · ${tn('{n} crashed{note}', '{n} crashed{note}', crashed.length, { note: exitNote })}` : '');
    cls = 'partial';  // i18n-ignore
  }
  return `<span class="ctr-state ${cls}">${escHtml(text)}</span>`;
}

/* The Containers tab's quick filters: the ids a click stores (data-ctr-chip). */
const CTR_CHIPS = {  // i18n-ignore
  containers: ['', 'running', 'stopped', 'crashed', 'outdated'],  // i18n-ignore
  images: ['', 'used', 'unused', 'dangling'],  // i18n-ignore
  volumes: ['', 'used', 'unused'],  // i18n-ignore
  networks: ['', 'used', 'unused'],  // i18n-ignore
};

//: Each filter id with the words its chip shows.
function ctrChipChoices(tab) {
  const words = {
    '': t('All'), running: t('Running'), stopped: t('Stopped'), crashed: t('Crashed'),
    outdated: t('Update available'), used: t('In use'), unused: t('Unused'), dangling: t('Dangling'),
  };
  return CTR_CHIPS[tab].map((value) => [value, words[value]]);
}

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
    } catch (e) { showLoadFailure(wrap, t('this host\'s containers'), e, () => renderDockerContainers(hostId)); return; }
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
  if (tabCount) tabCount.textContent = /** @type {string} */ (allContainers.length || '');
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
  // A host with nothing at all gets New stack under its empty message, where
  // the eye already is, instead of in the bar above it.
  const hostIsEmpty = !allContainers.length && !allDown.length;
  if (typeof setContainersEmpty === 'function') setContainersEmpty(hostIsEmpty);
  if (!containers.length && !downStacks.length) {
    countEl.textContent = '';
    wrap.innerHTML = `<div class="docker-empty">${filtering && allContainers.length
      ? escHtml(t('No containers match this filter.')) : escHtml(t('No containers reported for this host.'))}</div>`
      + (hostIsEmpty && typeof setContainersEmpty === 'function'
        ? `<div class="docker-empty-act"><button class="btn btn-sm btn-accent" type="button" data-stack-new>${escHtml(t('New stack'))}</button></div>` : '');
    return;
  }
  countEl.textContent = filtering
    ? t('{shown} of {total} containers', { shown: containers.length, total: allContainers.length })
    : tn('{n} container', '{n} containers', containers.length);

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
    const label = stack || t('Ungrouped');
    // A stack header counts its own rows; tn() with that number is the only
    // form under which the English below reads exactly as it did.
    const noun = tn('container', 'containers', rows.length);
    const info = stackInfo[stack];
    const stackMeta = info ? `<span class="chip chip-muted apps-src" title="${escAttr((info.config_files || []).join(', '))}">${escHtml(info.ownership)}</span>${info.engine ? `<span class="chip chip-muted apps-src">${escHtml(info.engine)}</span>` : ''}` : '';
    // A stack from Git updates by redeploying: its images are built from the repo, not pulled.
    const gitStack = stack && typeof managedGitStack === 'function' ? managedGitStack(stack) : null;
    const stackActs = stack ? `<span class="docker-stack-acts">
        <button class="btn btn-xs btn-outline" data-stack-act="Restart stack" data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}">${t('Restart')}</button>
        ${gitStack
          ? `<button class="btn btn-xs btn-outline" data-stack-git-update="${escAttr(gitStack.id)}" title="${escAttr(t('Fetch the latest commit, rebuild and redeploy'))}">${t('Update')}</button>`
          : `<button class="btn btn-xs btn-outline" data-stack-act="Update stack" data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}">${t('Update')}</button>`}
        ${info && info.ownership === 'external' ? `<button class="btn btn-xs btn-lav" data-stack-adopt data-host="${escAttr(hostId)}" data-project="${escAttr(stack)}" title="${escAttr(t('Let Vigil manage this stack where it stands — nothing is recreated'))}">${t('Adopt')}</button>` : ''}
      </span>` : '';
    html += `<div class="docker-stack">
      <div class="docker-stack-header">
        <span class="docker-stack-name">${escHtml(label)}</span>
        ${stack ? editButton(stack) : ''}
        <span class="docker-stack-count">${rows.length} ${escHtml(noun)}</span>
        ${stack ? _stackStateBadge(rows) : ''}
        ${stackMeta}${stackActs}
      </div>
      <table class="ctr-table">
        <colgroup>
          <col class="ctr-c-name"><col class="ctr-c-image"><col class="ctr-c-state">
          <col class="ctr-c-cpu"><col class="ctr-c-mem"><col class="ctr-c-acts">
        </colgroup>
        <thead><tr>
          <th>${t('Name')}</th><th>${t('Image')}</th><th>${t('State')}</th>
          <th class="num">${t('CPU')}</th><th class="num">${t('Memory')}</th><th></th>
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
        <td class="ctr-fix">${c.outdated ? `<button class="btn btn-xs btn-accent" data-ctr-update data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}" title="${escAttr(t('A newer image is available'))}">${t('Update')}</button>` : ''}
          ${state === 'running'
            ? `<button class="btn btn-xs btn-outline" data-ctr-act="Restart container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">${t('Restart')}</button>
               <button class="btn btn-xs btn-outline" data-ctr-act="Stop container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">${t('Stop')}</button>`
            : `<button class="btn btn-xs btn-outline" data-ctr-act="Start container" data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">${t('Start')}</button>`}
          <button class="btn btn-xs btn-outline" data-ctr-shell data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}" title="${escAttr(t('Open a shell in this container'))}">${t('Shell')}</button>
          <button class="btn btn-xs btn-outline" data-ctr-logs data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}">${t('Logs')}</button>
          ${c.previous_image && !c.rolled_back ? `<button class="btn btn-xs btn-outline" data-ctr-rollback data-host="${escAttr(hostId)}" data-name="${escAttr(c.name || '')}" data-image="${escAttr(c.previous_image)}" title="${escAttr(t('Go back to {image}', { image: c.previous_image }))}">${t('Roll back')}</button>` : ''}
          ${c.rolled_back ? `<span class="chip apps-chip-warn" title="${escAttr(t('Pinned to {image} — the next update clears it', { image: c.rolled_back }))}">${t('rolled back')}</span>` : ''}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }
  for (const s of downStacks) {
    // A down stack reports none, so its count is plural — "0 containers".
    const noun = tn('container', 'containers', 0);
    html += `<div class="docker-stack">
      <div class="docker-stack-header">
        <span class="docker-stack-name">${escHtml(s.project)}</span>
        ${editButton(s.project)}
        <span class="docker-stack-count">0 ${escHtml(noun)}</span>
        <span class="ctr-state stopped">${t('down')}</span>
        <span class="chip chip-muted apps-src">${escHtml(s.ownership)}</span>
      </div>
      <div class="docker-empty">${escHtml(t("No containers reported. Just deployed? They appear after the host's next check-in. Otherwise it was taken down or its containers were removed; Edit it to deploy it again."))}</div>
    </div>`;
  }
  wrap.innerHTML = html;
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-ctr-update]'))) btn.addEventListener('click', () => {
    if (typeof openUpdateContainer === 'function') {
      openUpdateContainer(btn.dataset.host, btn.dataset.name);
    }
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-ctr-act]'))) btn.addEventListener('click', () => {
    openBuiltinTask(btn.dataset.ctrAct, btn.dataset.host, { container_name: btn.dataset.name });
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-ctr-rollback]'))) btn.addEventListener('click', () => {
    openBuiltinTask('Roll back container', btn.dataset.host,  // i18n-ignore
      { container_name: btn.dataset.name, image: btn.dataset.image });
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-ctr-logs]'))) btn.addEventListener('click', () => {
    openContainerLogs(btn.dataset.host, btn.dataset.name);
  });
  // The same terminal dialog the Connect menu opens. The container's name is
  // what the agent shells into; the host id is where the session starts.
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-ctr-shell]'))) btn.addEventListener('click', () => {
    loadTerminalAssets()
      .then(() => openSession(btn.dataset.host, 'container', btn.dataset.name))
      .catch((err) => showToast(err.message, 'error'));
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-stack-adopt]'))) btn.addEventListener('click', async () => {
    const totp = await totpPrompt(t('Adopting {project} reads its compose file and .env into Vigil.', { project: btn.dataset.project }));
    if (!totp) return;
    try {
      await apiJson('/api/v1/stacks/adopt/', { method: 'POST', body: JSON.stringify(
        { host_id: btn.dataset.host, project: btn.dataset.project, totp: totp.trim() }) });
      showToast(t("Adopting {project} — it appears under Managed by Vigil after the agent's next check-in", { project: btn.dataset.project }), 'success');
    } catch (e) { showToast(e.message, 'error'); }
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-stack-act]'))) btn.addEventListener('click', () => {
    openBuiltinTask(btn.dataset.stackAct, btn.dataset.host, { project: btn.dataset.project });
  });
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (wrap.querySelectorAll('[data-stack-git-update]'))) btn.addEventListener('click', () => {
    updateGitStack(btn.dataset.stackGitUpdate);
  });
}


/* ── Live container logs (M11) ───────────────────────────────────────── */
// Opening logs is a signed task (TOTP). The agent sends the last lines, then
// streams new ones while this view polls; closing it (or 15 s without a poll)
// stops the agent.
const logView = { session: null, after: 0, timer: null, initialShown: false,
                  lines: [], ansi: null, wrap: false, timestamps: 'off' };
//: The viewer keeps this many lines; past it the oldest go, as in a terminal's
//: scrollback.
const LOG_VIEW_MAX = 5000;

function _logEls() {
  let overlay = document.getElementById('ctr-logs-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.id = 'ctr-logs-overlay';
    const modal = document.createElement('div');
    modal.className = 'modal modal-wide ctr-logs-modal';
    modal.id = 'ctr-logs-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML = `<div class="modal-title"><span id="ctr-logs-title">${t('Logs')}</span>
        <button class="modal-close" type="button" id="ctr-logs-close" aria-label="${escAttr(t('Close'))}">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button></div>
      <div class="ctr-logs-bar">
        <span class="ctr-logs-state" id="ctr-logs-state">${escHtml(t('Waiting for the agent…'))}</span>
        <button class="btn btn-outline btn-xs" type="button" id="ctr-logs-wrap" aria-pressed="false">${t('Wrap lines')}</button>
        <button class="btn btn-outline btn-xs" type="button" id="ctr-logs-ts" aria-pressed="false">${t('Timestamps')}</button>
        <button class="btn btn-outline btn-xs" type="button" id="ctr-logs-copy">${t('Copy')}</button>
      </div>
      <pre class="ctr-logs term" id="ctr-logs-body" tabindex="0"></pre>`;
    document.body.append(overlay, modal);
    overlay.addEventListener('click', closeContainerLogs);
    modal.querySelector('#ctr-logs-close').addEventListener('click', closeContainerLogs);
    modal.querySelector('#ctr-logs-wrap').addEventListener('click', (/** @type {MouseEvent} */ ev) => {
      logView.wrap = !logView.wrap;
      /** @type {HTMLElement} */ (ev.currentTarget).setAttribute('aria-pressed', String(logView.wrap));
      document.getElementById('ctr-logs-body').classList.toggle('term-wrap', logView.wrap);
    });
    modal.querySelector('#ctr-logs-ts').addEventListener('click', (/** @type {MouseEvent} */ ev) => {
      logView.timestamps = logView.timestamps === 'off' ? 'time' : 'off';
      /** @type {HTMLElement} */ (ev.currentTarget).setAttribute('aria-pressed', String(logView.timestamps === 'time'));
      _renderLogLines();
    });
    modal.querySelector('#ctr-logs-copy').addEventListener('click', _copyLogLines);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && modal.classList.contains('open')) closeContainerLogs();
    });
  }
  return { overlay, modal: document.getElementById('ctr-logs-modal'),
           body: document.getElementById('ctr-logs-body'), state: document.getElementById('ctr-logs-state') };
}

//: Redraws every kept line, for a change of how they are shown.
function _renderLogLines() {
  const body = document.getElementById('ctr-logs-body');
  if (!body) return;
  logView.ansi = termNewState();
  body.innerHTML = logView.lines.length
    ? termHtml(logView.lines.join('\n'), { timestamps: logView.timestamps }, logView.ansi) + '\n' : '';
}

//: New lines onto the end; the colour state carries on from the line before.
function _appendLogLines(lines) {
  if (!lines.length) return;
  logView.lines.push(...lines);
  if (logView.lines.length > LOG_VIEW_MAX) {
    logView.lines.splice(0, logView.lines.length - LOG_VIEW_MAX);
    _renderLogLines();
    return;
  }
  document.getElementById('ctr-logs-body').insertAdjacentHTML('beforeend',
    termHtml(lines.join('\n'), { timestamps: logView.timestamps }, logView.ansi) + '\n');
}

async function _copyLogLines() {
  const text = termPlain(logView.lines.join('\n'), { timestamps: logView.timestamps });
  try {
    await navigator.clipboard.writeText(text);
    showToast(tn('Copied {n} line', 'Copied {n} lines', logView.lines.length), 'success');
  } catch {
    showToast(t('The browser would not let the page copy; select the text instead.'), 'error');
  }
}

async function openContainerLogs(hostId, name) {
  const totp = await totpPrompt(t('Reading the logs of {name} shows everything those containers printed.', { name }));
  if (!totp) return;
  let opened;
  try {
    opened = await apiJson(`/api/v1/hosts/${hostId}/containers/${encodeURIComponent(name)}/logs/`,
      { method: 'POST', body: JSON.stringify({ totp: totp.trim(), tail: 200 }) });
  } catch (e) { showToast(e.message, 'error'); return; }
  const els = _logEls();
  Object.assign(logView, { session: opened.session, after: 0, initialShown: false,
                           lines: [], ansi: termNewState() });
  document.getElementById('ctr-logs-title').textContent = t('Logs — {name}', { name });
  els.body.textContent = '';
  els.state.textContent = t('Waiting for the agent (up to one check-in)…');
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
  } catch { return; /* state-ok: the next poll refetches the log tail */ }
  // Follow the tail only while the reader is already at the bottom; someone
  // scrolled up to read is left where they are.
  const atBottom = els.body.scrollTop + els.body.clientHeight >= els.body.scrollHeight - 8;
  if (!logView.initialShown && body.initial) {
    _appendLogLines(String(body.initial).split('\n'));
    logView.initialShown = true;
  }
  const fresh = [];
  for (const [seq, line] of body.lines) {
    fresh.push(line);
    logView.after = seq;
  }
  _appendLogLines(fresh);
  els.state.textContent = body.task_state === 'failed' ? t('The agent could not read the logs.')
    : body.task_state === 'completed'
      ? (body.live ? t('Live — new lines appear as they are written.') : t('Stopped.'))
      : t('Waiting for the agent (up to one check-in)…');
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
  try {
    body = await apiJson(`/api/v1/hosts/${encodeURIComponent(hostId)}/docker/`);
  } catch (e) {
    if (hostId !== containersHostId) return;
    ctrState.docker = null;
    if (ctrState.tab === 'containers') { renderDockerContainers(hostId); return; }
    // The panel draws the loading state while the retry is out.
    _renderCtrPanel();
    const el = document.getElementById(`ctr-${ctrState.tab}`);
    if (el) showLoadFailure(el, t('this host\'s images, volumes and networks'), e, () => loadDockerResources(hostId));
    return;
  }
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
  // data-ctr-task names the built-in task the agent runs — not text a person reads.
  const measure = `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Measure container storage">${escHtml(u ? t('Measure again') : t('Measure storage'))}</button>`;  // i18n-ignore: the data-ctr-task value
  if (!u) {
    strip.innerHTML = `<span class="ctr-storage-note">${escHtml(t('Volume sizes and what pruning would free need a measurement: the engine walks every volume, so it runs when you ask.'))}</span>${measure}`;
    return;
  }
  const cell = (label, size, free) => `<div class="ctr-storage-cell"><span class="ctr-storage-label">${escHtml(label)}</span>
      <span class="ctr-storage-size">${_bytes(size)}</span>
      ${free ? `<span class="ctr-storage-free">${t('{size} reclaimable', { size: _bytes(free) })}</span>` : ''}</div>`;
  strip.innerHTML = cell(t('Images'), u.images_size, u.images_reclaimable)
    + cell(t('Volumes'), u.volumes_size, u.volumes_reclaimable)
    + cell(t('Containers'), u.containers_size, 0)
    + cell(t('Build cache'), u.build_cache_size, u.build_cache_reclaimable)
    + `<span class="ctr-storage-note">${escHtml(t('measured {when}', { when: timeAgo(u.read_at) || '' }))}</span>${measure}`;
}

function _renderCtrChips() {
  const wrap = document.getElementById('ctr-filter-chips');
  wrap.innerHTML = ctrChipChoices(ctrState.tab).map(([v, label]) => `<button type="button"
      class="chip ctr-chip${ctrState.chip === v ? ' is-on' : ' chip-muted'}" data-ctr-chip="${escAttr(v)}"
      aria-pressed="${ctrState.chip === v}">${escHtml(label)}</button>`).join('');
  const imagesActs = `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune dangling images">${t('Prune dangling')}</button>`  // i18n-ignore
    + `
             <button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune unused images">${t('Prune unused')}</button>`;  // i18n-ignore
  const acts = {
    // The image prunes are what frees space after container churn, so the
    // tab people land on carries them too.
    containers: imagesActs,
    images: imagesActs,
    volumes: `<button class="btn btn-outline btn-xs ctr-danger" type="button" data-ctr-task="Prune unused volumes">${t('Prune unused…')}</button>`,  // i18n-ignore
    networks: `<button class="btn btn-outline btn-xs" type="button" data-ctr-task="Prune unused networks">${t('Prune unused')}</button>`,  // i18n-ignore
  };
  document.getElementById('ctr-tab-actions').innerHTML = acts[ctrState.tab];
  /** @type {HTMLInputElement} */ (document.getElementById('ctr-filter-q')).placeholder = {
    containers: t('Filter by name, image or stack…'), images: t('Filter by tag, id or container…'),
    volumes: t('Filter by name, stack or container…'), networks: t('Filter by name, subnet or container…'),
  }[ctrState.tab];
}

//: The engine's own marker for an image with no tag.
const CTR_NONE = '&lt;none&gt;';  // i18n-ignore

function _usedBy(list) {
  return list.length ? list.map(x => `<span class="chip chip-muted">${escHtml(x)}</span>`).join(' ')
    : `<span class="ctr-unused">${t('unused')}</span>`;
}


/* The tab id as words a person reads. One noun per kind, chosen by the
   sentence's own count: `total` is the whole list's phrase, `shown` the
   filtered one's. */
/** @param {string} kind @param {number} total @param {number} shown @returns {{all: string, some: string}} */
function ctrKindPhrase(kind, total, shown) {
  if (kind === 'images') {
    return { all: tn('{n} image', '{n} images', total),
             some: tn('{shown} of {n} images', '{shown} of {n} images', total, { shown }) };
  }
  if (kind === 'volumes') {
    return { all: tn('{n} volume', '{n} volumes', total),
             some: tn('{shown} of {n} volumes', '{shown} of {n} volumes', total, { shown }) };
  }
  return { all: tn('{n} network', '{n} networks', total),
           some: tn('{shown} of {n} networks', '{shown} of {n} networks', total, { shown }) };
}

//: The same noun without a count, for a sentence naming the kind.
/** @param {string} kind @returns {string} */
function ctrKindName(kind) {
  if (kind === 'images') return t('images');
  if (kind === 'volumes') return t('volumes');
  return t('networks');
}

function _renderImages(items) {
  return `<table class="ctr-table ctr-res-table ctr-img-table"><thead><tr><th>${t('Image')}</th><th>${t('ID')}</th><th class="num">${t('Size')}</th>
      <th>${t('Created')}</th><th>${t('Used by')}</th><th></th></tr></thead><tbody>${items.map(i => `<tr>
      <td>${i.tags.length ? i.tags.map(tag => `<div class="ctr-name">${escHtml(tag)}</div>`).join('') : `<span class="ctr-unused">${CTR_NONE} ${t('dangling')}</span>`}</td>
      <td class="ctr-image">${escHtml(i.short_id)}</td>
      <td class="ctr-stat">${_bytes(i.size)}</td>
      <td class="ctr-image">${escHtml(i.created ? timeAgo(i.created) : '')}</td>
      <td>${_usedBy(i.used_by)}</td>
      <td class="ctr-fix">${i.used_by.length ? '' : `<button class="btn btn-xs btn-outline" type="button"
          data-ctr-task="Remove image" data-ctr-input="image" data-ctr-value="${escAttr(i.tags[0] || i.id)}">${t('Remove')}</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderVolumes(items) {
  return `<table class="ctr-table ctr-res-table ctr-vol-table"><thead><tr><th>${t('Volume')}</th><th>${t('Stack')}</th><th class="num">${t('Size')}</th>
      <th>${t('Mounted by')}</th><th>${t('Driver')}</th><th></th></tr></thead><tbody>${items.map(v => `<tr>
      <td><div class="ctr-name">${escHtml(v.name)}</div><div class="ctr-svc">${escHtml(v.mountpoint)}</div></td>
      <td>${escHtml(v.stack || '')}</td>
      <td class="ctr-stat">${_bytes(v.size)}</td>
      <td>${v.used_by.length ? v.used_by.map(u => `<span class="chip chip-muted" title="${escAttr(u.destination)}${u.rw ? '' : ' ' + escAttr(t('(read-only)'))}">${escHtml(u.container)}</span>`).join(' ') : `<span class="ctr-unused">${t('unused')}</span>`}</td>
      <td class="ctr-image">${escHtml(v.driver)}</td>
      <td class="ctr-fix">${v.used_by.length ? '' : `<button class="btn btn-xs btn-outline ctr-danger" type="button"
          data-ctr-task="Remove volume" data-ctr-input="volume" data-ctr-value="${escAttr(v.name)}">${t('Remove…')}</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderNetworks(items) {
  return `<table class="ctr-table ctr-res-table ctr-net-table"><thead><tr><th>${t('Network')}</th><th>${t('Driver')}</th><th>${t('Subnet')}</th>
      <th>${t('Containers')}</th><th>${t('Stack')}</th><th></th></tr></thead><tbody>${items.map(n => `<tr>
      <td><div class="ctr-name">${escHtml(n.name)}</div>${n.internal ? `<div class="ctr-svc">${t('internal')}</div>` : ''}</td>
      <td class="ctr-image">${escHtml(n.driver)}${n.scope && n.scope !== 'local' ? ' · ' + escHtml(n.scope) : ''}</td>
      <td class="ctr-image ctr-subnets">${n.subnets.map(x => `<div title="${escAttr(x)}">${escHtml(x)}</div>`).join('')}</td>
      <td>${n.containers.length ? n.containers.map(c => `<span class="chip chip-muted">${escHtml(c.container)}${c.ip ? ' <span class="ctr-ip">' + escHtml(c.ip) + '</span>' : ''}</span>`).join(' ') : `<span class="ctr-unused">${t('none')}</span>`}</td>
      <td>${escHtml(n.stack || '')}</td>
      <td class="ctr-fix">${n.containers.length || n.builtin ? (n.builtin ? `<span class="ctr-unused">${t('engine')}</span>` : '')
        : `<button class="btn btn-xs btn-outline" type="button" data-ctr-task="Remove network" data-ctr-input="network"
             data-ctr-value="${escAttr(n.name)}">${t('Remove')}</button>`}</td>
    </tr>`).join('')}</tbody></table>`;
}

function _renderCtrPanel() {
  if (!containersHostId) return;   // nothing chosen yet: the empty state shows
  _renderStorageStrip();
  const kind = ctrState.tab;
  if (kind === 'containers') { renderDockerContainers(containersHostId, true); return; }
  const el = document.getElementById(`ctr-${kind}`);
  const body = ctrState.docker;
  if (!body) { el.innerHTML = loadingHtml(); return; }
  const all = body[kind];
  const items = all.filter(item => _ctrResourceMatches(kind, item));
  const countEl = document.getElementById('docker-count');
  const phrases = ctrKindPhrase(kind, all.length, items.length);
  countEl.textContent = items.length === all.length ? phrases.all : phrases.some;
  if (!all.length) {
    el.innerHTML = `<div class="docker-empty">${escHtml(t('No {kind} reported yet. The host sends them with its next check-in (agent 2026.14.3 or newer).', { kind: ctrKindName(kind) }))}</div>`;
    return;
  }
  el.innerHTML = items.length
    ? (kind === 'images' ? _renderImages(items) : kind === 'volumes' ? _renderVolumes(items) : _renderNetworks(items))
    : `<div class="docker-empty">${escHtml(t('No {kind} match this filter.', { kind: ctrKindName(kind) }))}</div>`;
}

function setCtrTab(tab) {
  ctrState.tab = tab;
  ctrState.chip = '';
  document.querySelectorAll('[data-ctr-tab]').forEach((/** @type {HTMLElement} */ t) => {
    const on = t.dataset.ctrTab === tab;
    t.classList.toggle('is-on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('[data-ctr-panel]').forEach((/** @type {HTMLElement} */ p) => { p.hidden = p.dataset.ctrPanel !== tab; });
  _renderCtrChips();
  _renderCtrPanel();
  try { sessionStorage.setItem('vigil-ctr-tab', tab); } catch (e) { /* state-ok: private window */ }
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
document.addEventListener('input', (/** @type {InputEvent} */ ev) => {
  if (/** @type {HTMLInputElement} */ (ev.target).id !== 'ctr-filter-q') return;
  clearTimeout(_ctrFilterTimer);
  _ctrFilterTimer = setTimeout(() => { ctrState.q = /** @type {HTMLInputElement} */ (ev.target).value.trim(); _renderCtrPanel(); }, 120);
});

document.addEventListener('DOMContentLoaded', () => {
  let tab = 'containers';
  try { tab = sessionStorage.getItem('vigil-ctr-tab') || 'containers'; } catch (e) { /* state-ok: private window */ }
  if (CTR_CHIPS[tab]) setCtrTab(tab);
});
