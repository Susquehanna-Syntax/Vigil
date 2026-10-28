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

/* ── Host detail panel ───────────────────────────────────────────────── */
const overlay = document.getElementById('detail-overlay');
const panel = document.getElementById('detail-panel');

function openHostDetail(card) {
  const d = card.dataset;
  document.getElementById('detail-hostname').textContent = d.hostname;
  document.getElementById('detail-meta').textContent =
    d.os + ' · ' + d.ip + (d.kernel ? ' · ' + d.kernel : '');

  const logoEl = document.getElementById('detail-os-logo');
  if (logoEl) {
    logoEl.replaceChildren();
    logoEl.appendChild(osLogoNode(d.os));
  }

  const dot = document.getElementById('detail-status-dot');
  dot.className = 'status-dot ' + d.status;

  const statusNames = {online: 'Online', offline: 'Offline', pending: 'Pending Enrollment'};
  document.getElementById('detail-status-text').textContent = statusNames[d.status] || d.status;

  const badge = document.getElementById('detail-mode-badge');
  badge.textContent = d.modeDisplay;
  badge.className = 'mode-badge mode-' + d.mode;

  document.getElementById('detail-last-checkin').textContent =
    d.lastCheckin ? 'Recent' : 'Never';

  // Agent version — colour mint when it matches the server's expected
  // version, peach when it's behind (the same signal the "Agent
  // outdated" alert uses). Paint instantly from the (possibly stale)
  // card data, then refresh from the API so a just-updated agent shows
  // its new version without a full page reload.
  _renderDetailAgentVersion(d.agentVersion || '');
  _refreshDetailAgentVersion(d.id);

  // Force-update control: only meaningful when the host actually runs
  // tasks (monitor-mode agents never will).
  const updateBtn = document.getElementById('detail-update-agent-btn');
  if (updateBtn) updateBtn.style.display = (d.mode === 'monitor') ? 'none' : '';

  // Quick actions based on mode
  const actions = document.getElementById('detail-actions');
  if (d.mode === 'monitor') {
    actions.innerHTML = '<div style="color: var(--text-3); font-size: 13px;">This host is in monitor mode — no task execution.</div>';
  } else {
    actions.innerHTML = `
      <button class="btn btn-sm btn-sky" disabled>
        <svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
        Restart Service
      </button>
      <button class="btn btn-sm btn-outline" disabled>Clear Temp Files</button>
      <button class="btn btn-sm btn-outline" disabled>Run Updates</button>
      ${d.mode === 'full_control' ? '<button class="btn btn-sm btn-rose" disabled>Reboot</button>' : ''}
      <button class="btn btn-sm btn-rose" id="detail-rebuild-btn">Rebuild OS…</button>
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

  // Fetch live metrics for this host
  loadDetailMetrics(d.id);

  // Security score — hidden until we know the host has a summary.
  _loadDetailScore(d.id);

  // What Jackil links to this host — its own section, hidden until asked.
  loadJackilRelated('host', d.id);
}

/* ── "Related in Jackil" (the drawer) ─────────────────────────────────── */

// A helpdesk outage, a missing key or an old Jackil are all normal states of
// this section: the drawer shows the reason or gets out of the way, and never
// fails because another server did.
async function loadJackilRelated(kind, id) {
  const section = document.getElementById('detail-jackil-section');
  if (!section) return;
  let data = null;
  try {
    const resp = await fetch(`/api/v1/jackil/related/?type=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`,
      { credentials: 'same-origin' });
    if (resp.ok) data = await resp.json();
  } catch { /* unreachable Vigil — same as no answer at all */ }

  // The drawer may have moved to another host while this was in flight.
  if (panel.dataset.hostId !== String(id)) return;
  if (!data || data.configured === false) { section.style.display = 'none'; return; }

  section.style.display = '';
  const body = document.getElementById('detail-jackil-body');
  if (!data.available) {
    body.innerHTML = `<div style="color: var(--text-3);">${escHtml(data.message || '')}</div>`;
    return;
  }

  const links = data.links || [];
  const items = data.items || [];
  if (!links.length && !items.length) {
    body.innerHTML = '<div style="color: var(--text-3);">Nothing in Jackil is linked to this host yet.</div>';
    return;
  }

  let html = '';
  for (const link of links) {
    const status = link.status
      ? `<div style="font-size: 11px; color: ${_jackilToneColor(link.tone)};">${escHtml(link.status)}</div>`
      : '';
    html += `<div>${escHtml(link.relation)} · ${_jackilLink(link.url, link.label)}</div>${status}`;
  }
  if (items.length) {
    html += '<div style="color: var(--text-3); font-size: 11px; text-transform: uppercase;'
          + ' letter-spacing: .08em; margin-top: 8px;">Jackil Items</div>';
    for (const item of items) {
      html += `<div>${_jackilLink(item.url, item.name)}</div>`;
    }
  }
  body.innerHTML = html;
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
    ? `<a href="${escAttr(url)}" target="_blank" rel="noopener" style="color: var(--text-1); text-decoration: none; font-weight: 500;">${text}</a>`
    : text;
}

async function forceUpdateAgent() {
  const hostId = panel.dataset.hostId;
  if (!hostId) return;
  const totp = await totpPrompt('Force the agent to self-update on this host. It applies on the next check-in.');
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
      showToast('Agent update queued — applies on the next check-in', 'success');
    } else if (resp.status === 409) {
      showToast('An agent update is already queued for this host', 'info');
    } else {
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
  } catch (e) {
    showToast('Update failed: ' + e.message, 'error');
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
  } catch { /* keep the value painted from card data */ }
}

async function _renderDetailAgentVersion(reported) {
  const el = document.getElementById('detail-agent-version');
  if (!el) return;
  if (!reported) {
    el.textContent = 'Unknown';
    el.style.color = 'var(--text-3)';
    return;
  }
  el.textContent = 'v' + reported;
  const expected = await _getExpectedAgentVersion();
  if (expected && isOlderVersion(reported, expected)) {
    el.textContent = `v${reported} (outdated → v${expected})`;
    el.style.color = 'var(--peach-ink)';
  } else if (expected) {
    el.style.color = 'var(--mint-ink)';
  } else {
    el.style.color = 'var(--text-1)';
  }
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
  } catch {
    section.style.display = 'none';
  }
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
    empty.textContent = 'No tags yet.';
    wrap.appendChild(empty);
    return;
  }
  for (const t of _detailTagState.tags) {
    const isAuto = /^(os|os_family|pkg|arch):/.test(t);
    const chip = document.createElement('span');
    chip.className = 'host-tag-chip' + (isAuto ? ' host-tag-auto' : '');
    if (isAuto) {
      chip.title = 'Auto-classified from inventory — recomputed on every check-in.';
      chip.textContent = t;
    } else {
      chip.style.cursor = 'pointer';
      chip.title = 'Click to remove';
      chip.textContent = t + '  ✕';
      chip.addEventListener('click', () => _removeDetailTag(t));
    }
    wrap.appendChild(chip);
  }
}

async function _refreshDetailTags(hostId) {
  try {
    const host = await apiJson(`/api/v1/hosts/${hostId}/`);
    _renderDetailTags(hostId, host.tags || []);
  } catch (e) { /* keep card-derived tags as fallback */ }
}

async function _saveDetailTags(tags) {
  if (!_detailTagState.hostId) return;
  try {
    const host = await apiJson(`/api/v1/hosts/${_detailTagState.hostId}/tags/`, {
      method: 'PATCH',
      body: JSON.stringify({ tags }),
    });
    _renderDetailTags(_detailTagState.hostId, host.tags || []);
    showToast('Tags updated', 'success');
  } catch (e) {
    showToast('Failed to update tags: ' + e.message, 'error');
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
    const card = metricCards[i];
    const valueEl = card.querySelector('.metric-card-value');
    const fillEl = card.querySelector('.metric-card-fill');
    if (pts.length) {
      const latest = pts.sort((a, b) => new Date(b.time) - new Date(a.time))[0];
      const pct = Math.round(latest.value);
      valueEl.textContent = pct + '%';
      fillEl.style.width = pct + '%';
    } else {
      valueEl.textContent = '—';
      fillEl.style.width = '0%';
    }
  });

  // CPU History chart
  const cpuCanvas = document.getElementById('detail-chart-cpu');
  if (detailChartCpu) detailChartCpu.destroy();
  const cpuFiltered = cpuData
    .filter(p => (p.labels || {}).core === 'total')
    .map(p => ({ x: new Date(p.time), y: p.value }))
    .sort((a, b) => a.x - b.x);
  detailChartCpu = new Chart(cpuCanvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [{
        label: 'CPU',
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
  const netCanvas = document.getElementById('detail-chart-net');
  if (detailChartNet) detailChartNet.destroy();
  const recvSorted = netRecv.sort((a, b) => new Date(a.time) - new Date(b.time));
  const sentSorted = netSent.sort((a, b) => new Date(a.time) - new Date(b.time));
  const recvByIface = groupByLabel(recvSorted, 'interface');
  const sentByIface = groupByLabel(sentSorted, 'interface');
  const iface = Object.keys(recvByIface)[0] || Object.keys(sentByIface)[0];
  const recvRates = iface ? computeRates(recvByIface[iface] || []) : [];
  const sentRates = iface ? computeRates(sentByIface[iface] || []) : [];
  detailChartNet = new Chart(netCanvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [
        { label: 'Received', data: recvRates, borderColor: '#82c4ee', backgroundColor: '#82c4ee18', fill: true },
        { label: 'Sent', data: sentRates, borderColor: '#f0b888', backgroundColor: '#f0b88818', fill: true },
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
  const totp = await totpPrompt('Approve this host.');
  if (!totp) return;
  try {
    btn.disabled = true;
    await apiJson(`/api/v1/hosts/${hostId}/approve/`, {
      method: 'POST',
      body: JSON.stringify({ totp }),
    });
    showToast('Host approved', 'success');
    btn.closest('.enroll-card').style.opacity = '0.4';
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
    btn.disabled = false;
  }
}

async function rejectHost(hostId, btn) {
  try {
    btn.disabled = true;
    await apiPost(`/api/v1/hosts/${hostId}/reject/`);
    showToast('Host rejected', 'success');
    btn.closest('.enroll-card').style.opacity = '0.4';
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
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
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || 'Delete failed');
    showToast(`${hostname} removed`, 'success');
    closeHostDetail();
    // Remove the card from the DOM immediately.
    const card = document.querySelector(`.host-card[data-id="${hostId}"]`);
    if (card) card.remove();
    // Invalidate fleet cache so the deploy modal won't show the deleted host.
    _deployHostCache = null;
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
    btn.disabled = false;
  }
}

/* ── Pinned hosts (localStorage) ─────────────────────────────────────── */
const PIN_KEY = 'vigil-pinned-hosts';

function getPins() {
  try { return JSON.parse(localStorage.getItem(PIN_KEY)) || []; }
  catch { return []; }
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
document.addEventListener('click', (ev) => {
  const btn = ev.target.closest && ev.target.closest('.hdi-pin');
  if (!btn) return;
  ev.stopPropagation();
  const item = btn.closest('.host-dropdown-item');
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
    const item = btn.closest('.host-dropdown-item');
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
  if (dd && !dd.contains(e.target) && e.target.id !== 'host-selector-btn') {
    dd.classList.remove('open');
  }
});

document.querySelectorAll('.host-dropdown-item').forEach(item => {
  item.addEventListener('click', () => {
    selectMonitorHost(item.dataset.hostId);
    document.getElementById('host-dropdown').classList.remove('open');
  });
});

/* ── Live host card metrics + filter ─────────────────────────────────── */
// `root` scopes the refresh to one subtree — the dashboard's host widget
// renders the same cards, and refreshing the whole document from there would
// re-fetch five metric series for every card on the hosts page as well.
async function refreshHostCards(root) {
  // Skip cards inside the collapsed inactive section — no live data needed.
  const cards = (root || document).querySelectorAll('.host-card:not([data-inactive="1"])');
  for (const card of cards) {
    const hostId = card.dataset.id;
    if (!hostId || card.dataset.status !== 'online') continue;
    const from = new Date(Date.now() - 5 * 60000).toISOString();
    try {
      const [cpuResp, memResp, diskResp, netInResp, netOutResp] = await Promise.all([
        fetch(`/api/v1/metrics/${hostId}/cpu/usage_percent/?from=${from}&limit=5`, { credentials: 'same-origin' }),
        fetch(`/api/v1/metrics/${hostId}/memory/usage_percent/?from=${from}&limit=5`, { credentials: 'same-origin' }),
        fetch(`/api/v1/metrics/${hostId}/disk/usage_percent/?from=${from}&limit=5`, { credentials: 'same-origin' }),
        fetch(`/api/v1/metrics/${hostId}/network/bytes_recv/?from=${from}&limit=20`, { credentials: 'same-origin' }),
        fetch(`/api/v1/metrics/${hostId}/network/bytes_sent/?from=${from}&limit=20`, { credentials: 'same-origin' }),
      ]);
      const [cpuData, memData, diskData, netInData, netOutData] = await Promise.all([
        cpuResp.ok    ? cpuResp.json()    : [],
        memResp.ok    ? memResp.json()    : [],
        diskResp.ok   ? diskResp.json()   : [],
        netInResp.ok  ? netInResp.json()  : [],
        netOutResp.ok ? netOutResp.json() : [],
      ]);
      const cpuEl = card.querySelector('[data-metric="cpu"]');
      const memEl = card.querySelector('[data-metric="memory"]');
      const diskEl = card.querySelector('[data-metric="disk"]');
      const netInEl = card.querySelector('[data-metric="net-in"]');
      const netOutEl = card.querySelector('[data-metric="net-out"]');

      const cpuPts = cpuData.filter(p => (p.labels || {}).core === 'total');
      if (cpuPts.length && cpuEl) {
        const v = Math.round(cpuPts[0].value);
        cpuEl.querySelector('.host-metric-value').textContent = v + '%';
        cpuEl.querySelector('.host-metric-fill').style.width = v + '%';
      }
      if (memData.length && memEl) {
        const v = Math.round(memData[0].value);
        memEl.querySelector('.host-metric-value').textContent = v + '%';
        memEl.querySelector('.host-metric-fill').style.width = v + '%';
      }
      if (diskData.length && diskEl) {
        // Aggregate latest disk usage across mounts — show the highest %.
        const max = diskData.reduce((m, p) => Math.max(m, p.value || 0), 0);
        const v = Math.round(max);
        diskEl.querySelector('.host-metric-value').textContent = v + '%';
        diskEl.querySelector('.host-metric-fill').style.width = v + '%';
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
          samples.sort((a,b) => new Date(a.time) - new Date(b.time));
          if (samples.length < 2) continue;
          const a = samples[samples.length - 2];
          const b = samples[samples.length - 1];
          const dt = (new Date(b.time) - new Date(a.time)) / 1000;
          if (dt > 0) total += Math.max(0, (b.value - a.value) / dt);
        }
        return total;
      }
      const inRate = _rate(netInData);
      const outRate = _rate(netOutData);
      if (netInEl) {
        netInEl.querySelector('.host-metric-value').textContent = _formatBytesPerSec(inRate);
        const pct = Math.min(100, Math.round((inRate / (1024*1024)) * 100));
        netInEl.querySelector('.host-metric-fill').style.width = pct + '%';
      }
      if (netOutEl) {
        netOutEl.querySelector('.host-metric-value').textContent = _formatBytesPerSec(outRate);
        const pct = Math.min(100, Math.round((outRate / (1024*1024)) * 100));
        netOutEl.querySelector('.host-metric-fill').style.width = pct + '%';
      }
    } catch {}
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
  for (const card of document.querySelectorAll('.host-card')) {
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
  const inactive = document.getElementById('inactive-hosts-section');
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
    _appendStylesheet(src.dataset.css);
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

  addRow('Web shell', opts.shell, () => {
    loadTerminalAssets()
      .then(() => openSession(hostId, 'shell'))
      .catch((err) => showToast(err.message, 'error'));
  });
  // Windows serves RDP itself, Linux through xrdp, and a Mac's desktop is its
  // own Screen Sharing (VNC): the same session either way, named for what it is.
  addRow(opts.os_family === 'macos' ? 'Screen sharing' : 'Web RDP', opts.rdp, () => {
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
    addRow('Download .rdp file', always,
      () => downloadHostRdp(hostId, hostname || opts.host));
  } else {
    addRow('SSH command', always,
      () => openSshFor(hostname || opts.host, opts.ssh_target));
  }
  if (opts.recorded) {
    const note = document.createElement('div');
    note.className = 'connect-note';
    note.textContent = 'Sessions on this install are recorded.';
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
