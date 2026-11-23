// @ts-check
// vigil-host-cards.js
// Owns: Dashboard host cards, host detail drawer, host CRUD actions,
//   pin bar (Monitor page), and the host dropdown selector.
// HTML: templates/pages/_dashboard.html (host cards), templates/pages/_monitor.html
//   (pin bar + dropdown), and the host detail drawer in templates/base.html.
// Depends on: vigil-utils.js (apiPost, apiJson, showToast, escHtml,
//   formatBytes, _formatBytesPerSec, groupByLabel, computeRates),
//   vigil-deploy.js (_deployHostCache invalidated on delete).
// API: GET/PATCH /api/v1/hosts/{id}/, GET /api/v1/metrics/{id}/...,
//   POST /api/v1/hosts/{id}/{approve,reject}/, DELETE /api/v1/hosts/{id}/

/** @typedef {{id: string, hostname: string, os: string, ip: string, kernel: string, status: string,
 *  mode: string, modeDisplay: string, lastCheckin: string, agentVersion: string, tags: string,
 *  siteName: string, siteId: string}} HostCardDataset */

/* ── Host detail panel ───────────────────────────────────────────────── */
/** @type {HTMLElement} */
const overlay = document.getElementById('detail-overlay');
/** @type {HTMLElement} */
const panel = document.getElementById('detail-panel');

/** @param {HTMLElement & {dataset: HostCardDataset}} card @returns {void} */
function openHostDetail(card) {
  const d = card.dataset;
  /** @type {HTMLElement} */ (document.getElementById('detail-hostname')).textContent = d.hostname;
  /** @type {HTMLElement} */ (document.getElementById('detail-meta')).textContent =
    d.os + ' · ' + d.ip + (d.kernel ? ' · ' + d.kernel : '');

  const logoEl = document.getElementById('detail-os-logo');
  if (logoEl) {
    logoEl.replaceChildren();
    logoEl.appendChild(osLogoNode(d.os));
  }

  const dot = /** @type {HTMLElement} */ (document.getElementById('detail-status-dot'));
  dot.className = 'status-dot ' + d.status;

  const statusNames = {online: t('Online'), offline: t('Offline'), pending: t('Pending Enrollment')};
  /** @type {HTMLElement} */ (document.getElementById('detail-status-text')).textContent = statusNames[d.status] || d.status;

  const badge = /** @type {HTMLElement} */ (document.getElementById('detail-mode-badge'));
  badge.textContent = d.modeDisplay;
  badge.className = 'mode-badge mode-' + d.mode;

  /** @type {HTMLElement} */ (document.getElementById('detail-last-checkin')).textContent =
    d.lastCheckin ? t('Recent') : t('Never');

  // Agent version — colour mint when it matches the server's expected
  // version, peach when it's behind (the same signal the "Agent
  // outdated" alert uses). Paint instantly from the (possibly stale)
  // card data, then refresh from the API so a just-updated agent shows
  // its new version without a full page reload.
  _renderDetailAgentVersion(d.agentVersion || '');
  // A previous host's update line must never show on the next host; the API
  // refresh below paints the real one.
  _renderDetailAgentUpdate(null);
  _refreshDetailAgentVersion(d.id);

  // Force-update control: only meaningful when the host actually runs
  // tasks (monitor-mode agents never will).
  const updateBtn = /** @type {HTMLElement} */ (document.getElementById('detail-update-agent-btn'));
  if (updateBtn) updateBtn.style.display = (d.mode === 'monitor') ? 'none' : '';

  // Quick actions based on mode
  const actions = /** @type {HTMLElement} */ (document.getElementById('detail-actions'));
  if (d.mode === 'monitor') {
    actions.innerHTML = `<div class="detail-actions-note">${escHtml(t('This host is in monitor mode — no task execution.'))}</div>`;
  } else {
    actions.innerHTML = `
      <button class="btn btn-sm btn-sky" disabled>
        <svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
        ${escHtml(t('Restart Service'))}
      </button>
      <button class="btn btn-sm btn-outline" disabled>${escHtml(t('Clear Temp Files'))}</button>
      <button class="btn btn-sm btn-outline" disabled>${escHtml(t('Run Updates'))}</button>
      ${d.mode === 'full_control' ? `<button class="btn btn-sm btn-rose" disabled>${escHtml(t('Reboot'))}</button>` : ''}
      <button class="btn btn-sm btn-rose" id="detail-rebuild-btn">${escHtml(t('Rebuild OS…'))}</button>
    `;
    // Rebuild is offered wherever tasks run. Whether this particular agent
    // will accept it depends on allow_reprovision in its own agent.yml, and
    // whether this user may ask depends on the reprovision capability — the
    // server enforces both; this button only starts the conversation.
    const rebuildBtn = document.getElementById('detail-rebuild-btn');
    if (rebuildBtn) {
      rebuildBtn.onclick = () => openRebuildModal(d.id, d.hostname);
    }
  }

  // Store host identity on the panel for the delete button.
  panel.dataset.hostId = d.id;
  panel.dataset.hostname = d.hostname;

  overlay.classList.add('open');
  panel.classList.add('open');

  // Tags — initial render from card data, then refresh from API.
  _renderDetailTags(d.id, (d.tags || '').split(',').filter(Boolean));
  _refreshDetailTags(d.id);
  _loadDetailDependsOn(d.id);

  // Fetch live metrics for this host
  loadDetailMetrics(d.id);

  // Security score — hidden until we know the host has a summary.
  _loadDetailScore(d.id);

  // What Jackil links to this host — its own section, hidden until asked.
  loadJackilRelated('host', d.id);
}

/* ── "Related in Jackil" (the drawer) ─────────────────────────────────── */

// A helpdesk outage, a missing key or an old Jackil are all normal states of
// this section: the view shows the reason or gets out of the way, and never
// fails because another server did.
//
// `stillCurrent()` is the caller's staleness check — it returns true while the
// view still shows `id`, since in flight the drawer, the Monitor page or the
// run modal may have moved on to something else.
async function renderJackilRelated(section, body, kind, id, stillCurrent) {
  if (!section || !body) return;
  let data = null;
  try {
    const resp = await fetch(`/api/v1/jackil/related/?type=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`,
      { credentials: 'same-origin' });
    if (resp.ok) data = await resp.json();
  } catch { /* state-ok: no answer at all is the same as an unreachable Jackil — the section hides itself */ }

  if (!stillCurrent()) return;
  if (!data || data.configured === false) { section.style.display = 'none'; return; }

  section.style.display = '';
  if (!data.available) {
    body.innerHTML = `<div class="muted-span">${escHtml(data.message || '')}</div>`;
    return;
  }

  const links = data.links || [];
  const items = data.items || [];
  if (!links.length && !items.length) {
    body.innerHTML = `<div class="muted-span">${escHtml(t('Nothing in Jackil is linked to this {kind} yet.', { kind }))}</div>`;
    return;
  }

  let html = '';
  for (const link of links) {
    const status = link.status
      ? `<div class="jackil-link-status" data-css="color: ${_jackilToneColor(link.tone)};">${escHtml(link.status)}</div>`
      : '';
    html += `<div>${escHtml(link.relation)} · ${_jackilLink(link.url, link.label)}</div>${status}`;
  }
  if (items.length) {
    html += `<div class="jackil-related-label">${escHtml(t('Jackil Items'))}</div>`;
    for (const item of items) {
      html += `<div>${_jackilLink(item.url, item.name)}</div>`;
    }
  }
  body.innerHTML = html;
}

// Two-line wrapper for the host drawer.
function loadJackilRelated(kind, id) {
  renderJackilRelated(document.getElementById('detail-jackil-section'), document.getElementById('detail-jackil-body'),
    kind, id, () => panel.dataset.hostId === String(id));
}

function _jackilToneColor(tone) {
  return {
    ok: 'var(--mint-ink)',
    bad: 'var(--rose-ink)',
    warn: 'var(--peach-ink)',
    info: 'var(--sky-ink)',
  }[tone] || 'var(--text-3)';
}

// An empty url is the endpoint's way of saying "this one is not a link" —
// Jackil sends whatever its agents pasted, and only http(s) may be an href.
function _jackilLink(url, label) {
  const text = escHtml(label || '');
  return url
    ? `<a class="jackil-link" href="${escAttr(url)}" target="_blank" rel="noopener">${text}</a>`
    : text;
}

async function forceUpdateAgent() {
  const hostId = panel.dataset.hostId;
  if (!hostId) return;
  const totp = await totpPrompt(t('Force the agent to self-update on this host. It applies on the next check-in.'));
  if (!totp) return;
  try {
    const resp = await fetch(`/api/v1/hosts/${hostId}/update-agent/`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrfToken() },
      body: JSON.stringify({ totp }),
    });
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 201) {
      showToast(t('Agent update queued — applies on the next check-in'), 'success');
    } else if (resp.status === 409) {
      showToast(t('An agent update is already queued for this host'), 'info');
    } else {
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
  } catch (e) {
    showToast(t('Update failed: {message}', { message: e.message }), 'error');
  }
}

// Expected agent version, fetched once from /api/v1/about/ and cached.
let _expectedAgentVersion = null;
async function _getExpectedAgentVersion() {
  if (_expectedAgentVersion !== null) return _expectedAgentVersion;
  try {
    const resp = await fetch('/api/v1/about/', { credentials: 'same-origin' });
    _expectedAgentVersion = resp.ok ? ((await resp.json()).expected_agent_version || '') : '';
  } catch {
    _expectedAgentVersion = '';
  }
  return _expectedAgentVersion;
}

async function _refreshDetailAgentVersion(hostId) {
  try {
    const resp = await fetch(`/api/v1/hosts/${hostId}/`, { credentials: 'same-origin' });
    if (!resp.ok) return;
    const data = await resp.json();
    // Guard against a drawer switch while this was in flight.
    if (panel.dataset.hostId !== hostId) return;
    _renderDetailAgentVersion(data.agent_version || '');
    _renderDetailAgentUpdate(data.agent_update || null);
  } catch { /* state-ok: the version line keeps the value painted from card data */ }
}

async function _renderDetailAgentVersion(reported) {
  const el = document.getElementById('detail-agent-version');
  if (!el) return;
  if (!reported) {
    el.textContent = t('Unknown');
    el.style.color = 'var(--text-3)';
    return;
  }
  el.textContent = 'v' + reported;
  const expected = await _getExpectedAgentVersion();
  if (expected && isOlderVersion(reported, expected)) {
    el.textContent = t('v{reported} (outdated → v{expected})', { reported, expected });
    el.style.color = 'var(--peach-ink)';
  } else if (expected) {
    el.style.color = 'var(--mint-ink)';
  } else {
    el.style.color = 'var(--text-1)';
  }
}

/** How the agent's last self-update ended (sprint M8). Text only: every value
 *  came from the agent. @param {{state?: string, from_version?: string, to_version?: string, reason?: string, at?: string}|null} u */
function _renderDetailAgentUpdate(u) {
  const el = document.getElementById('detail-agent-update');
  if (!el) return;
  el.replaceChildren();
  el.className = 'detail-agent-update';
  if (!u || !u.state) { el.hidden = true; return; }
  // `at` arrives verbatim from the agent, so an unparseable one reads as
  // "no time to show" — same contract timeAgo gives everywhere else.
  const when = u.at ? timeAgo(u.at) : '';
  const head = document.createElement('span');
  if (u.state === 'confirmed') {
    // An old success is not news.
    if (_isStaleUpdateStamp(u.at)) { el.hidden = true; return; }
    el.classList.add('detail-agent-update--ok');
    head.textContent = t('Updated to v{version}', { version: u.to_version || '?' }) + (when ? ` ${when}` : '');
  } else if (u.state === 'rolled_back') {
    el.classList.add('detail-agent-update--bad');
    head.textContent = t('Update to v{to} rolled back to v{from}', { to: u.to_version || '?', from: u.from_version || '?' })
      + (when ? ` ${when}` : '');
  } else if (u.state === 'not_applied') {
    el.classList.add('detail-agent-update--bad');
    head.textContent = t('Update to v{version} did not take effect', { version: u.to_version || '?' })
      + (when ? ` ${when}` : '');
  } else {
    el.hidden = true;
    return;
  }
  el.appendChild(head);
  if (u.reason && u.state !== 'confirmed') {
    const reason = document.createElement('span');
    reason.className = 'detail-agent-update-reason';
    reason.textContent = String(u.reason);
    el.appendChild(reason);
  }
  el.hidden = false;
}

/** True when `at` is older than a week — the window in which a self-update
 *  still counts as news. Missing or unparseable stamps are not stale: with no
 *  date the server could not age the record out either. */
function _isStaleUpdateStamp(at) {
  if (!at) return false;
  const then = new Date(at);
  if (Number.isNaN(then.getTime())) return false;
  return Date.now() - then.getTime() > 7 * 24 * 60 * 60 * 1000;
}

async function _loadDetailScore(hostId) {
  const section = document.getElementById('detail-score-section');
  if (!section) return;
  try {
    const resp = await fetch(`/api/v1/vulns/?host=${hostId}`, { credentials: 'same-origin' });
    if (!resp.ok) { section.style.display = 'none'; return; }
    const rows = await resp.json();
    if (!rows.length) { section.style.display = 'none'; return; }
    const s = rows[0];
    section.style.display = '';
    const scoreEl = document.getElementById('detail-score');
    const scanEl = document.getElementById('detail-last-scan');
    if (scoreEl) {
      scoreEl.textContent = s.score;
      scoreEl.style.color = _scoreColor(s.score);
    }
    if (scanEl) scanEl.textContent = s.last_scan_at
      ? new Date(s.last_scan_at).toLocaleString()
      : 'never';
  } catch { /* state-ok: a score is an optional extra on the drawer; with no answer it stays hidden */ }
}

// Shared with vigil-vulns.js — same color tiers as the SVG face.
function _scoreColor(score) {
  if (score <= -40) return 'var(--coral-ink)';
  if (score <= 29) return 'var(--rose-ink)';
  if (score <= 49) return 'var(--peach-ink)';
  if (score <= 69) return 'var(--lemon-ink)';
  return 'var(--mint-ink)';
}

const _detailTagState = { hostId: null, tags: [] };

function _renderDetailTags(hostId, tags) {
  _detailTagState.hostId = hostId;
  _detailTagState.tags = (tags || []).slice();
  const wrap = document.getElementById('detail-tags-row');
  wrap.replaceChildren();
  if (!_detailTagState.tags.length) {
    const empty = document.createElement('div');
    empty.className = 'confirm-hint';
    empty.textContent = t('No tags yet.');
    wrap.appendChild(empty);
    return;
  }
  for (const tag of _detailTagState.tags) {
    const isAuto = /^(os|os_family|pkg|arch):/.test(tag);
    const chip = document.createElement('span');
    chip.className = 'host-tag-chip' + (isAuto ? ' host-tag-auto' : '');
    if (isAuto) {
      chip.title = t('Auto-classified from inventory — recomputed on every check-in.');
      chip.textContent = tag;
    } else {
      chip.style.cursor = 'pointer';
      chip.title = t('Click to remove');
      chip.textContent = tag + '  ✕';
      chip.addEventListener('click', () => _removeDetailTag(tag));
    }
    wrap.appendChild(chip);
  }
}

async function _refreshDetailTags(hostId) {
  try {
    const host = await apiJson(`/api/v1/hosts/${hostId}/`);
    _renderDetailTags(hostId, host.tags || []);
  } catch (e) { /* state-ok: the card's own tags are already drawn in the drawer */ }
}

/* ── Depends on (the drawer) ──────────────────────────────────────────── */

// Which host the drawer is showing, so a slow answer for a host the user has
// already left is dropped instead of painted over the current one.
const _detailDependsState = { hostId: null, value: '' };

async function _loadDetailDependsOn(hostId) {
  _detailDependsState.hostId = hostId;
  const select = /** @type {HTMLSelectElement|null} */ (document.getElementById('detail-depends-on'));
  if (!select) return;
  // Never leave the previous host's choice showing while this one loads.
  const placeholder = (text) => {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = text;
    select.replaceChildren(opt);
  };
  // A select cannot hold the shared loading block, so its one option says it.
  placeholder(t('Fetching the hosts…'));
  select.disabled = true;
  let host;
  let all;
  try {
    [host, all] = await Promise.all([apiJson(`/api/v1/hosts/${hostId}/`), fetchAllHosts()]);
  } catch (e) {
    if (_detailDependsState.hostId === hostId) placeholder(t('Could not load the hosts'));
    return;
  }
  if (_detailDependsState.hostId !== hostId) return;
  const current = host.depends_on ? String(host.depends_on) : '';
  // Only hosts of the same site can be chosen (the server refuses others).
  const choices = all.filter(h => String(h.id) !== String(hostId) && (h.site || null) === (host.site || null))
    .sort((a, b) => String(a.hostname).localeCompare(String(b.hostname)));
  select.replaceChildren();
  const none = document.createElement('option');
  none.value = '';
  none.textContent = t('Nothing');
  select.appendChild(none);
  for (const h of choices) {
    const opt = document.createElement('option');
    opt.value = String(h.id);
    opt.textContent = h.hostname;
    select.appendChild(opt);
  }
  // A parent this user cannot list still shows, by id, rather than vanishing.
  if (current && !choices.some(h => String(h.id) === current)) {
    const opt = document.createElement('option');
    opt.value = current;
    opt.textContent = current;
    select.appendChild(opt);
  }
  select.value = current;
  _detailDependsState.value = current;
  select.disabled = false;
}

/** @param {HTMLSelectElement} select */
async function saveDetailDependsOn(select) {
  const hostId = _detailDependsState.hostId;
  if (!hostId) return;
  const before = _detailDependsState.value;
  const wanted = select.value || null;
  select.disabled = true;
  try {
    const host = await apiJson(`/api/v1/hosts/${hostId}/depends-on/`, {
      method: 'PATCH',
      body: JSON.stringify({ depends_on: wanted }),
    });
    _detailDependsState.value = host.depends_on ? String(host.depends_on) : '';
    showToast(wanted ? t('Dependency saved') : t('Dependency cleared'), 'success');
  } catch (e) {
    select.value = before;
    showToast(t('Could not save: {message}', { message: e.message }), 'error');
  } finally {
    select.disabled = false;
  }
}

async function _saveDetailTags(tags) {
  if (!_detailTagState.hostId) return;
  try {
    const host = await apiJson(`/api/v1/hosts/${_detailTagState.hostId}/tags/`, {
      method: 'PATCH',
      body: JSON.stringify({ tags }),
    });
    _renderDetailTags(_detailTagState.hostId, host.tags || []);
    showToast(t('Tags updated'), 'success');
  } catch (e) {
    showToast(t('Failed to update tags: {message}', { message: e.message }), 'error');
  }
}

function _removeDetailTag(tag) {
  const next = _detailTagState.tags.filter(t => t !== tag);
  _saveDetailTags(next);
}

/** Add one tag to the open host and save (the drawer's tag combobox, vigil-tags.js). */
async function addDetailTag(name) {
  const v = String(name || '').trim().toLowerCase();
  if (!v || _detailTagState.tags.includes(v)) return;
  await _saveDetailTags([..._detailTagState.tags, v]);
}

let detailChartCpu = null;
let detailChartNet = null;

async function loadDetailMetrics(hostId) {
  const from = new Date(Date.now() - 60 * 60000).toISOString();

  async function fetchDetailMetric(category, metric) {
    try {
      const resp = await fetch(`/api/v1/metrics/${hostId}/${category}/${metric}/?from=${from}&limit=200`, { credentials: 'same-origin' });
      if (!resp.ok) return [];
      return await resp.json();
    } catch { return []; }
  }

  const [cpuData, memData, diskData, swapData, netRecv, netSent] = await Promise.all([
    fetchDetailMetric('cpu', 'usage_percent'),
    fetchDetailMetric('memory', 'usage_percent'),
    fetchDetailMetric('disk', 'usage_percent'),
    fetchDetailMetric('memory', 'swap_usage_percent'),
    fetchDetailMetric('network', 'bytes_recv'),
    fetchDetailMetric('network', 'bytes_sent'),
  ]);

  // Update resource cards
  const metricCards = panel.querySelectorAll('.metric-card');
  const metrics = [
    { data: cpuData, filter: { core: 'total' } },
    { data: memData, filter: null },
    { data: diskData, filter: null },
    { data: swapData, filter: null },
  ];

  metrics.forEach((m, i) => {
    if (i >= metricCards.length) return;
    let pts = m.data;
    if (m.filter) {
      pts = pts.filter(p => {
        for (const [k, v] of Object.entries(m.filter)) {
          if ((p.labels || {})[k] !== v) return false;
        }
        return true;
      });
    }
    const card = /** @type {HTMLElement} */ (metricCards[i]);
    const valueEl = /** @type {HTMLElement} */ (card.querySelector('.metric-card-value'));
    const fillEl = /** @type {HTMLElement} */ (card.querySelector('.metric-card-fill'));
    if (pts.length) {
      const latest = pts.sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime())[0];
      const pct = Math.round(latest.value);
      valueEl.textContent = pct + '%';
      fillEl.style.width = pct + '%';
    } else {
      valueEl.textContent = '—';
      fillEl.style.width = '0%';
    }
  });

  // CPU History chart
  const cpuCanvas = /** @type {HTMLCanvasElement} */ (document.getElementById('detail-chart-cpu'));
  if (detailChartCpu) detailChartCpu.destroy();
  const cpuFiltered = cpuData
    .filter(p => (p.labels || {}).core === 'total')
    .map(p => ({ x: new Date(p.time), y: p.value }))
    .sort((a, b) => a.x - b.x);
  detailChartCpu = new Chart(cpuCanvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [{
        label: t('CPU'),
        data: cpuFiltered,
        borderColor: '#82c4ee',
        backgroundColor: '#82c4ee18',
        fill: true,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: { type: 'time', grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: { min: 0, max: 100, grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { callback: v => v + '%', maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
      },
      plugins: { tooltip: { backgroundColor: tok('--s1'), borderColor: tok('--border'), borderWidth: 1, callbacks: { label: ctx => ctx.parsed.y.toFixed(1) + '%' } } },
    }
  });

  // Network I/O chart
  const netCanvas = /** @type {HTMLCanvasElement} */ (document.getElementById('detail-chart-net'));
  if (detailChartNet) detailChartNet.destroy();
  const recvSorted = netRecv.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());
  const sentSorted = netSent.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());
  const recvByIface = groupByLabel(recvSorted, 'interface');
  const sentByIface = groupByLabel(sentSorted, 'interface');
  const iface = Object.keys(recvByIface)[0] || Object.keys(sentByIface)[0];
  const recvRates = iface ? computeRates(recvByIface[iface] || []) : [];
  const sentRates = iface ? computeRates(sentByIface[iface] || []) : [];
  detailChartNet = new Chart(netCanvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [
        { label: t('Received'), data: recvRates, borderColor: '#82c4ee', backgroundColor: '#82c4ee18', fill: true },
        { label: t('Sent'), data: sentRates, borderColor: '#f0b888', backgroundColor: '#f0b88818', fill: true },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: { type: 'time', grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: { grid: { color: tok('--s3'), drawTicks: false }, border: { color: tok('--s3') }, ticks: { maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 }, callback: v => formatBytes(v) } },
      },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: tok('--s1'), borderColor: tok('--border'), borderWidth: 1, callbacks: { label: ctx => ctx.dataset.label + ': ' + formatBytes(ctx.parsed.y) } } },
    }
  });
}

function closeHostDetail() {
  overlay.classList.remove('open');
  panel.classList.remove('open');
  if (detailChartCpu) { detailChartCpu.destroy(); detailChartCpu = null; }
  if (detailChartNet) { detailChartNet.destroy(); detailChartNet = null; }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeHostDetail();
});


/* ── Approve / reject / delete actions ───────────────────────────────── */
async function approveHost(hostId, btn) {
  const totp = await totpPrompt(t('Approve this host.'));
  if (!totp) return;
  try {
    btn.disabled = true;
    await apiJson(`/api/v1/hosts/${hostId}/approve/`, {
      method: 'POST',
      body: JSON.stringify({ totp }),
    });
    showToast(t('Host approved'), 'success');
    btn.closest('.enroll-card').style.opacity = '0.4';
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast(t('Failed: {message}', { message: e.message }), 'error');
    btn.disabled = false;
  }
}

async function rejectHost(hostId, btn) {
  try {
    btn.disabled = true;
    await apiPost(`/api/v1/hosts/${hostId}/reject/`);
    showToast(t('Host rejected'), 'success');
    btn.closest('.enroll-card').style.opacity = '0.4';
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast(t('Failed: {message}', { message: e.message }), 'error');
    btn.disabled = false;
  }
}

async function deleteHost(hostId, hostname, btn) {
  if (!hostId) return;
  // Two-step confirm: first click arms, second click fires.
  if (btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    const orig = btn.innerHTML;
    btn.innerHTML = btn.innerHTML.replace(/Remove.*/, 'Confirm remove?');
    btn.style.background = 'rgba(242,139,130,.18)';
    setTimeout(() => {
      btn.dataset.armed = '0';
      btn.innerHTML = orig;
      btn.style.background = '';
    }, 3000);
    return;
  }
  try {
    btn.disabled = true;
    const resp = await fetch(`/api/v1/hosts/${hostId}/`, {
      method: 'DELETE',
      headers: { 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
    });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || t('Delete failed'));
    showToast(t('{hostname} removed', { hostname }), 'success');
    closeHostDetail();
    // Remove the card from the DOM immediately.
    const card = document.querySelector(`.host-card[data-id="${hostId}"]`);
    if (card) card.remove();
    // Invalidate fleet cache so the deploy modal won't show the deleted host.
    _deployHostCache = null;
  } catch (e) {
    showToast(t('Failed: {message}', { message: e.message }), 'error');
    btn.disabled = false;
  }
}

/* ── Pinned hosts (localStorage) ─────────────────────────────────────── */
const PIN_KEY = 'vigil-pinned-hosts';

function getPins() {
  try { return JSON.parse(localStorage.getItem(PIN_KEY)) || []; }
  catch { return []; /* state-ok: a blocked storage area simply leaves nothing pinned */ }
}

function savePins(pins) {
  localStorage.setItem(PIN_KEY, JSON.stringify(pins));
}

function isPinned(hostId) {
  return getPins().some(p => p.id === hostId);
}

function togglePin(hostId, hostname, status, btnEl) {
  let pins = getPins();
  const idx = pins.findIndex(p => p.id === hostId);
  if (idx >= 0) {
    pins.splice(idx, 1);
    if (btnEl) btnEl.classList.remove('pinned');
  } else {
    pins.push({ id: hostId, hostname, status });
    if (btnEl) btnEl.classList.add('pinned');
  }
  savePins(pins);
  renderPinBar();
}

/* The pin button in the host dropdown used to carry
   onclick="togglePin('{{ host.hostname }}', …)". Django escapes the quote in
   that hostname to &#x27;, and the browser turns it back into a live quote
   when it parses the attribute — so the autoescaping produced the injection
   rather than preventing it. The button now carries no handler at all and the
   values come from the dropdown item's existing data-* attributes, which is
   the pattern base.html already uses for the detail-panel delete button. */
document.addEventListener('click', (/** @type {MouseEvent} */ ev) => {
  const target = /** @type {HTMLElement} */ (ev.target);
  const btn = /** @type {HTMLElement} */ (target.closest && target.closest('.hdi-pin'));
  if (!btn) return;
  ev.stopPropagation();
  const item = /** @type {HTMLElement} */ (btn.closest('.host-dropdown-item'));
  if (!item) return;
  const { hostId, hostname, status } = item.dataset;
  togglePin(hostId, hostname, status, btn);
});

function renderPinBar() {
  const bar = document.getElementById('pin-bar');
  const selectorWrap = bar.querySelector('.host-selector-wrap');
  // Remove old chips
  bar.querySelectorAll('.pin-chip').forEach(c => c.remove());
  // Add chips before the selector
  const pins = getPins();
  pins.forEach(pin => {
    const chip = document.createElement('div');
    // vigil-monitor.js declares monitorHostId and loads after this file, so it
    // does not exist yet on the initial render below.
    const selected = typeof monitorHostId !== 'undefined' ? monitorHostId : null;
    chip.className = 'pin-chip' + (pin.id === selected ? ' selected' : '');
    const dotColor = pin.status === 'online' ? 'var(--mint)' : pin.status === 'pending' ? 'var(--peach)' : 'var(--rose)';

    // Built element by element rather than as an HTML string. A hostname is
    // whatever an agent reported, and this used to interpolate it twice into
    // markup — once as raw HTML, once inside a single-quoted JS string in an
    // onclick attribute. escHtml would not have saved the second one: the
    // escaping there is for the wrong context, because the browser decodes the
    // attribute before the JS parser ever sees it. textContent and a real
    // listener have no such context to get wrong.
    const dot = document.createElement('div');
    dot.className = 'pin-dot';
    dot.style.background = dotColor;
    chip.appendChild(dot);
    chip.appendChild(document.createTextNode(pin.hostname));

    const remove = document.createElement('button');
    remove.className = 'pin-remove';
    remove.innerHTML = '&times;';
    remove.addEventListener('click', (ev) => {
      ev.stopPropagation();
      togglePin(pin.id, pin.hostname, pin.status, null);
    });
    chip.appendChild(remove);

    chip.addEventListener('click', () => selectMonitorHost(pin.id));
    bar.insertBefore(chip, selectorWrap);
  });
  // Sync pin buttons in dropdown
  document.querySelectorAll('.hdi-pin').forEach(btn => {
    const item = /** @type {HTMLElement} */ (btn.closest('.host-dropdown-item'));
    if (item && isPinned(item.dataset.hostId)) btn.classList.add('pinned');
    else btn.classList.remove('pinned');
  });
}

/* ── Host dropdown selector (used by Monitor page) ───────────────────── */
function toggleHostDropdown(e) {
  e.stopPropagation();
  document.getElementById('host-dropdown').classList.toggle('open');
}

document.addEventListener('click', e => {
  const dd = document.getElementById('host-dropdown');
  if (dd && !dd.contains(/** @type {Node} */ (e.target)) && /** @type {HTMLElement} */ (e.target).id !== 'host-selector-btn') {
    dd.classList.remove('open');
  }
});

document.querySelectorAll('.host-dropdown-item').forEach((/** @type {HTMLElement} */ item) => {
  item.addEventListener('click', () => {
    selectMonitorHost(item.dataset.hostId);
    document.getElementById('host-dropdown').classList.remove('open');
  });
});

/* ── Live host card metrics + filter ─────────────────────────────────── */
// `root` scopes the refresh to one subtree — the dashboard's host widget
// renders the same cards, and refreshing the whole document from there would
// re-fetch every metric series for every card on the hosts page as well.
// N is per label set: the newest reading of each core and mount, and the last
// two of each interface (a rate needs two samples).
const _CARD_SERIES = 'cpu/usage_percent:1,memory/usage_percent:1,disk/usage_percent:1,'
                   + 'network/bytes_recv:2,network/bytes_sent:2';

// One request carries the five series for up to this many hosts; beyond it the
// URL would grow past what a proxy will accept, so ask in chunks.
const _CARD_CHUNK = 500;

async function _fetchCardMetrics(ids) {
  const from = new Date(Date.now() - 5 * 60000).toISOString();
  const all = {};
  for (let i = 0; i < ids.length; i += _CARD_CHUNK) {
    const chunk = ids.slice(i, i + _CARD_CHUNK);
    const resp = await fetch(`/api/v1/metrics/recent/?series=${_CARD_SERIES}`
      + `&from=${encodeURIComponent(from)}` + chunk.map(id => `&host=${encodeURIComponent(id)}`).join(''),
      { credentials: 'same-origin' });
    if (!resp.ok) throw new Error(`metrics/recent failed: HTTP ${resp.status}`);
    Object.assign(all, await resp.json());
  }
  return all;
}

async function refreshHostCards(/** @type {HTMLElement=} */ root) {
  // Skip cards inside the collapsed inactive section — no live data needed.
  const cards = [.../** @type {NodeListOf<HTMLElement & {dataset: HostCardDataset}>} */ ((root || document).querySelectorAll('.host-card:not([data-inactive="1"])'))];
  const ids = cards.filter(c => c.dataset.id && c.dataset.status === 'online').map(c => c.dataset.id);
  if (!ids.length) return;
  let all = {};
  try {
    all = await _fetchCardMetrics(ids);
  } catch (e) { /* state-ok: a card keeps the dashes it renders with; there is no list to draw over */ }

  for (const card of cards) {
    const hostId = card.dataset.id;
    if (!hostId || card.dataset.status !== 'online') continue;
    const s = all[hostId] || {};
    const cpuData = s['cpu/usage_percent'] || [];
    const memData = s['memory/usage_percent'] || [];
    const diskData = s['disk/usage_percent'] || [];
    const netInData = s['network/bytes_recv'] || [];
    const netOutData = s['network/bytes_sent'] || [];
    const cpuEl = card.querySelector('[data-metric="cpu"]');
    const memEl = card.querySelector('[data-metric="memory"]');
    const diskEl = card.querySelector('[data-metric="disk"]');
    const netInEl = card.querySelector('[data-metric="net-in"]');
    const netOutEl = card.querySelector('[data-metric="net-out"]');

    const cpuPts = cpuData.filter(p => (p.labels || {}).core === 'total');
    if (cpuPts.length && cpuEl) {
      const v = Math.round(cpuPts[0].value);
      /** @type {HTMLElement} */ (cpuEl.querySelector('.host-metric-value')).textContent = v + '%';
      /** @type {HTMLElement} */ (cpuEl.querySelector('.host-metric-fill')).style.width = v + '%';
    }
    if (memData.length && memEl) {
      const v = Math.round(memData[0].value);
      /** @type {HTMLElement} */ (memEl.querySelector('.host-metric-value')).textContent = v + '%';
      /** @type {HTMLElement} */ (memEl.querySelector('.host-metric-fill')).style.width = v + '%';
    }
    if (diskData.length && diskEl) {
      // Aggregate latest disk usage across mounts — show the highest %.
      const max = diskData.reduce((m, p) => Math.max(m, p.value || 0), 0);
      const v = Math.round(max);
      /** @type {HTMLElement} */ (diskEl.querySelector('.host-metric-value')).textContent = v + '%';
      /** @type {HTMLElement} */ (diskEl.querySelector('.host-metric-fill')).style.width = v + '%';
    }

    // Network rates require two adjacent samples. Sum across interfaces
    // (excluding loopback) to give a top-level fleet view.
    function _rate(points) {
      if (!points || points.length < 2) return 0;
      const byIface = {};
      for (const p of points) {
        const iface = (p.labels || {}).interface || '_';
        if (!byIface[iface]) byIface[iface] = [];
        byIface[iface].push(p);
      }
      let total = 0;
      for (const samples of Object.values(byIface)) {
        samples.sort((a,b) => new Date(a.time).getTime() - new Date(b.time).getTime());
        if (samples.length < 2) continue;
        const a = samples[samples.length - 2];
        const b = samples[samples.length - 1];
        const dt = (new Date(b.time).getTime() - new Date(a.time).getTime()) / 1000;
        if (dt > 0) total += Math.max(0, (b.value - a.value) / dt);
      }
      return total;
    }
    const inRate = _rate(netInData);
    const outRate = _rate(netOutData);
    if (netInEl) {
      /** @type {HTMLElement} */ (netInEl.querySelector('.host-metric-value')).textContent = _formatBytesPerSec(inRate);
      const pct = Math.min(100, Math.round((inRate / (1024*1024)) * 100));
      /** @type {HTMLElement} */ (netInEl.querySelector('.host-metric-fill')).style.width = pct + '%';
    }
    if (netOutEl) {
      /** @type {HTMLElement} */ (netOutEl.querySelector('.host-metric-value')).textContent = _formatBytesPerSec(outRate);
      const pct = Math.min(100, Math.round((outRate / (1024*1024)) * 100));
      /** @type {HTMLElement} */ (netOutEl.querySelector('.host-metric-fill')).style.width = pct + '%';
    }
  }
}

// Inject OS logos into server-rendered host cards.
// osLogoNode() builds an <img> whose src comes from a fixed slug list, so
// nothing here parses or interpolates the host-supplied OS string.
function _injectOsLogos(root) {
  (root || document).querySelectorAll('.host-os-logo[data-os]').forEach(el => {
    if (el.firstChild) return;
    el.appendChild(osLogoNode(el.dataset.os));
  });
}
_injectOsLogos();

// Refresh host cards on page load
refreshHostCards();

function filterHostCards(query) {
  const q = (query || '').trim().toLowerCase();
  for (const card of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('.host-card'))) {
    if (!q) {
      card.classList.remove('hidden');
      continue;
    }
    const hay = [
      card.dataset.hostname || '',
      card.dataset.os || '',
      card.dataset.ip || '',
      card.dataset.tags || '',
      card.dataset.modeDisplay || '',
    ].join(' ').toLowerCase();
    card.classList.toggle('hidden', !hay.includes(q));
  }
  // Auto-expand the inactive section when a search is active so matches
  // there don't stay hidden behind a collapsed details element.
  const inactive = /** @type {HTMLDetailsElement} */ (document.getElementById('inactive-hosts-section'));
  if (inactive) inactive.open = !!q;
}

// ── Card-level quick actions ──
function downloadHostRdp(hostId, hostname) {
  const url = `/api/v1/hosts/${hostId}/rdp/`;
  const a = document.createElement('a');
  a.href = url;
  a.download = (hostname || 'host') + '.rdp';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// The Connect menu's lazily loaded session assets (below). Declared ahead of
// the initial render so nothing that render throws can leave them in the
// temporal dead zone, which breaks every Connect click until a reload.
let _termAssets = null;
let _rdpAssets = null;

// Initial render of pinned-hosts bar.
renderPinBar();

/* ── The Connect menu (QA-58) ──────────────────────────────────────────────
   One button per host, and a server-side answer to "which of these can I
   actually use?" Every greyed item carries its reason: an operator left to
   guess why an item is dead files the bug against the wrong subsystem. Nothing
   here decides availability locally — _start()'s checks are the authority and
   /sessions/options/ repeats them, so the menu cannot promise a shell the API
   would refuse. */

//: The vendored terminal, on demand. base.html does not carry it: xterm is
//: ~260 KB that only someone about to open a session needs. Resolved once, in
//: the order the scripts require (xterm, its fit addon, then ours).
function loadTerminalAssets() {
  if (_termAssets) return _termAssets;
  _termAssets = (async () => {
    const src = document.getElementById('vigil-term-assets');
    if (!src) throw new Error('terminal assets not found');
    _appendStylesheet(src.dataset.xtermCss);
    await _appendScript(src.dataset.xterm);
    await _appendScript(src.dataset.fit);
    await _appendScript(src.dataset.js);
    return true;
  })().catch((err) => { _termAssets = null; throw err; });
  return _termAssets;
}

//: The desktop's vendored Guacamole, on demand, exactly like the terminal's:
//: one script here and ours, in that order — vigil-rdp.js needs Guacamole to
//: exist when its tunnel reaches for a state constant.
function loadRdpAssets() {
  if (_rdpAssets) return _rdpAssets;
  _rdpAssets = (async () => {
    const src = document.getElementById('vigil-term-assets');
    if (!src) throw new Error('remote desktop assets not found');
    await _appendScript(src.dataset.guac);
    await _appendScript(src.dataset.rdp);
    return true;
  })().catch((err) => { _rdpAssets = null; throw err; });
  return _rdpAssets;
}

function _appendStylesheet(href) {
  if (!href || document.querySelector('link[data-term-css]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  link.setAttribute('data-term-css', '');
  document.head.appendChild(link);
}

//: Resolved on load rather than error, so one unreadable vendor file fails with
//: a named toast instead of "Terminal is not defined" three steps later.
function _appendScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.onload = () => resolve(src);
    el.onerror = () => reject(new Error(`could not load ${src}`));
    document.body.appendChild(el);
  });
}

function _closeConnectMenu() {
  const open = document.querySelector('.connect-menu');
  if (open) open.remove();
}

async function openConnectMenu(hostId, hostname, anchorEl) {
  _closeConnectMenu();
  let opts;
  try {
    opts = await apiJson(`/api/v1/sessions/options/?host=${encodeURIComponent(hostId)}`);
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }

  const menu = document.createElement('div');
  menu.className = 'connect-menu';
  menu.setAttribute('role', 'menu');

  const always = { available: true, reason: '' };
  const addRow = (label, item, run) => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'connect-item' + (item.available ? '' : ' connect-item-off');
    row.setAttribute('role', 'menuitem');
    row.innerHTML = `<span class="connect-item-label">${escHtml(label)}</span>`
      + (item.available ? ''
        : `<span class="connect-item-reason">${escAttr(item.reason || '')}</span>`);
    if (item.available) {
      row.addEventListener('click', () => { _closeConnectMenu(); run(); });
    } else {
      // aria-disabled rather than [disabled]: a disabled button swallows the
      // hover that reveals why, and "why is this grey" is the whole question.
      row.setAttribute('aria-disabled', 'true');
      row.title = item.reason || '';
    }
    menu.appendChild(row);
  };

  addRow(t('Web shell'), opts.shell, () => {
    loadTerminalAssets()
      .then(() => openSession(hostId, 'shell'))
      .catch((err) => showToast(err.message, 'error'));
  });
  // Windows serves RDP itself, Linux through xrdp, and a Mac's desktop is its
  // own Screen Sharing (VNC): the same session either way, named for what it is.
  addRow(opts.os_family === 'macos' ? t('Screen sharing') : t('Web RDP'), opts.rdp, () => {
    // vigil-host-cards.js is loaded by base.html; guacamole-common.js and
    // vigil-rdp.js only when a desktop is first opened, so the reference to
    // openRdpSession has to stay dynamic. The item is offered only when
    // /sessions/options/ reported rdp available — agent switch on, guacd
    // configured — the one path that can reach it.
    loadRdpAssets()
      .then(() => { if (typeof openRdpSession === 'function') openRdpSession(hostId); })
      .catch((err) => showToast(err.message, 'error'));
  });
  if (opts.is_windows) {
    addRow(t('Download .rdp file'), always,
      () => downloadHostRdp(hostId, hostname || opts.host));
  } else {
    addRow(t('SSH command'), always,
      () => openSshFor(hostname || opts.host, opts.ssh_target));
  }
  if (opts.recorded) {
    const note = document.createElement('div');
    note.className = 'connect-note';
    note.textContent = t('Sessions on this install are recorded.');
    menu.appendChild(note);
  }

  // Two frames before measuring: setting .open is what makes the dialog
  // display:block, so a rect taken any earlier has no width to place by.
  document.body.appendChild(menu);
  menu.classList.add('open');
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const at = anchorEl.getBoundingClientRect();
  const box = menu.getBoundingClientRect();
  // Fixed, so it outlives any overflow:hidden ancestor — host cards clip.
  let left = Math.max(8, Math.min(at.left, window.innerWidth - box.width - 8));
  let top = at.bottom + 4;
  if (top + box.height > window.innerHeight - 8) {
    top = Math.max(8, at.top - box.height - 4);
  }
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;

  const dismiss = () => {
    _closeConnectMenu();
    document.removeEventListener('mousedown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const onDown = (ev) => { if (!menu.contains(ev.target)) dismiss(); };
  const onKey = (ev) => { if (ev.key === 'Escape') dismiss(); };
  // Capture phase, registered after this click has opened the menu: the
  // pointerdown that got us here would otherwise close it again immediately.
  setTimeout(() => {
    document.addEventListener('mousedown', onDown, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}


/* The detail-panel delete button used to reach into the DOM from inside its
   own onclick attribute. Named here so the attribute is just a name. */
function deleteHostFromDetailPanel(btn) {
  const panel = document.getElementById('detail-panel');
  if (!panel) return;
  deleteHost(panel.dataset.hostId, panel.dataset.hostname, btn);
}

document.getElementById('detail-depends-on')?.addEventListener('change', (e) => saveDetailDependsOn(e.target));
