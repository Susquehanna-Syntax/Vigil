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

  const dot = document.getElementById('detail-status-dot');
  dot.className = 'status-dot ' + d.status;

  const statusNames = {online: 'Online', offline: 'Offline', pending: 'Pending Enrollment'};
  document.getElementById('detail-status-text').textContent = statusNames[d.status] || d.status;

  const badge = document.getElementById('detail-mode-badge');
  badge.textContent = d.modeDisplay;
  badge.className = 'mode-badge mode-' + d.mode;

  document.getElementById('detail-last-checkin').textContent =
    d.lastCheckin ? 'Recent' : 'Never';

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
    `;
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
    const chip = document.createElement('span');
    chip.className = 'host-tag-chip';
    chip.style.cursor = 'pointer';
    chip.title = 'Click to remove';
    chip.textContent = t + '  ✕';
    chip.addEventListener('click', () => _removeDetailTag(t));
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

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('detail-tag-input');
  const btn = document.getElementById('detail-tag-add');
  if (!input || !btn) return;
  function commit() {
    const v = (input.value || '').trim().toLowerCase();
    if (!v) return;
    if (_detailTagState.tags.includes(v)) { input.value = ''; return; }
    const next = [..._detailTagState.tags, v];
    input.value = '';
    _saveDetailTags(next);
  }
  btn.addEventListener('click', commit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
  });
});

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
        x: { type: 'time', grid: { color: '#32323a', drawTicks: false }, border: { color: '#32323a' }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: { min: 0, max: 100, grid: { color: '#32323a', drawTicks: false }, border: { color: '#32323a' }, ticks: { callback: v => v + '%', maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
      },
      plugins: { tooltip: { backgroundColor: '#232329', borderColor: '#3a3a43', borderWidth: 1, callbacks: { label: ctx => ctx.parsed.y.toFixed(1) + '%' } } },
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
        x: { type: 'time', grid: { color: '#32323a', drawTicks: false }, border: { color: '#32323a' }, ticks: { maxTicksLimit: 6, font: { family: "'IBM Plex Mono', monospace", size: 10 } } },
        y: { grid: { color: '#32323a', drawTicks: false }, border: { color: '#32323a' }, ticks: { maxTicksLimit: 4, font: { family: "'IBM Plex Mono', monospace", size: 10 }, callback: v => formatBytes(v) } },
      },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: '#232329', borderColor: '#3a3a43', borderWidth: 1, callbacks: { label: ctx => ctx.dataset.label + ': ' + formatBytes(ctx.parsed.y) } } },
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
  try {
    btn.disabled = true;
    await apiPost(`/api/v1/hosts/${hostId}/approve/`);
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

function renderPinBar() {
  const bar = document.getElementById('pin-bar');
  const selectorWrap = bar.querySelector('.host-selector-wrap');
  // Remove old chips
  bar.querySelectorAll('.pin-chip').forEach(c => c.remove());
  // Add chips before the selector
  const pins = getPins();
  pins.forEach(pin => {
    const chip = document.createElement('div');
    chip.className = 'pin-chip' + (pin.id === monitorHostId ? ' selected' : '');
    const dotColor = pin.status === 'online' ? 'var(--mint)' : pin.status === 'pending' ? 'var(--peach)' : 'var(--rose)';
    chip.innerHTML = `<div class="pin-dot" style="background:${dotColor}"></div>${pin.hostname}<button class="pin-remove" onclick="event.stopPropagation();togglePin('${pin.id}','${pin.hostname}','${pin.status}',null)">&times;</button>`;
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
async function refreshHostCards() {
  // Skip cards inside the collapsed inactive section — no live data needed.
  const cards = document.querySelectorAll('.host-card:not([data-inactive="1"])');
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

// Initial render of pinned-hosts bar.
renderPinBar();
