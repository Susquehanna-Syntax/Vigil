// @ts-check
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

/** One sample from a metrics endpoint, as this page reads it. `time` is the API's
 *  timestamp — epoch ms — but it reaches a Date constructor below, so it is the
 *  same TimeLike vigil-utils.js types its metric helpers with. */
/** @typedef {{labels?: Record<string, string>, time: number|string, value: number}} MonitorMetricPoint */
/** A pinned host (vigil-host-cards.js, localStorage). */
/** @typedef {{id: string, hostname: string, status: string}} Pin */

/* ── Chart.js global config ──────────────────────────────────────────── */
Chart.defaults.color = cssVar('--text-3') || '#6e6b76';
Chart.defaults.font.family = "'DM Sans', sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.display = false;
Chart.defaults.elements.point.radius = 0;
Chart.defaults.elements.point.hoverRadius = 4;
Chart.defaults.elements.line.borderWidth = 2;
// 0.35 looked nice and cost real frames: a bezier through every point is
// recomputed on each redraw, and a redraw happens on every hover. 0.2 keeps
// the curve readable at a fraction of the work.
Chart.defaults.elements.line.tension = 0.2;
// Animating a routine data refresh burns CPU for something nobody is looking
// at. Range changes animate deliberately — see setTimeRange.
Chart.defaults.animation.duration = 0;
// Hover redraws the chart; without this it also re-tests every point.
Chart.defaults.elements.point.hitRadius = 8;
// Grids and tooltips read tokens through tok(); a theme change redraws every
// chart so they pick the new ones up, and the default text colour with them.
document.addEventListener('vigil:theme', () => {
  Chart.defaults.color = cssVar('--text-3');
  Object.values(/** @type {Record<string, any>} */ (Chart.instances)).forEach((/** @type {{update: (mode: string) => void}} */ c) => c.update('none'));
});

// `chart.update('none')` skips animation outright — which is what a routine
// poll wants, and is also why the range-change zoom silently never played
// until this existed. setTimeRange names a mode for exactly one refresh.
let monitorZoomMode = null;
const chartUpdateMode = () => monitorZoomMode || 'none';

// Named update modes, declared once and referenced by `chart.update(mode)`.
// Widening eases longer because more of the chart is changing.
// A fresh object per chart: Chart.js caches resolved values onto the config
// objects it is handed, so two charts must not share one literal.
const zoomModes = () => ({
  zoomOut: { animation: { duration: 480, easing: 'easeOutQuart' } },
  zoomIn: { animation: { duration: 360, easing: 'easeOutQuart' } },
});

const CIRCUMFERENCE = 2 * Math.PI * 34; // gauge ring r=34

// Monitor state
let monitorHostId = null;
let monitorHostIp = '';
let monitorHostName = '';
let monitorTimeRange = 60; // minutes
/** @type {any} */
let chartCpu = null, /** @type {any} */ chartMem = null, /** @type {any} */ chartNet = null, /** @type {any} */ chartSwap = null;
/** @type {number|null} */
let monitorInterval = null;

/* ── Host selection / time range / metric fetch ─────────────────────── */
/** @param {string} hostId @returns {void} */
function selectMonitorHost(hostId) {
  monitorHostId = hostId;
  // Find host data from dropdown items
  const item = /** @type {HTMLElement|null} */ (document.querySelector(`.host-dropdown-item[data-host-id="${hostId}"]`));
  if (!item) return;

  const d = item.dataset;
  monitorHostIp = d.ip && d.ip !== '—' ? d.ip : '';
  monitorHostName = d.hostname || '';
  document.getElementById('monitor-empty').style.display = 'none';
  document.getElementById('monitor-content').style.display = 'block';
  document.getElementById('mon-hostname').textContent = d.hostname;
  document.getElementById('mon-meta').textContent = [d.os, d.ip, d.mode].filter(Boolean).join(' · ');
  const dot = document.getElementById('mon-status-dot');
  dot.className = 'status-dot ' + d.status;

  const monLogo = document.getElementById('mon-os-logo');
  if (monLogo) {
    monLogo.replaceChildren();
    monLogo.appendChild(osLogoNode(d.os));
  }

  renderPinBar();
  refreshMonitor();
  if (typeof renderJackilRelated === 'function') {
    renderJackilRelated(document.getElementById('mon-jackil-section'), document.getElementById('mon-jackil-body'),
      'host', hostId, () => monitorHostId === hostId);
  }
}

// ── RDP launch ──
function launchRdpForCurrentMonitor() {
  if (!monitorHostId) { showToast(t('No host selected'), 'error'); return; }
  // Stream a .rdp file download. The server returns it with
  // Content-Disposition: attachment so the OS hands it off to mstsc /
  // Microsoft Remote Desktop / Remmina depending on platform.
  window.location.href = `/api/v1/hosts/${monitorHostId}/rdp/`;
}

// ── SSH command (client-side only; opens in the operator's own terminal) ──
function openSshFor(hostname, ip) {
  monitorHostName = hostname || monitorHostName;
  monitorHostIp = ip || '';
  const target = _sshTarget();
  if (!target) { showToast(t('No address known for this host'), 'error'); return; }
  document.getElementById('ssh-modal-title').textContent = t('SSH to {host}', { host: monitorHostName || target });
  /** @type {HTMLInputElement} */ (document.getElementById('ssh-user')).value = '';
  updateSshCommand();
  document.getElementById('ssh-overlay').classList.add('open');
  document.getElementById('ssh-modal').classList.add('open');
}

function openSshForCurrentMonitor() {
  if (!monitorHostId) { showToast(t('No host selected'), 'error'); return; }
  openSshFor(monitorHostName, monitorHostIp);
}

function closeSshModal() {
  document.getElementById('ssh-overlay').classList.remove('open');
  document.getElementById('ssh-modal').classList.remove('open');
}

function _sshTarget() {
  return monitorHostIp || monitorHostName || 'host';
}

function updateSshCommand() {
  const user = /** @type {HTMLInputElement} */ (document.getElementById('ssh-user')).value.trim();
  const host = _sshTarget();
  const dest = user ? `${user}@${host}` : host;
  document.getElementById('ssh-cmd').textContent = `ssh ${dest}`; // i18n-ignore
  // Encode userinfo and host separately so the "@" stays structural while a
  // stray character in either part can't reshape the URI or the href attribute.
  const uri = 'ssh://' + (user ? encodeURIComponent(user) + '@' : '') + encodeURIComponent(host);
  /** @type {HTMLAnchorElement} */ (document.getElementById('ssh-uri-link')).href = uri;
}

function copySshCommand() {
  const cmd = document.getElementById('ssh-cmd').textContent;
  copyText(cmd).then((ok) => showToast(
    ok ? t('Copied — paste into your terminal') : t('Copy failed'),
    ok ? 'success' : 'error'));
}

// ── Time range ──
function setTimeRange(minutes, btn) {
  const widening = minutes > monitorTimeRange;
  monitorTimeRange = minutes;
  document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');

  // Routine refreshes animate at 0ms — animating data nobody asked to change
  // is wasted CPU on a slow machine. A range change is different: it is a
  // deliberate act and the chart is about to look completely different, so a
  // short ease makes it legible as a zoom rather than a flicker. Widening gets
  // slightly longer, because more is changing.
  //
  // This goes through a named transition mode rather than by writing to
  // `chart.options`. `chart.options` is a Chart.js resolver proxy: assigning a
  // nested object back onto it (`opts.transitions = opts.transitions || {}`)
  // makes the proxy resolve through itself and blows the stack, which took the
  // whole refresh down with it. zoomModes() is declared in the chart config,
  // so nothing is mutated at runtime and there is nothing to restore.
  monitorZoomMode = widening ? 'zoomOut' : 'zoomIn';
  const done = () => { monitorZoomMode = null; };
  if (monitorHostId) {
    Promise.resolve(refreshMonitor()).finally(done);
  } else {
    done();
  }
}

// ── Fetch metrics ──
/** @param {string} hostId @param {string} category @param {string} metric @param {number=} limit @returns {Promise<MonitorMetricPoint[]>} */
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
/** @param {string} fillId @param {string} textId @param {number|null|undefined} pct @returns {void} */
function setGauge(fillId, textId, pct) {
  // getElementById is typed HTMLElement; the gauge fill is an SVG <circle>.
  const fill = /** @type {SVGElement} */ (/** @type {unknown} */ (document.getElementById(fillId)));
  const text = document.getElementById(textId);
  if (pct === null || pct === undefined) {
    fill.style.strokeDashoffset = String(CIRCUMFERENCE);
    text.textContent = '—';
    return;
  }
  const offset = CIRCUMFERENCE * (1 - Math.min(pct, 100) / 100);
  fill.style.strokeDashoffset = String(offset);
  text.textContent = Math.round(pct) + '%';
}

// ── Chart factory ──
/** @param {string} canvasId @param {string} color @param {string} label @returns {any} */
function makeTimeChart(canvasId, color, label) {
  const ctx = /** @type {HTMLCanvasElement} */ (document.getElementById(canvasId)).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [{
        label,
        data: [],
        borderColor: color,
        backgroundColor: color + '18',
        fill: true,
      },
        // The min-max band (bandSeries): empty unless the range is bucketed.
        // The second fills up to the first, so the band sits between them.
        { label: label + ' max', data: [], band: true, borderWidth: 0, pointRadius: 0,
          backgroundColor: color + '30', fill: false },
        { label: label + ' min', data: [], band: true, borderWidth: 0, pointRadius: 0,
          backgroundColor: color + '30', fill: '-1' },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // `index` + `intersect:false` finds the nearest x rather than hit-testing
      // every point, which is what made hovering expensive on a slow CPU.
      interaction: { intersect: false, mode: 'index', axis: 'x' },
      transitions: zoomModes(),
      // Chart.js draws at most one point per pixel column; beyond that it is
      // redrawing detail the screen cannot show.
      parsing: false,
      normalized: true,
      scales: {
        x: {
          type: 'time',
          // Declared so axisUnits() can set time.unit as a plain value; never
          // assign an object onto chart.options (see setTimeRange).
          time: { unit: false },
          grid: { color: tok('--s3'), drawTicks: false },
          border: { color: tok('--s3') },
          ticks: { maxTicksLimit: 8, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        },
        y: {
          min: 0, max: 100,
          grid: { color: tok('--s3'), drawTicks: false },
          border: { color: tok('--s3') },
          ticks: { callback: v => v + '%', maxTicksLimit: 5, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        }
      },
      plugins: {
        decimation: { enabled: true, algorithm: 'lttb', samples: 250 },
        tooltip: {
          backgroundColor: tok('--s1'),
          borderColor: tok('--border'),
          borderWidth: 1,
          titleFont: { family: "'DM Sans', sans-serif", weight: 600 },
          bodyFont: { family: "'IBM Plex Mono', monospace" },
          filter: item => !item.dataset.band,
          callbacks: {
            label: ctx => {
              const r = ctx.raw || {};
              const avg = ctx.parsed.y.toFixed(1) + '%';
              return typeof r.min === 'number' && typeof r.max === 'number'
                ? `${avg} average, ${r.min.toFixed(1)}% to ${r.max.toFixed(1)}%`
                : avg;
            },
          }
        }
      }
    }
  });
}

function makeNetChart(canvasId) {
  const ctx = /** @type {HTMLCanvasElement} */ (document.getElementById(canvasId)).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        { label: t('Received'), data: [], borderColor: '#82c4ee', backgroundColor: '#82c4ee18', fill: true },
        { label: t('Sent'), data: [], borderColor: '#f0b888', backgroundColor: '#f0b88818', fill: true },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // `index` + `intersect:false` finds the nearest x rather than hit-testing
      // every point, which is what made hovering expensive on a slow CPU.
      interaction: { intersect: false, mode: 'index', axis: 'x' },
      transitions: zoomModes(),
      // Chart.js draws at most one point per pixel column; beyond that it is
      // redrawing detail the screen cannot show.
      parsing: false,
      normalized: true,
      scales: {
        x: {
          type: 'time',
          // Declared so axisUnits() can set time.unit as a plain value; never
          // assign an object onto chart.options (see setTimeRange).
          time: { unit: false },
          grid: { color: tok('--s3'), drawTicks: false },
          border: { color: tok('--s3') },
          ticks: { maxTicksLimit: 8, font: { family: "'IBM Plex Mono', monospace", size: 10 } },
        },
        y: {
          grid: { color: tok('--s3'), drawTicks: false },
          border: { color: tok('--s3') },
          ticks: {
            maxTicksLimit: 5,
            font: { family: "'IBM Plex Mono', monospace", size: 10 },
            callback: v => formatBytes(v),
          },
        }
      },
      plugins: {
        decimation: { enabled: true, algorithm: 'lttb', samples: 250 },
        legend: { display: false },
        tooltip: {
          backgroundColor: tok('--s1'),
          borderColor: tok('--border'),
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

// Past a day, the automatic unit picked ticks ~22 hours apart labelled by
// time of day alone ("12AM, 10PM, 8PM"), which read as going backwards. Dates
// are what a week or a month wants.
function axisUnits(chart) {
  chart.options.scales.x.time.unit = monitorTimeRange > 1440 ? 'day' : false;
}

// ── Update chart data ──
/** @param {any} chart @param {MonitorMetricPoint[]} points @param {Record<string, string>=} labelFilter @returns {{x: number, y: number}[]} */
function updateChart(chart, points, labelFilter) {
  let filtered = points;
  if (labelFilter) {
    filtered = points.filter((/** @type {MonitorMetricPoint} */ p) => {
      for (const [k, v] of Object.entries(labelFilter || {})) {
        if ((p.labels || {})[k] !== v) return false;
      }
      return true;
    });
  }
  // Numeric timestamps, not Date objects: `parsing: false` hands the array
  // straight to the time scale, which wants milliseconds.
  const data = filtered.map(p => ({ x: new Date(p.time).getTime(), y: p.value, min: p.min, max: p.max }))
    .sort((a, b) => a.x - b.x);
  chart.data.datasets[0].data = data;
  // Only the time charts have band datasets; the network chart is not drawn
  // through here, but check rather than assume.
  if (chart.data.datasets.length >= 3 && chart.data.datasets[1].band) {
    const band = bandSeries(filtered);
    chart.data.datasets[1].data = band ? band.high : [];
    chart.data.datasets[2].data = band ? band.low : [];
  }
  chart.update(chartUpdateMode());
  return data;
}

// ── Main refresh ──
async function refreshMonitor() {
  if (!monitorHostId) return;
  const btn = /** @type {HTMLButtonElement} */ (document.getElementById('monitor-refresh-btn'));
  btn.disabled = true;
  btn.style.opacity = '0.5';

  ensureCharts();
  [chartCpu, chartMem, chartSwap, chartNet].forEach(axisUnits);

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
  chartSwap.update(chartUpdateMode());
  document.getElementById('swap-chart-latest').textContent = swapLatest !== null ? swapLatest.toFixed(1) + '%' : '—';

  // ── Disk bars ──
  renderDiskBars(diskData);

  // ── Network I/O (compute deltas for rate) ──
  // getTime() on each side of these sorts rather than `new Date(a) - new Date(b)`:
  // tsc rejects the latter because a metric point's timestamp may arrive as an
  // ISO string. Same value — vigil-utils.js computeRates() made this change in 03f.
  const recvSorted = netRecv.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());
  const sentSorted = netSent.sort((a, b) => new Date(a.time).getTime() - new Date(b.time).getTime());
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
  chartNet.update(chartUpdateMode());

  // ── Disk gauge (primary mount) ──
  const diskLatest = getLatestDiskPercent(diskData);
  setGauge('gauge-disk-fill', 'gauge-disk-text', diskLatest);

  // ── Load average ──
  // getTime() on each side rather than `new Date(b) - new Date(a)`: tsc rejects
  // the latter because a metric point's timestamp may arrive as an ISO string.
  // Same value — vigil-utils.js computeRates() made the same change in 03f.
  if (loadData.length) {
    const latest = loadData.sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime())[0];
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

  // ── Installed software (fire-and-forget, same reasoning) ──
  if (typeof renderHostSoftware === 'function') renderHostSoftware(monitorHostId);

  btn.disabled = false;
  btn.style.opacity = '1';
}

/* ── Top processes table ─────────────────────────────────────────────── */
function renderProcTable(tableId, points, metricName) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  if (!points.length) {
    tbody.innerHTML = `<tr><td colspan="3" class="proc-td-wait">${escHtml(t('No data yet'))}</td></tr>`;
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
      <td class="proc-val" data-css="color:${accentColor};">
        <div class="proc-val-row">
          <div class="proc-bar-track">
            <div class="proc-bar-fill" data-css="width:${barW}%;background:${accentColor};"></div>
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
  const pts = byMount[mount].sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime());
  return pts[0]?.value ?? null;
}

function renderDiskBars(diskData) {
  const container = document.getElementById('disk-bars');
  if (!diskData.length) {
    container.innerHTML = `<div class="disk-bars-empty">${escHtml(t('Waiting for data...'))}</div>`;
    return;
  }
  // Get latest value per mount
  const byMount = groupByLabel(diskData, 'mount');
  const mounts = {};
  for (const [mount, pts] of Object.entries(byMount)) {
    const latest = pts.sort((a, b) => new Date(b.time).getTime() - new Date(a.time).getTime())[0];
    mounts[mount] = latest.value;
  }
  // Sort by mount name
  const sorted = Object.entries(mounts).sort((a, b) => a[0].localeCompare(b[0]));
  container.innerHTML = sorted.map(([mount, pct]) => {
    const color = pct > 90 ? 'var(--rose)' : pct > 75 ? 'var(--lemon)' : 'var(--mint)';
    return `<div class="disk-row">
      <div class="disk-mount" title="${mount}">${mount}</div>
      <div class="disk-bar-track"><div class="disk-bar-fill" data-css="width:${pct}%;background:${color};"></div></div>
      <div class="disk-pct" data-css="color:${color};">${Math.round(pct)}%</div>
    </div>`;
  }).join('');
}

/* ── Auto-refresh (every 60s when monitor page is visible) ──────────── */
function startAutoRefresh() {
  stopAutoRefresh();
  monitorInterval = pollingInterval(() => {
    if (document.getElementById('page-monitor').classList.contains('active') && monitorHostId) {
      refreshMonitor();
    }
  }, 60000);
}

function stopAutoRefresh() {
  if (monitorInterval) { clearPollingInterval(monitorInterval); monitorInterval = null; }
}

// Kick off the auto-refresh interval on script load.
startAutoRefresh();

/* ── navigateTo wrapper: monitor → auto-select pinned, vulns → refresh,
    settings → refresh TOTP. Each feature file stacks its own wrapper. ── */
const origNavigate = navigateTo;
window.navigateTo = function(pageName) {
  origNavigate(pageName);
  if (pageName === 'monitor' && !monitorHostId) {
    const pins = getPins();
    if (pins.length) selectMonitorHost(pins[0].id);
  }
  if (pageName === 'vulns') refreshVulns();
  if (pageName === 'settings') {
    refreshTotpStatus();
    // The Account pane opens first and holds the Sign-in sessions group,
    // which only loaded when another settings pane was clicked.
    if (typeof loadInstanceSettings === 'function') loadInstanceSettings(false, true);
  }
};

/* ── The monitor header's Connect button (QA-58) ───────────────────────────
   Same menu the host cards open; only where the host id comes from differs. */
function openConnectMenuForMonitor(el) {
  if (!monitorHostId) { showToast(t('No host selected'), 'error'); return; }
  openConnectMenu(monitorHostId, monitorHostName, el);
}
