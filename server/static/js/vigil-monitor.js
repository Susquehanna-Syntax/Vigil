// vigil-monitor.js
// Owns: Monitor page — gauges, time-series charts, top processes, network I/O,
//   disk bars, auto-refresh, and the navigateTo wrapper that auto-selects
//   the first pinned host on entry.
// HTML: templates/pages/_monitor.html
// Depends on: vigil-utils.js (escHtml, formatBytes, groupByLabel, computeRates),
//   vigil-host-cards.js (selectMonitorHost reads from the host dropdown DOM,
//   getPins for auto-select), vigil-vulns.js (refreshVulns called on nav),
//   vigil-settings.js (refreshTotpStatus called on nav).
// API: GET /api/v1/metrics/{host}/{category}/{metric}/

/* ── Chart.js global config ──────────────────────────────────────────── */
Chart.defaults.color = '#6e6b76';
Chart.defaults.font.family = "'DM Sans', sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.display = false;
Chart.defaults.elements.point.radius = 0;
Chart.defaults.elements.point.hoverRadius = 4;
Chart.defaults.elements.line.borderWidth = 2;
Chart.defaults.elements.line.tension = 0.35;
Chart.defaults.animation.duration = 600;

const CIRCUMFERENCE = 2 * Math.PI * 34; // gauge ring r=34

// Monitor state
let monitorHostId = null;
let monitorTimeRange = 60; // minutes
let chartCpu = null, chartMem = null, chartNet = null, chartSwap = null;
let monitorInterval = null;

/* ── Host selection / time range / metric fetch ─────────────────────── */
function selectMonitorHost(hostId) {
  monitorHostId = hostId;
  // Find host data from dropdown items
  const item = document.querySelector(`.host-dropdown-item[data-host-id="${hostId}"]`);
  if (!item) return;

  const d = item.dataset;
  document.getElementById('monitor-empty').style.display = 'none';
  document.getElementById('monitor-content').style.display = 'block';
  document.getElementById('mon-hostname').textContent = d.hostname;
  document.getElementById('mon-meta').textContent = [d.os, d.ip, d.mode].filter(Boolean).join(' · ');
  const dot = document.getElementById('mon-status-dot');
  dot.className = 'status-dot ' + d.status;

  const monLogo = document.getElementById('mon-os-logo');
  if (monLogo) {
    monLogo.replaceChildren();
    const svgDoc = new DOMParser().parseFromString(osLogo(d.os), 'image/svg+xml');
    monLogo.appendChild(document.adoptNode(svgDoc.documentElement));
  }

  renderPinBar();
  refreshMonitor();
}

// ── RDP launch ──
function launchRdpForCurrentMonitor() {
  if (!monitorHostId) { showToast('No host selected', 'error'); return; }
  // Stream a .rdp file download. The server returns it with
  // Content-Disposition: attachment so the OS hands it off to mstsc /
  // Microsoft Remote Desktop / Remmina depending on platform.
  window.location.href = `/api/v1/hosts/${monitorHostId}/rdp/`;
}

// ── Time range ──
function setTimeRange(minutes, btn) {
  monitorTimeRange = minutes;
  document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (monitorHostId) refreshMonitor();
}

// ── Fetch metrics ──
async function fetchMetric(hostId, category, metric, limit) {
  const from = new Date(Date.now() - monitorTimeRange * 60000).toISOString();
  const url = `/api/v1/metrics/${hostId}/${category}/${metric}/?from=${from}&limit=${limit || 500}`;
  try {
    const resp = await fetch(url, { credentials: 'same-origin' });
    if (!resp.ok) return [];
    return await resp.json();
  } catch { return []; }
}

// ── Gauge helpers ──
function setGauge(fillId, textId, pct) {
  const fill = document.getElementById(fillId);
  const text = document.getElementById(textId);
  if (pct === null || pct === undefined) {
    fill.style.strokeDashoffset = CIRCUMFERENCE;
    text.textContent = '—';
    return;
  }
  const offset = CIRCUMFERENCE * (1 - Math.min(pct, 100) / 100);
  fill.style.strokeDashoffset = offset;
  text.textContent = Math.round(pct) + '%';
}

// ── Chart factory ──
function makeTimeChart(canvasId, color, label) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label,
        data: [],
        borderColor: color,
        backgroundColor: color + '18',
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: {
          type: 'time',
          grid: { color: '#32323a', drawTicks: false },
          border: { color: '#32323a' },
          ticks: { maxTicksLimit: 8, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        },
        y: {
          min: 0, max: 100,
          grid: { color: '#32323a', drawTicks: false },
          border: { color: '#32323a' },
          ticks: { callback: v => v + '%', maxTicksLimit: 5, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        }
      },
      plugins: {
        tooltip: {
          backgroundColor: '#232329',
          borderColor: '#3a3a43',
          borderWidth: 1,
          titleFont: { family: "'DM Sans', sans-serif", weight: 600 },
          bodyFont: { family: "'IBM Plex Mono', monospace" },
          callbacks: {
            label: ctx => ctx.parsed.y.toFixed(1) + '%',
          }
        }
      }
    }
  });
}

function makeNetChart(canvasId) {
  const ctx = document.getElementById(canvasId).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        { label: 'Received', data: [], borderColor: '#82c4ee', backgroundColor: '#82c4ee18', fill: true },
        { label: 'Sent', data: [], borderColor: '#f0b888', backgroundColor: '#f0b88818', fill: true },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      scales: {
        x: {
          type: 'time',
          grid: { color: '#32323a', drawTicks: false },
          border: { color: '#32323a' },
          ticks: { maxTicksLimit: 8, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        },
        y: {
          grid: { color: '#32323a', drawTicks: false },
          border: { color: '#32323a' },
          ticks: {
            maxTicksLimit: 5,
            font: { family: "'IBM Plex Mono', monospace", size: 10 },
            callback: v => formatBytes(v),
          },
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#232329',
          borderColor: '#3a3a43',
          borderWidth: 1,
          callbacks: { label: ctx => ctx.dataset.label + ': ' + formatBytes(ctx.parsed.y) },
        }
      }
    }
  });
}

/* ── Charts: ensureCharts, updateChart, refreshMonitor ───────────────── */
function ensureCharts() {
  if (!chartCpu) chartCpu = makeTimeChart('chart-cpu', '#82c4ee', 'CPU');
  if (!chartMem) chartMem = makeTimeChart('chart-mem', '#baa8e8', 'Memory');
  if (!chartSwap) chartSwap = makeTimeChart('chart-swap', '#f0b888', 'Swap');
  if (!chartNet) chartNet = makeNetChart('chart-net');
}

// ── Update chart data ──
function updateChart(chart, points, labelFilter) {
  let filtered = points;
  if (labelFilter) {
    filtered = points.filter(p => {
      for (const [k, v] of Object.entries(labelFilter)) {
        if ((p.labels || {})[k] !== v) return false;
      }
      return true;
    });
  }
  const data = filtered.map(p => ({ x: new Date(p.time), y: p.value })).sort((a, b) => a.x - b.x);
  chart.data.datasets[0].data = data;
  chart.update('none');
  return data;
}

// ── Main refresh ──
async function refreshMonitor() {
  if (!monitorHostId) return;
  const btn = document.getElementById('monitor-refresh-btn');
  btn.disabled = true;
  btn.style.opacity = '0.5';

  ensureCharts();

  const limit = monitorTimeRange <= 60 ? 200 : monitorTimeRange <= 360 ? 400 : 800;

  // Fetch all metrics in parallel
  const [cpuData, memData, diskData, netRecv, netSent, swapData, loadData] = await Promise.all([
    fetchMetric(monitorHostId, 'cpu', 'usage_percent', limit),
    fetchMetric(monitorHostId, 'memory', 'usage_percent', limit),
    fetchMetric(monitorHostId, 'disk', 'usage_percent', limit),
    fetchMetric(monitorHostId, 'network', 'bytes_recv', limit),
    fetchMetric(monitorHostId, 'network', 'bytes_sent', limit),
    fetchMetric(monitorHostId, 'memory', 'swap_usage_percent', limit),
    fetchMetric(monitorHostId, 'cpu', 'load_1m', limit),
  ]);

  // ── CPU ──
  const cpuPoints = updateChart(chartCpu, cpuData, { core: 'total' });
  const cpuLatest = cpuPoints.length ? cpuPoints[cpuPoints.length - 1].y : null;
  setGauge('gauge-cpu-fill', 'gauge-cpu-text', cpuLatest);
  document.getElementById('cpu-chart-latest').textContent = cpuLatest !== null ? cpuLatest.toFixed(1) + '%' : '—';

  // ── Memory ──
  const memPoints = updateChart(chartMem, memData);
  const memLatest = memPoints.length ? memPoints[memPoints.length - 1].y : null;
  setGauge('gauge-mem-fill', 'gauge-mem-text', memLatest);
  document.getElementById('mem-chart-latest').textContent = memLatest !== null ? memLatest.toFixed(1) + '%' : '—';

  // ── Swap ──
  const swapPoints = updateChart(chartSwap, swapData);
  const swapLatest = swapPoints.length ? swapPoints[swapPoints.length - 1].y : null;
  chartSwap.options.scales.y.max = 100;
  chartSwap.update('none');
  document.getElementById('swap-chart-latest').textContent = swapLatest !== null ? swapLatest.toFixed(1) + '%' : '—';

  // ── Disk bars ──
  renderDiskBars(diskData);

  // ── Network I/O (compute deltas for rate) ──
  const recvSorted = netRecv.sort((a, b) => new Date(a.time) - new Date(b.time));
  const sentSorted = netSent.sort((a, b) => new Date(a.time) - new Date(b.time));
  // Group by interface, pick first interface found
  const recvByIface = groupByLabel(recvSorted, 'interface');
  const sentByIface = groupByLabel(sentSorted, 'interface');
  const iface = Object.keys(recvByIface)[0] || Object.keys(sentByIface)[0];
  if (iface) {
    const recvRates = computeRates(recvByIface[iface] || []);
    const sentRates = computeRates(sentByIface[iface] || []);
    chartNet.data.datasets[0].data = recvRates;
    chartNet.data.datasets[1].data = sentRates;
    chartNet.options.scales.y.min = undefined;
    chartNet.options.scales.y.max = undefined;
  } else {
    chartNet.data.datasets[0].data = [];
    chartNet.data.datasets[1].data = [];
  }
  chartNet.update('none');

  // ── Disk gauge (primary mount) ──
  const diskLatest = getLatestDiskPercent(diskData);
  setGauge('gauge-disk-fill', 'gauge-disk-text', diskLatest);

  // ── Load average ──
  if (loadData.length) {
    const latest = loadData.sort((a, b) => new Date(b.time) - new Date(a.time))[0];
    document.getElementById('gauge-load-text').textContent = latest.value.toFixed(2);
  } else {
    document.getElementById('gauge-load-text').textContent = '—';
  }

  // ── Top processes ──
  const [procCpu, procMem] = await Promise.all([
    fetchMetric(monitorHostId, 'process', 'cpu_percent', 50),
    fetchMetric(monitorHostId, 'process', 'memory_percent', 50),
  ]);
  renderProcTable('proc-table-cpu', procCpu, 'cpu_percent');
  renderProcTable('proc-table-mem', procMem, 'memory_percent');

  btn.disabled = false;
  btn.style.opacity = '1';
}

/* ── Top processes table ─────────────────────────────────────────────── */
function renderProcTable(tableId, points, metricName) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  if (!points.length) {
    tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-3);text-align:center;padding:20px;">No data yet</td></tr>';
    return;
  }
  // Get the latest snapshot: group by rank, take most recent per rank
  const byRank = {};
  for (const p of points) {
    const rank = parseInt((p.labels || {}).rank || '99');
    const time = new Date(p.time);
    if (!byRank[rank] || time > byRank[rank].time) {
      byRank[rank] = { ...p, time };
    }
  }
  const sorted = Object.values(byRank).sort((a, b) => b.value - a.value);
  const isCpu = metricName === 'cpu_percent';
  const accentColor = isCpu ? 'var(--sky)' : 'var(--lavender)';

  tbody.innerHTML = sorted.map(p => {
    const name = (p.labels || {}).name || '?';
    const pid = (p.labels || {}).pid || '?';
    const val = p.value.toFixed(1);
    const barW = Math.min(p.value, 100);
    return `<tr>
      <td class="proc-name">${escHtml(name)}</td>
      <td class="proc-pid">${pid}</td>
      <td class="proc-val" style="color:${accentColor};">
        <div style="display:flex;align-items:center;justify-content:flex-end;gap:8px;">
          <div style="width:50px;height:4px;background:var(--s3);border-radius:2px;overflow:hidden;">
            <div style="height:100%;width:${barW}%;background:${accentColor};border-radius:2px;"></div>
          </div>
          ${val}%
        </div>
      </td>
    </tr>`;
  }).join('');
}

/* ── Disk usage helpers ──────────────────────────────────────────────── */
function getLatestDiskPercent(diskData) {
  if (!diskData.length) return null;
  // Find the "/" mount or first mount
  const byMount = groupByLabel(diskData, 'mount');
  const mount = byMount['/'] ? '/' : Object.keys(byMount)[0];
  if (!mount) return null;
  const pts = byMount[mount].sort((a, b) => new Date(b.time) - new Date(a.time));
  return pts[0]?.value ?? null;
}

function renderDiskBars(diskData) {
  const container = document.getElementById('disk-bars');
  if (!diskData.length) {
    container.innerHTML = '<div style="color:var(--text-3);font-size:13px;text-align:center;padding:20px;">Waiting for data...</div>';
    return;
  }
  // Get latest value per mount
  const byMount = groupByLabel(diskData, 'mount');
  const mounts = {};
  for (const [mount, pts] of Object.entries(byMount)) {
    const latest = pts.sort((a, b) => new Date(b.time) - new Date(a.time))[0];
    mounts[mount] = latest.value;
  }
  // Sort by mount name
  const sorted = Object.entries(mounts).sort((a, b) => a[0].localeCompare(b[0]));
  container.innerHTML = sorted.map(([mount, pct]) => {
    const color = pct > 90 ? 'var(--rose)' : pct > 75 ? 'var(--lemon)' : 'var(--mint)';
    return `<div class="disk-row">
      <div class="disk-mount" title="${mount}">${mount}</div>
      <div class="disk-bar-track"><div class="disk-bar-fill" style="width:${pct}%;background:${color};"></div></div>
      <div class="disk-pct" style="color:${color};">${Math.round(pct)}%</div>
    </div>`;
  }).join('');
}

/* ── Auto-refresh (every 60s when monitor page is visible) ──────────── */
function startAutoRefresh() {
  stopAutoRefresh();
  monitorInterval = setInterval(() => {
    if (document.getElementById('page-monitor').classList.contains('active') && monitorHostId) {
      refreshMonitor();
    }
  }, 60000);
}

function stopAutoRefresh() {
  if (monitorInterval) { clearInterval(monitorInterval); monitorInterval = null; }
}

// Kick off the auto-refresh interval on script load.
startAutoRefresh();

/* ── navigateTo wrapper: monitor → auto-select pinned, vulns → refresh,
    settings → refresh TOTP. Each feature file stacks its own wrapper. ── */
const origNavigate = navigateTo;
navigateTo = function(pageName) {
  origNavigate(pageName);
  if (pageName === 'monitor' && !monitorHostId) {
    const pins = getPins();
    if (pins.length) selectMonitorHost(pins[0].id);
  }
  if (pageName === 'vulns') refreshVulns();
  if (pageName === 'settings') refreshTotpStatus();
};
