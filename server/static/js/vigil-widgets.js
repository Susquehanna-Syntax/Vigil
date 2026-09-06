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
  metric_chart: _renderPending,
  gauge: _renderPending,
  docker_containers: _renderPending,
  top_processes: _renderPending,
};

/* ── Catalog and settings — phase 04 ───────────────────────────────────── */

function openWidgetCatalog() {
  showToast('The widget catalog lands in the next phase', 'info');
}

function openWidgetSettings(_widgetId) {
  showToast('Per-widget settings land in the next phase', 'info');
}
