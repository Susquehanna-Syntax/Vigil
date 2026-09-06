/* Widget renderers — one function per widget kind.
 *
 * Each takes (bodyElement, settings, widget) and fills the body. They are
 * called on first paint and again on the dashboard's refresh poll, so a
 * renderer must be safe to run repeatedly against the same element.
 *
 * Data comes from the same endpoints the fixed pages use; nothing here has a
 * private API. A renderer that throws is caught by the caller and its message
 * shown in the widget, so a broken one costs a tile rather than the grid.
 */

/* ── Shared helpers ────────────────────────────────────────────────────── */

//: Cache per paint cycle so eight widgets asking for the host list issue one
//: request between them rather than eight. Cleared by its own timer, not on
//: read, so a slow renderer cannot resurrect a stale entry.
const _WCACHE = new Map();

function _wCached(url, ttlMs = 10000) {
  const hit = _WCACHE.get(url);
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise;
  const promise = apiJson(url);
  _WCACHE.set(url, { at: Date.now(), promise });
  promise.catch(() => _WCACHE.delete(url));   // never cache a failure
  return promise;
}

function _wRows(payload) {
  return Array.isArray(payload) ? payload : (payload && payload.results) || [];
}

function _wEmpty(body, message) {
  body.innerHTML = `<div class="dash-empty muted-note">${escHtml(message)}</div>`;
}

/* ── Renderers ─────────────────────────────────────────────────────────── */

async function _renderStatTile(body, settings) {
  const [hosts, alerts] = await Promise.all([
    _wCached('/api/v1/hosts/'),
    _wCached('/api/v1/alerts/'),
  ]);
  const h = _wRows(hosts);
  const a = _wRows(alerts);
  const stats = {
    hosts: { value: h.length, label: 'Total hosts', color: 'var(--lavender)' },
    online: { value: h.filter(x => x.status === 'online').length,
              label: 'Online', color: 'var(--mint)' },
    offline: { value: h.filter(x => x.status === 'offline').length,
               label: 'Offline', color: 'var(--text-3)' },
    alerts_firing: { value: a.filter(x => x.state === 'firing').length,
                     label: 'Alerts firing', color: 'var(--rose)' },
    pending: { value: h.filter(x => x.status === 'pending').length,
               label: 'Pending approval', color: 'var(--peach)' },
  };
  const stat = stats[settings.stat] || stats.hosts;
  body.innerHTML =
    `<div class="dash-stat">
       <div class="dash-stat-value" style="color:${stat.color};">${stat.value}</div>
       <div class="dash-stat-label">${escHtml(stat.label)}</div>
     </div>`;
}

async function _renderHostStatusGrid(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/hosts/'));
  const want = (settings.tag_filter || '').trim().toLowerCase();
  const shown = want
    ? rows.filter(h => (h.tags || []).some(t => String(t).toLowerCase() === want))
    : rows;
  if (!shown.length) {
    _wEmpty(body, want ? `No hosts tagged ${settings.tag_filter}` : 'No hosts yet');
    return;
  }
  body.innerHTML = `<div class="dash-hosts">` + shown.map(h => `
    <div class="dash-host" title="${escAttr(h.hostname)}">
      <span class="dash-host-dot dash-dot-${escAttr(h.status)}"></span>
      <span class="dash-host-name">${escHtml(h.hostname)}</span>
      <span class="dash-host-meta">${escHtml(h.os || '')}</span>
    </div>`).join('') + `</div>`;
}

const _SEVERITY_RANK = { info: 0, warning: 1, critical: 2 };

async function _renderAlertList(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/alerts/'));
  const floor = _SEVERITY_RANK[settings.severity] ?? 1;
  const limit = Number(settings.limit) || 10;
  const shown = rows
    .filter(a => a.state === 'firing')
    .filter(a => (_SEVERITY_RANK[a.severity] ?? 1) >= floor)
    .slice(0, limit);
  if (!shown.length) { _wEmpty(body, 'Nothing firing'); return; }
  body.innerHTML = `<div class="dash-alerts">` + shown.map(a => `
    <div class="dash-alert">
      <span class="sev sev-${escAttr(a.severity || 'warning')}">${escHtml(a.severity || '')}</span>
      <span class="dash-alert-msg">${escHtml(a.message || '')}</span>
      <span class="dash-alert-host">${escHtml(a.host_hostname || '')}</span>
    </div>`).join('') + `</div>`;
}

async function _renderRolloutProgress(body) {
  const rows = _wRows(await _wCached('/api/v1/rollouts/'));
  const live = rows.filter(r => r.state === 'running' || r.state === 'validating');
  if (!live.length) { _wEmpty(body, 'No rollouts in flight'); return; }
  body.innerHTML = `<div class="dash-rollouts">` + live.map(r => {
    const waves = r.waves || [];
    const done = waves.filter(w => w.status === 'complete').length;
    return `<div class="dash-rollout">
      <div class="dash-rollout-name">${escHtml(r.target_name || 'Rollout')}</div>
      <div class="dash-rollout-meta">
        <span class="chip">${escHtml(r.state)}</span>
        wave ${done}/${waves.length}
        ${r.current_wave_name ? `· ${escHtml(r.current_wave_name)}` : ''}
        ${r.wave_group_tag ? `· group ${escHtml(r.wave_group_tag)}` : ''}
      </div>
    </div>`;
  }).join('') + `</div>`;
}

async function _renderVulnScore(body) {
  let payload;
  try {
    payload = await _wCached('/api/v1/vulns/score/');
  } catch (e) {
    _wEmpty(body, 'No vulnerability data yet');
    return;
  }
  const score = payload && (payload.score ?? payload.current);
  if (score === undefined || score === null) {
    _wEmpty(body, 'No vulnerability data yet');
    return;
  }
  const colour = score >= 80 ? 'var(--mint)'
               : score >= 50 ? 'var(--lemon)' : 'var(--rose)';
  body.innerHTML =
    `<div class="dash-stat">
       <div class="dash-stat-value" style="color:${colour};">${escHtml(String(score))}</div>
       <div class="dash-stat-label">Fleet vulnerability score</div>
     </div>`;
}


/* ── Metric-backed renderers ───────────────────────────────────────────── */

//: Chart.js instances, keyed by widget id. A widget repaints every 15s and
//: rebuilding the chart each time would leak an instance per tick and throw
//: away the animation, so the canvas is created once and the data swapped.
const _WCHARTS = new Map();

function _wMetricUrl(settings, hours, limit) {
  const from = new Date(Date.now() - (hours || 24) * 3600 * 1000).toISOString();
  return `/api/v1/metrics/${encodeURIComponent(settings.host)}`
       + `/${encodeURIComponent(settings.category)}`
       + `/${encodeURIComponent(settings.metric)}/`
       + `?from=${from}&limit=${limit}`;
}

function _wNeedsHost(body, settings) {
  if (settings.host) return false;
  _wEmpty(body, 'Pick a host in this widget\u2019s settings');
  return true;
}

async function _renderMetricChart(body, settings, widget) {
  if (_wNeedsHost(body, settings)) return;
  const hours = Number(settings.range_hours) || 24;
  const points = _wRows(await _wCached(_wMetricUrl(settings, hours, 500), 12000));

  let chart = _WCHARTS.get(widget.id);
  // The body is rebuilt whenever the grid re-renders, which orphans the canvas
  // the chart was drawn on — so verify it is still in the document.
  if (chart && !body.contains(chart.canvas)) { chart.destroy(); chart = null; }
  if (!chart) {
    body.innerHTML = '<div class="dash-chart"><canvas></canvas></div>';
    const ctx = body.querySelector('canvas').getContext('2d');
    chart = new Chart(ctx, {
      type: 'line',
      data: { datasets: [{
        label: `${settings.category}/${settings.metric}`,
        data: [], borderColor: 'var(--sky)', backgroundColor: 'rgba(130,196,238,.14)',
        borderWidth: 2, pointRadius: 0, fill: true, tension: 0.25,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { type: 'time', ticks: { maxTicksLimit: 5, color: '#8b8ba3' },
               grid: { color: 'rgba(255,255,255,.05)' } },
          y: { beginAtZero: true, ticks: { maxTicksLimit: 4, color: '#8b8ba3' },
               grid: { color: 'rgba(255,255,255,.05)' } },
        },
        plugins: { legend: { display: false } },
      },
    });
    _WCHARTS.set(widget.id, chart);
  }
  if (!points.length) {
    _wEmpty(body, 'No readings in this window');
    chart.destroy();
    _WCHARTS.delete(widget.id);
    return;
  }
  // Oldest first: the API returns newest first and a time axis drawn backwards
  // renders as a single spike.
  chart.data.datasets[0].data = points
    .map(pt => ({ x: new Date(pt.time).getTime(), y: pt.value }))
    .sort((a, b) => a.x - b.x);
  chart.update('none');
}

async function _renderGauge(body, settings) {
  if (_wNeedsHost(body, settings)) return;
  const points = _wRows(await _wCached(_wMetricUrl(settings, 1, 1), 12000));
  if (!points.length) { _wEmpty(body, 'No recent reading'); return; }
  const value = Number(points[0].value);
  const pct = Math.max(0, Math.min(100, value));
  const circumference = 2 * Math.PI * 34;
  const offset = circumference * (1 - pct / 100);
  const colour = pct >= 90 ? 'var(--rose)' : pct >= 75 ? 'var(--lemon)' : 'var(--mint)';
  body.innerHTML = `
    <div class="dash-gauge">
      <svg viewBox="0 0 80 80" class="dash-gauge-ring">
        <circle cx="40" cy="40" r="34" class="gauge-bg"/>
        <circle cx="40" cy="40" r="34" stroke="${colour}"
                stroke-dasharray="${circumference.toFixed(1)}"
                stroke-dashoffset="${offset.toFixed(1)}" class="gauge-fill"/>
      </svg>
      <div class="dash-gauge-text" style="color:${colour};">${Math.round(value)}%</div>
      <div class="dash-stat-label">${escHtml(settings.metric || '')}</div>
    </div>`;
}

async function _renderDockerContainers(body, settings) {
  if (_wNeedsHost(body, settings)) return;
  const rows = _wRows(await _wCached(
    `/api/v1/hosts/${encodeURIComponent(settings.host)}/containers/`));
  if (!rows.length) { _wEmpty(body, 'No containers reported'); return; }
  body.innerHTML = `<div class="dash-hosts">` + rows.map(c => `
    <div class="dash-host" title="${escAttr(c.image || '')}">
      <span class="dash-host-dot dash-dot-${c.state === 'running' ? 'online' : 'offline'}"></span>
      <span class="dash-host-name">${escHtml(c.name || '')}</span>
      <span class="dash-host-meta">${escHtml(c.status || c.state || '')}</span>
    </div>`).join('') + `</div>`;
}

async function _renderTopProcesses(body, settings) {
  if (_wNeedsHost(body, settings)) return;
  const metric = settings.sort_by === 'memory' ? 'memory_percent' : 'cpu_percent';
  const points = _wRows(await _wCached(
    _wMetricUrl({ host: settings.host, category: 'process', metric }, 1, 50), 12000));
  if (!points.length) { _wEmpty(body, 'No process data reported'); return; }
  // One point per process per scrape; keep the newest reading for each name.
  const latest = new Map();
  for (const pt of points) {
    const name = (pt.labels || {}).process || (pt.labels || {}).name || '?';
    if (!latest.has(name)) latest.set(name, pt.value);
  }
  const rows = [...latest.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);
  body.innerHTML = `<div class="dash-procs">` + rows.map(([name, value]) => `
    <div class="dash-proc">
      <span class="dash-proc-name">${escHtml(name)}</span>
      <span class="dash-proc-value">${Number(value).toFixed(1)}%</span>
    </div>`).join('') + `</div>`;
}

/* Kinds whose renderers land in the next phase. Named rather than missing, so
   the widget says what it is instead of looking broken. */
function _renderPending(body, _settings, widget) {
  body.innerHTML =
    `<div class="dash-empty muted-note">`
    + `${escHtml((widget && widget.kind) || 'This widget')} renders in the next phase.`
    + `</div>`;
}

const WIDGET_RENDERERS = {
  stat_tile: _renderStatTile,
  host_status_grid: _renderHostStatusGrid,
  alert_list: _renderAlertList,
  rollout_progress: _renderRolloutProgress,
  vuln_score: _renderVulnScore,
  metric_chart: _renderMetricChart,
  gauge: _renderGauge,
  docker_containers: _renderDockerContainers,
  top_processes: _renderTopProcesses,
};

/* ── Catalog and settings — phase 04 ───────────────────────────────────── */

function openWidgetCatalog() {
  showToast('The widget catalog lands in the next phase', 'info');
}

function openWidgetSettings(_widgetId) {
  showToast('Per-widget settings land in the next phase', 'info');
}
