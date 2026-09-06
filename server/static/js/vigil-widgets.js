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

/* ── Per-widget settings ───────────────────────────────────────────────────
   The form is generated from the registry's typed settings schema, so adding a
   setting to a widget means editing one dict rather than a dict and a form. */

//: Option lists for the picker field types, fetched once per settings open.
async function _settingsOptions(type) {
  if (type === 'host') {
    const rows = _wRows(await _wCached('/api/v1/hosts/', 30000));
    return rows.map(h => [h.id, h.hostname]);
  }
  if (type === 'playbook') {
    const rows = _wRows(await _wCached('/api/v1/playbooks/', 30000));
    return rows.map(b => [b.id, b.name]);
  }
  if (type === 'definition') {
    const rows = _wRows(await _wCached('/api/v1/tasks/definitions/?scope=mine', 30000));
    return rows.map(d => [d.id, d.name]);
  }
  if (type === 'metric') {
    // Only what the fleet is actually reporting — a picker offering a metric
    // nothing sends produces a widget that looks broken rather than unset.
    //
    // The value carries the category too. Several categories report a metric
    // called usage_percent, so a value of just "usage_percent" is ambiguous:
    // the select would land on whichever came first and could then disagree
    // with the category field sitting next to it.
    const rows = _wRows(await _wCached('/api/v1/metrics/catalog/', 30000));
    return rows.map(m => [`${m.category}/${m.metric}`, m.label]);
  }
  return [];
}

function _settingsField(name, field, value, options) {
  const id = `ws-${name}`;
  const label = `<label class="form-label" for="${escAttr(id)}">${escHtml(field.label || name)}</label>`;
  const t = field.type;

  if (t === 'bool') {
    return `<label class="setting-check">
      <input type="checkbox" id="${escAttr(id)}" data-setting="${escAttr(name)}"
             ${value ? 'checked' : ''}> ${escHtml(field.label || name)}</label>`;
  }
  if (t === 'longtext') {
    return `<div class="form-group">${label}
      <textarea class="form-control" id="${escAttr(id)}" rows="5"
                data-setting="${escAttr(name)}">${escHtml(value ?? '')}</textarea></div>`;
  }
  if (t === 'int') {
    return `<div class="form-group">${label}
      <input type="number" class="form-control" id="${escAttr(id)}"
             data-setting="${escAttr(name)}" value="${escAttr(value ?? field.default ?? 0)}"
             ${field.min !== undefined ? `min="${escAttr(field.min)}"` : ''}
             ${field.max !== undefined ? `max="${escAttr(field.max)}"` : ''}></div>`;
  }
  if (t === 'choice' || options.length) {
    const list = t === 'choice'
      ? (field.options || []).map(o => [o, o])
      : options;
    const blank = t === 'choice' ? '' : `<option value="">— none —</option>`;
    return `<div class="form-group">${label}
      <select class="form-control" id="${escAttr(id)}" data-setting="${escAttr(name)}">
        ${blank}${list.map(([v, text]) =>
          `<option value="${escAttr(v)}"${String(v) === String(value) ? ' selected' : ''}>${escHtml(text)}</option>`).join('')}
      </select></div>`;
  }
  // A picker type whose list came back empty still needs to be editable, so it
  // falls through to a plain text field rather than rendering nothing.
  return `<div class="form-group">${label}
    <input type="text" class="form-control" id="${escAttr(id)}"
           data-setting="${escAttr(name)}" value="${escAttr(value ?? '')}"
           ${field.placeholder ? `placeholder="${escAttr(field.placeholder)}"` : ''}></div>`;
}

async function openWidgetSettings(widgetId) {
  const widget = (DASH.current.widgets || []).find(w => String(w.id) === String(widgetId));
  if (!widget) return;
  const spec = (DASH.catalog.widgets || {})[widget.kind] || {};
  const schema = spec.settings || {};

  const m = mountModal('widget-settings');
  m.setBody(`<div class="modal-title"><span>${escHtml(spec.label || widget.kind)}</span>
      <button class="modal-close" id="ws-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button></div>
    <div id="ws-body"><span class="muted-note">Loading…</span></div>`);
  m.modal.querySelector('#ws-x').onclick = m.close;
  m.open();

  if (!Object.keys(schema).length) {
    document.getElementById('ws-body').innerHTML =
      `<p class="muted-note">${escHtml(spec.description || '')}</p>
       <p class="muted-note">This widget has nothing to configure.</p>
       <div class="modal-actions"><button class="btn btn-outline btn-sm" id="ws-close">Close</button></div>`;
    document.getElementById('ws-close').onclick = m.close;
    return;
  }

  // Fetch each picker's options once, in parallel, before drawing the form.
  const types = [...new Set(Object.values(schema).map(f => f.type))];
  const lists = {};
  await Promise.all(types.map(async (t) => {
    try { lists[t] = await _settingsOptions(t); } catch (e) { lists[t] = []; }
  }));

  // A metric picker chooses the category/metric pair together, so the separate
  // category field beside it is not drawn — two controls for one fact is how
  // they end up disagreeing.
  const metricField = Object.entries(schema).find(([, f]) => f.type === 'metric');
  const current = widget.settings || {};
  const fields = Object.entries(schema)
    .filter(([name]) => !(metricField && name === 'category'))
    .map(([name, field]) => {
      let value = current[name];
      if (field.type === 'metric') value = `${current.category || ''}/${value || ''}`;
      return _settingsField(name, field, value, lists[field.type] || []);
    }).join('');

  document.getElementById('ws-body').innerHTML =
    `${spec.description ? `<p class="muted-note" style="margin-bottom:14px;">${escHtml(spec.description)}</p>` : ''}
     ${fields}
     <div class="modal-actions">
       <button class="btn btn-outline btn-sm" id="ws-cancel">Cancel</button>
       <button class="btn btn-mint btn-sm" id="ws-save">Save</button>
     </div>`;

  document.getElementById('ws-cancel').onclick = m.close;
  document.getElementById('ws-save').onclick = () => {
    const next = {};
    for (const [name, field] of Object.entries(schema)) {
      const el = document.querySelector(`[data-setting="${CSS.escape(name)}"]`);
      if (!el) continue;
      if (field.type === 'bool') next[name] = el.checked;
      else if (field.type === 'int') next[name] = parseInt(el.value, 10);
      else if (field.type === 'metric') {
        const [category, metric] = String(el.value).split('/');
        next[name] = metric || '';
        if ('category' in schema) next.category = category || '';
      } else next[name] = el.value;
    }
    // The category field was not drawn when a metric picker owns it; keep
    // whatever the picker just derived, or what was there before.
    if (metricField && 'category' in schema && next.category === undefined) {
      next.category = current.category;
    }
    widget.settings = next;
    DASH.dirty = true;
    m.close();
    // The title lives in the widget's header, and _dashPaintAll only repaints
    // bodies — so without this the new name does not appear until a reload.
    const shell = document.querySelector(`[data-widget-body="${CSS.escape(String(widget.id))}"]`)
      ?.closest('.dash-widget');
    const heading = shell && shell.querySelector('.dash-widget-title');
    if (heading) heading.textContent = (next.title || '').trim() || spec.label || widget.kind;
    // Repaint just this widget so the change is visible without a round trip;
    // the value itself is persisted with the rest of the layout on save.
    _dashPaintAll();
    showToast('Widget updated — saved when you leave edit mode', 'success');
  };
}
