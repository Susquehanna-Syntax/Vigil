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

/* ── List cards ────────────────────────────────────────────────────────────
   Every list widget renders through _dashCard so they read alike: a coloured
   left edge for what the row is about, a title, a thin sub-line, and an
   optional right column. Cards carrying data-nav lead somewhere. */

//: Row meaning → SQSY accent. Shared by alerts and findings, whose severity
//: vocabularies overlap but are not identical.
const _EDGE_BY_SEVERITY = {
  critical: 'rose', high: 'rose', warning: 'lemon', medium: 'peach',
  info: 'sky', low: 'sky',
};
const _EDGE_BY_RUN_STATE = {
  completed: 'mint', failed: 'rose', timeout: 'rose', rejected: 'rose',
  running: 'sky', pending: 'grey', skipped: 'grey',
};

function _dashCard(o) {
  const edge = `edge-${o.edge || 'grey'}`;
  //: The nav target rides on the card as data, never as an onclick string —
  //: these fields carry hostnames and container names, which come from agents.
  const nav = o.nav
    ? ` data-nav="${escAttr(o.nav.page)}"`
      + (o.nav.tab ? ` data-nav-tab="${escAttr(o.nav.tab)}"` : '')
      + (o.nav.anchor ? ` data-nav-anchor="${escAttr(o.nav.anchor)}"` : '')
      + (o.nav.host ? ` data-nav-host="${escAttr(o.nav.host)}"` : '')
    : '';
  const right = (o.right || o.rightSub)
    ? `<div class="dash-card-right">${o.right ? escHtml(o.right) : ''}`
      + `${o.rightSub ? `<div class="dash-card-sub">${escHtml(o.rightSub)}</div>` : ''}</div>`
    : '';
  return `<div class="dash-card ${edge}"${nav} title="${escAttr(o.tip || o.title || '')}">
    ${o.dot ? `<span class="dash-card-dot ${edge}"></span>` : ''}
    <div class="dash-card-main">
      <div class="dash-card-title">${escHtml(o.title || '')}</div>
      ${o.sub ? `<div class="dash-card-sub">${escHtml(o.sub)}</div>` : ''}
    </div>
    ${right}
  </div>`;
}

function _dashCards(items) {
  return `<div class="dash-cards">${items.join('')}</div>`;
}

//: Take the operator to the thing the card names. A host card opens the host
//: detail instead of navigating, because hosts have no page of their own.
function _dashNavFromCard(card) {
  const d = card.dataset;
  if (d.navHost) {
    const real = document.querySelector(`.host-card[data-id="${CSS.escape(d.navHost)}"]`);
    if (real && typeof openHostDetail === 'function') { openHostDetail(real); return; }
  }
  if (!d.nav) return;
  navigateTo(d.nav);
  if (d.navTab) document.querySelector(`.tab[data-tab="${CSS.escape(d.navTab)}"]`)?.click();
  if (d.navAnchor) {
    const target = document.getElementById(d.navAnchor);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.remove('dash-flash');
      void target.offsetWidth;                 // restart the animation
      target.classList.add('dash-flash');
    }
  }
}

//: Bound once on the document — widget bodies are replaced on every refresh
//: poll, so a listener attached to a card would not survive the next paint.
document.addEventListener('click', (ev) => {
  const card = ev.target.closest && ev.target.closest('.dash-card[data-nav], .dash-card[data-nav-host]');
  if (card) _dashNavFromCard(card);
});

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

//: Mirrors Host.Mode.choices. The host list API sends the raw value; the
//: badge wants the label the server-rendered card shows.
const _MODE_LABEL = { monitor: 'Monitor', managed: 'Managed', full_control: 'Full Control' };

/* The widget builds the same .host-card markup templates/_host_card.html
   renders, carrying the same data-* attributes. That is deliberate rather than
   duplicative: openHostDetail() reads only the dataset, and refreshHostCards()
   finds cards by class, so matching the contract gives the widget the detail
   modal, the OS logo and the five live metric bars without re-implementing any
   of them. */
function _dashHostCard(h) {
  const os = h.os || 'Unknown OS';
  const tags = (h.tags || []).map(String);
  return `<div class="host-card" data-id="${escAttr(h.id)}"
       data-hostname="${escAttr(h.hostname)}"
       data-os="${escAttr(os)}"
       data-ip="${escAttr(h.ip_address || '—')}"
       data-status="${escAttr(h.status)}"
       data-mode="${escAttr(h.mode)}"
       data-mode-display="${escAttr(_MODE_LABEL[h.mode] || h.mode)}"
       data-last-checkin="${escAttr(h.last_checkin || '')}"
       data-agent-version="${escAttr(h.agent_version || '')}"
       data-reboot-required="${h.reboot_required ? '1' : ''}"
       data-kernel="${escAttr(h.kernel || '')}"
       data-tags="${escAttr(tags.join(','))}">
    <div class="host-card-id">
      <div class="host-card-id-row">
        <div class="status-dot ${escAttr(h.status)}"></div>
        <div class="host-name">${escHtml(h.hostname)}</div>
        <div class="mode-badge mode-${escAttr(h.mode)}">${escHtml(_MODE_LABEL[h.mode] || h.mode)}</div>
      </div>
      <div class="host-card-id-meta">
        <span class="host-os-logo" data-os="${escAttr(os)}"></span>
        ${escHtml(os)} · ${escHtml(h.ip_address || '—')}
      </div>
      ${tags.length ? `<div class="host-tag-row">${
        tags.map(t => `<span class="host-tag-chip">${escHtml(t)}</span>`).join('')}</div>` : ''}
    </div>
    <div class="host-metrics">
      ${[['cpu', 'CPU', 'sky'], ['memory', 'Memory', 'lavender'], ['disk', 'Disk', 'mint'],
         ['net-in', 'Net In', 'peach'], ['net-out', 'Net Out', 'lemon']].map(([k, label, colour]) => `
        <div class="host-metric" data-metric="${k}">
          <div class="host-metric-label">${label}</div>
          <div class="host-metric-value">—</div>
          <div class="host-metric-bar">
            <div class="host-metric-fill" style="width: 0%; background: var(--${colour});"></div>
          </div>
        </div>`).join('')}
    </div>
    <div class="host-card-actions">
      <div class="host-checkin">${escHtml(h.last_checkin ? timeAgo(h.last_checkin) : 'Never')}</div>
      <button type="button" class="btn btn-ghost" data-host-act="rdp">RDP</button>
      <button type="button" class="btn btn-sky" data-host-act="deploy">Deploy</button>
      <button type="button" class="btn btn-ghost btn-rose-ghost" data-host-act="remove"
              title="Remove agent">Remove</button>
    </div>
  </div>`;
}

/* templates/_host_card.html carries these as inline onclick attributes built by
   the template engine — but nothing has included that template since the
   classic dashboard was retired, so this widget is the only place host cards
   exist now and it owns their behaviour. The values are hostnames reported by
   agents, so they stay in data-* and every handler is delegated. */
document.addEventListener('click', (ev) => {
  const card = ev.target.closest && ev.target.closest('.dash-host-cards .host-card');
  if (card && !ev.target.closest('[data-host-act]') && typeof openHostDetail === 'function') {
    openHostDetail(card);
  }
});

document.addEventListener('click', (ev) => {
  const btn = ev.target.closest && ev.target.closest('.dash-host-cards [data-host-act]');
  if (!btn) return;
  ev.stopPropagation();                      // never also open the detail modal
  const card = btn.closest('.host-card');
  const { id, hostname } = card.dataset;
  const act = btn.dataset.hostAct;
  if (act === 'rdp' && typeof downloadHostRdp === 'function') downloadHostRdp(id, hostname);
  if (act === 'deploy' && typeof openDeployForHost === 'function') openDeployForHost(id);
  if (act === 'remove' && typeof deleteHost === 'function') deleteHost(id, hostname, btn);
});

async function _renderHostStatusGrid(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/hosts/'));
  const want = (settings.tag_filter || '').trim().toLowerCase();
  let shown = want
    ? rows.filter(h => (h.tags || []).some(t => String(t).toLowerCase() === want))
    : rows;
  if (settings.status && settings.status !== 'any') {
    shown = shown.filter(h => h.status === settings.status);
  }
  if (!shown.length) {
    _wEmpty(body, want ? `No hosts tagged ${settings.tag_filter}` : 'No hosts yet');
    return;
  }
  // Offline and pending first: a fleet view is read for what is wrong with it.
  const rank = { offline: 0, pending: 1, online: 2 };
  shown = [...shown].sort((a, b) =>
    (rank[a.status] ?? 3) - (rank[b.status] ?? 3) || a.hostname.localeCompare(b.hostname));

  // show_search has been in the registry since the widget shipped but was
  // never rendered. The filter is local to this widget's cards.
  const search = settings.show_search === false ? '' :
    `<input type="text" class="form-control dash-host-search" placeholder="Filter hosts…">`;
  body.innerHTML = search
    + `<div class="dash-host-cards">${shown.map(_dashHostCard).join('')}</div>`;

  const input = body.querySelector('.dash-host-search');
  if (input) {
    input.addEventListener('input', () => {
      const q = input.value.trim().toLowerCase();
      body.querySelectorAll('.host-card').forEach((card) => {
        const d = card.dataset;
        const hay = `${d.hostname} ${d.os} ${d.ip} ${d.tags}`.toLowerCase();
        card.classList.toggle('hidden', Boolean(q) && !hay.includes(q));
      });
    });
  }
  // The logos and the live metric bars come from the hosts page's own code,
  // scoped to this widget so it does not re-fetch metrics for the whole page.
  if (typeof _injectOsLogos === 'function') _injectOsLogos(body);
  if (typeof refreshHostCards === 'function') refreshHostCards(body);
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
  body.innerHTML = _dashCards(shown.map(a => _dashCard({
    edge: _EDGE_BY_SEVERITY[a.severity] || 'lemon',
    title: a.message || '(no message)',
    sub: [a.host_hostname, timeAgo(a.fired_at)].filter(Boolean).join(' · '),
    right: a.severity || '',
    tip: `${a.severity || ''} · ${a.message || ''}`,
    nav: { page: 'alerts' },
  })));
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
  body.innerHTML = _dashCards(rows.map(c => _dashCard({
    edge: c.state === 'running' ? 'mint' : 'grey',
    dot: true,
    title: c.name || '(unnamed)',
    sub: c.image || '',
    right: c.state || '',
    rightSub: c.status || '',
    tip: c.image || c.name || '',
    nav: { page: 'inventory', host: settings.host },
  })));
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


/* ── Fleet conversions ─────────────────────────────────────────────────── */

async function _renderInactiveHosts(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/hosts/'));
  const days = Number(settings.days) || 90;
  const cutoff = Date.now() - days * 86400000;
  const quiet = rows.filter(h => h.status === 'offline'
    && (!h.last_checkin || new Date(h.last_checkin).getTime() < cutoff));
  if (!quiet.length) { _wEmpty(body, `Nothing quiet for ${days} days`); return; }
  body.innerHTML = `<div class="dash-hosts">` + quiet.map(h => `
    <div class="dash-host">
      <span class="dash-host-dot dash-dot-offline"></span>
      <span class="dash-host-name">${escHtml(h.hostname)}</span>
      <span class="dash-host-meta">${h.last_checkin
        ? escHtml(new Date(h.last_checkin).toLocaleDateString()) : 'never'}</span>
    </div>`).join('') + `</div>`;
}

async function _renderPendingEnrollments(body) {
  const rows = _wRows(await _wCached('/api/v1/hosts/', 5000))
    .filter(h => h.status === 'pending');
  if (!rows.length) { _wEmpty(body, 'Nothing waiting for approval'); return; }
  body.innerHTML = `<div class="dash-alerts">` + rows.map(h => `
    <div class="dash-alert">
      <span class="dash-host-dot dash-dot-pending"></span>
      <span class="dash-alert-msg"><strong>${escHtml(h.hostname)}</strong>
        <span class="muted-note">${escHtml(h.ip_address || '')}</span></span>
      <span style="margin-left:auto;display:flex;gap:6px;">
        <button class="btn btn-mint btn-xs" data-approve="${escAttr(h.id)}">Approve</button>
        <button class="btn btn-rose btn-xs" data-reject="${escAttr(h.id)}">Reject</button>
      </span>
    </div>`).join('') + `</div>`;
  // Enrollment is a real decision, so it confirms rather than acting on a
  // stray click in a widget somebody may only be glancing at.
  body.querySelectorAll('[data-approve],[data-reject]').forEach((btn) => {
    btn.onclick = async () => {
      const approve = 'approve' in btn.dataset;
      const id = approve ? btn.dataset.approve : btn.dataset.reject;
      const name = btn.closest('.dash-alert').querySelector('strong').textContent;
      if (!(await confirmModal(
        `${approve ? 'Approve' : 'Reject'} ${name}?`,
        { confirmText: approve ? 'Approve' : 'Reject', danger: !approve }))) return;
      try {
        await apiJson(`/api/v1/hosts/${id}/${approve ? 'approve' : 'reject'}/`,
                      { method: 'POST', body: JSON.stringify({}) });
        _WCACHE.delete('/api/v1/hosts/');
        showToast(`${name} ${approve ? 'approved' : 'rejected'}`, 'success');
        _dashPaintAll();
      } catch (e) { showToast(e.message || 'Failed', 'error'); }
    };
  });
}

/* ── Fleet health ──────────────────────────────────────────────────────── */

function _hostListWidget(body, rows, emptyMessage, meta) {
  if (!rows.length) { _wEmpty(body, emptyMessage); return; }
  body.innerHTML = `<div class="dash-hosts">` + rows.map(h => `
    <div class="dash-host">
      <span class="dash-host-dot dash-dot-${escAttr(h.status || 'offline')}"></span>
      <span class="dash-host-name">${escHtml(h.hostname)}</span>
      <span class="dash-host-meta">${escHtml(meta(h))}</span>
    </div>`).join('') + `</div>`;
}

async function _renderRebootRequired(body) {
  const rows = _wRows(await _wCached('/api/v1/hosts/')).filter(h => h.reboot_required);
  _hostListWidget(body, rows, 'No host is waiting on a reboot', h => h.os || '');
}

async function _renderOutdatedAgents(body) {
  const [hosts, about] = await Promise.all([
    _wCached('/api/v1/hosts/'),
    _wCached('/api/v1/about/', 60000).catch(() => ({})),
  ]);
  const current = about.expected_agent_version || '';
  if (!current) { _wEmpty(body, 'Server does not report an expected version'); return; }
  // isOlderVersion compares numerically per segment — "2026.10.0" is newer than
  // "2026.9.0", which a string compare gets backwards.
  const behind = _wRows(hosts).filter(h =>
    h.agent_version && isOlderVersion(h.agent_version, current));
  _hostListWidget(body, behind, `Every agent is on ${current}`,
                  h => `${h.agent_version} → ${current}`);
}

async function _renderDiskPressure(body, settings) {
  const hosts = _wRows(await _wCached('/api/v1/hosts/'));
  const floor = Number(settings.threshold) || 0;
  const limit = Number(settings.limit) || 8;
  const readings = await Promise.all(hosts.map(async (h) => {
    try {
      const points = _wRows(await _wCached(
        `/api/v1/metrics/${h.id}/disk/usage_percent/?limit=1`, 30000));
      return points.length ? { host: h, value: Number(points[0].value) } : null;
    } catch (e) { return null; }
  }));
  const rows = readings.filter(r => r && r.value >= floor)
    .sort((a, b) => b.value - a.value).slice(0, limit);
  if (!rows.length) { _wEmpty(body, 'No disk readings'); return; }
  body.innerHTML = `<div class="dash-procs">` + rows.map(({ host, value }) => {
    const colour = value >= 90 ? 'var(--rose)' : value >= 75 ? 'var(--lemon)' : 'var(--mint)';
    return `<div class="dash-proc">
      <span class="dash-proc-name">${escHtml(host.hostname)}</span>
      <span class="dash-proc-value" style="color:${colour};">${value.toFixed(0)}%</span>
    </div>`;
  }).join('') + `</div>`;
}

async function _renderNetworkThroughput(body, settings, widget) {
  if (_wNeedsHost(body, settings)) return;
  const hours = Number(settings.range_hours) || 24;
  const [sent, recv] = await Promise.all([
    _wCached(_wMetricUrl({ host: settings.host, category: 'network', metric: 'bytes_sent' }, hours, 300), 12000),
    _wCached(_wMetricUrl({ host: settings.host, category: 'network', metric: 'bytes_recv' }, hours, 300), 12000),
  ]);
  const series = [_wRows(sent), _wRows(recv)];
  if (!series[0].length && !series[1].length) { _wEmpty(body, 'No readings in this window'); return; }
  let chart = _WCHARTS.get(widget.id);
  if (chart && !body.contains(chart.canvas)) { chart.destroy(); chart = null; }
  if (!chart) {
    body.innerHTML = '<div class="dash-chart"><canvas></canvas></div>';
    chart = new Chart(body.querySelector('canvas').getContext('2d'), {
      type: 'line',
      data: { datasets: [
        { label: 'out', data: [], borderColor: '#82c4ee', borderWidth: 2, pointRadius: 0, tension: .25 },
        { label: 'in',  data: [], borderColor: '#7eddb5', borderWidth: 2, pointRadius: 0, tension: .25 },
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { type: 'time', ticks: { maxTicksLimit: 5, color: '#8b8ba3' },
               grid: { color: 'rgba(255,255,255,.05)' } },
          y: { beginAtZero: true, ticks: { maxTicksLimit: 4, color: '#8b8ba3' },
               grid: { color: 'rgba(255,255,255,.05)' } },
        },
        plugins: { legend: { display: true, labels: { color: '#8b8ba3', boxWidth: 10 } } },
      },
    });
    _WCHARTS.set(widget.id, chart);
  }
  series.forEach((points, i) => {
    chart.data.datasets[i].data = points
      .map(pt => ({ x: new Date(pt.time).getTime(), y: pt.value }))
      .sort((a, b) => a.x - b.x);
  });
  chart.update('none');
}

/* ── Work in flight ────────────────────────────────────────────────────── */

const _RUN_STATE_COLOUR = {
  completed: 'var(--mint)', failed: 'var(--rose)', timeout: 'var(--rose)',
  rejected: 'var(--rose)', running: 'var(--sky)', pending: 'var(--text-3)',
  skipped: 'var(--text-3)',
};

async function _renderTaskHistory(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/tasks/history/', 8000));
  const failures = new Set(['failed', 'timeout', 'rejected']);
  const shown = rows
    .filter(t => !settings.failures_only || failures.has(t.state))
    .slice(0, Number(settings.limit) || 10);
  if (!shown.length) {
    _wEmpty(body, settings.failures_only ? 'No failures' : 'Nothing has run yet');
    return;
  }
  body.innerHTML = _dashCards(shown.map(t => _dashCard({
    edge: _EDGE_BY_RUN_STATE[t.state] || 'grey',
    title: t.step_label || t.action || '(no step)',
    sub: [t.host_hostname, timeAgo(t.completed_at || t.created_at)]
      .filter(Boolean).join(' · '),
    right: t.state || '',
    tip: `${t.state || ''} · ${t.step_label || t.action || ''}`,
    nav: { page: 'tasks', tab: 'tasks-history' },
  })));
}

async function _renderWaveStatus(body, settings) {
  const wanted = (settings.group_tag || '').trim();
  const url = wanted ? `/api/v1/waves/?group=${encodeURIComponent(wanted)}` : '/api/v1/waves/';
  const rows = _wRows(await _wCached(url));
  if (!rows.length) { _wEmpty(body, wanted ? `No wave tagged ${wanted}` : 'No waves yet'); return; }
  body.innerHTML = _dashCards(rows.map(w => _dashCard({
    edge: 'lav',
    title: `${w.order} · ${w.name}`,
    sub: (w.group_tags || []).join(' · '),
    right: String(w.exclusive_host_count ?? w.host_count ?? 0),
    rightSub: 'hosts',
    tip: w.name,
    nav: { page: 'playbooks', anchor: 'wave-panel' },
  })));
}

async function _renderPlaybookCoverage(body, settings) {
  if (!settings.playbook) { _wEmpty(body, 'Pick a playbook in this widget\u2019s settings'); return; }
  const [playbooks, hosts] = await Promise.all([
    _wCached('/api/v1/playbooks/'), _wCached('/api/v1/hosts/'),
  ]);
  const book = _wRows(playbooks).find(b => String(b.id) === String(settings.playbook));
  if (!book) { _wEmpty(body, 'That playbook no longer exists'); return; }
  const targets = (book.target_tags || []).map(t => String(t).toLowerCase());
  const done = String(book.completion_tag || '').toLowerCase();
  const matching = _wRows(hosts).filter((h) => {
    const tags = (h.tags || []).map(t => String(t).toLowerCase());
    return targets.length ? targets.some(t => tags.includes(t)) : true;
  });
  const ran = matching.filter(h =>
    done && (h.tags || []).some(t => String(t).toLowerCase() === done));
  const pct = matching.length ? Math.round(ran.length / matching.length * 100) : 0;
  body.innerHTML = `
    <div class="dash-stat">
      <div class="dash-stat-value" style="color:${pct === 100 ? 'var(--mint)' : 'var(--lemon)'};">
        ${ran.length}<span style="font-size:18px;color:var(--text-3);">/${matching.length}</span></div>
      <div class="dash-stat-label">${escHtml(book.name)}${done ? '' : ' — no completion tag set'}</div>
      <div class="dash-cover-bar"><span style="width:${pct}%;"></span></div>
    </div>`;
}

async function _renderAutomationActivity(body, settings) {
  const payload = await _wCached('/api/v1/automations/');
  const rows = (payload && payload.automations) || _wRows(payload);
  const shown = rows.slice(0, Number(settings.limit) || 8);
  if (!shown.length) { _wEmpty(body, 'No automations yet'); return; }
  body.innerHTML = _dashCards(shown.map(a => _dashCard({
    edge: a.enabled ? 'mint' : 'grey',
    dot: true,
    title: a.name || '(unnamed)',
    sub: a.enabled ? 'enabled' : 'disabled',
    right: a.last_run ? timeAgo(a.last_run) : 'never',
    rightSub: a.last_run ? 'last run' : '',
    tip: a.name || '',
    nav: { page: 'playbooks', anchor: 'automations-list' },
  })));
}

/* ── Security ──────────────────────────────────────────────────────────── */

const _VULN_RANK = { low: 0, medium: 1, high: 2, critical: 3 };

async function _renderVulnFindings(body, settings) {
  const rows = _wRows(await _wCached('/api/v1/vulns/findings/'));
  const floor = _VULN_RANK[settings.severity] ?? 2;
  const shown = rows
    .filter(f => f.state === 'open')
    .filter(f => (_VULN_RANK[f.severity] ?? 0) >= floor)
    .sort((a, b) => (_VULN_RANK[b.severity] ?? 0) - (_VULN_RANK[a.severity] ?? 0))
    .slice(0, Number(settings.limit) || 10);
  if (!shown.length) { _wEmpty(body, `Nothing open at ${settings.severity} or above`); return; }
  body.innerHTML = _dashCards(shown.map(f => _dashCard({
    edge: _EDGE_BY_SEVERITY[f.severity] || 'grey',
    title: f.cve_id || f.title || '(untitled finding)',
    // The title often restates the CVE already in the heading, so it only
    // earns its place when it says something the heading does not.
    sub: [f.host_hostname, f.package_name,
          (f.cve_id && f.title && !f.title.includes(f.cve_id)) ? f.title : '']
      .filter(Boolean).join(' · '),
    right: f.severity || '',
    // days_remaining goes negative once a finding is past due; trust the
    // number rather than only the flag, so nothing reads "-51d left".
    rightSub: (f.overdue || (f.days_remaining ?? 0) < 0) ? 'overdue'
      : (typeof f.days_remaining === 'number' ? `${f.days_remaining}d left` : ''),
    tip: f.title || f.cve_id || '',
    nav: { page: 'vulns' },
  })));
}

async function _renderFirewallStatus(body, settings) {
  if (_wNeedsHost(body, settings)) return;
  let payload;
  try {
    payload = await _wCached(`/api/v1/hosts/${encodeURIComponent(settings.host)}/firewall/`, 20000);
  } catch (e) { _wEmpty(body, 'No firewall data for this host'); return; }
  const rules = _wRows(payload.rules || payload);
  const backend = payload.backend || payload.name || 'unknown';
  const active = payload.enabled ?? payload.active;
  body.innerHTML = `
    <div class="dash-stat">
      <div class="dash-stat-value" style="color:${active === false ? 'var(--rose)' : 'var(--mint)'};font-size:22px;">
        ${escHtml(String(backend))}</div>
      <div class="dash-stat-label">${rules.length} rule${rules.length === 1 ? '' : 's'}${
        active === false ? ' · inactive' : ''}</div>
    </div>`;
}

async function _renderWindowsPatches(body) {
  const rows = _wRows(await _wCached('/api/v1/hosts/'))
    .filter(h => /windows/i.test(h.os || ''));
  if (!rows.length) { _wEmpty(body, 'No Windows hosts'); return; }
  // Sort by what is missing, worst first; hosts that have never reported sink
  // to the bottom rather than reading as clean.
  const counted = rows.map(h => ({ host: h, u: h.windows_updates || null }));
  counted.sort((a, b) => (b.u ? b.u.pending : -1) - (a.u ? a.u.pending : -1));
  body.innerHTML = `<div class="dash-procs">` + counted.map(({ host, u }) => {
    if (!u) {
      return `<div class="dash-proc">
        <span class="dash-proc-name">${escHtml(host.hostname)}</span>
        <span class="dash-proc-value muted-note" title="No agent has counted yet">—</span>
      </div>`;
    }
    const colour = u.critical ? 'var(--rose)'
                 : u.pending ? 'var(--lemon)' : 'var(--mint)';
    const detail = u.critical ? `${u.critical} critical` : (u.pending ? 'pending' : 'up to date');
    return `<div class="dash-proc">
      <span class="dash-proc-name">
        <span class="dash-host-dot dash-dot-${escAttr(host.status)}"></span>
        ${escHtml(host.hostname)}
        ${host.reboot_required ? '<span class="chip">reboot</span>' : ''}
      </span>
      <span class="dash-proc-value" style="color:${colour};"
            title="${escAttr(detail)}">${u.pending}</span>
    </div>`;
  }).join('') + `</div>`;
}

/* ── Utility ───────────────────────────────────────────────────────────── */

function _renderNotes(body, settings) {
  const heading = (settings.heading || '').trim();
  const text = (settings.body || '').trim();
  if (!heading && !text) { _wEmpty(body, 'Write something in this widget\u2019s settings'); return; }
  body.innerHTML =
    `${heading ? `<div class="dash-note-head">${escHtml(heading)}</div>` : ''}
     <div class="dash-note-body">${escHtml(text)}</div>`;
}

async function _renderQuickDeploy(body, settings) {
  if (!settings.definition) { _wEmpty(body, 'Pick a task in this widget\u2019s settings'); return; }
  const rows = _wRows(await _wCached('/api/v1/tasks/definitions/?scope=mine', 30000));
  const def = rows.find(d => String(d.id) === String(settings.definition));
  if (!def) { _wEmpty(body, 'That task no longer exists'); return; }
  body.innerHTML = `
    <div class="dash-stat" style="gap:10px;">
      <div class="dash-stat-label">${escHtml(def.name)}</div>
      <button class="btn btn-peach btn-sm" data-quick-deploy>Deploy…</button>
    </div>`;
  // Hands off to the normal deploy modal rather than dispatching from here:
  // choosing hosts and passing 2FA is the ceremony, not a detail to skip.
  body.querySelector('[data-quick-deploy]').onclick = () => {
    if (typeof openDeployModal === 'function') openDeployModal(def.id);
    else showToast('Open the Tasks page to deploy', 'info');
  };
}

function _renderClock(body, settings) {
  const zone = (settings.timezone || 'UTC').trim();
  let time;
  try {
    time = new Date().toLocaleTimeString([], { timeZone: zone, hour: '2-digit', minute: '2-digit' });
  } catch (e) { _wEmpty(body, `${zone} is not a timezone`); return; }
  body.innerHTML = `
    <div class="dash-stat">
      <div class="dash-stat-value">${escHtml(time)}</div>
      <div class="dash-stat-label">${escHtml((settings.label || '').trim() || zone)}</div>
    </div>`;
}

async function _renderUptimeBars(body, settings) {
  if (_wNeedsHost(body, settings)) return;
  const days = Number(settings.days) || 30;
  const rows = _wRows(await _wCached(
    `/api/v1/status-pages/uptime/${encodeURIComponent(settings.host)}/?days=${days}`, 60000));
  if (!rows.length) { _wEmpty(body, 'No uptime history for this host'); return; }
  const mean = rows.reduce((n, r) => n + r.uptime, 0) / rows.length;
  body.innerHTML = `
    <div class="dash-uptime">
      ${rows.map(r => {
        const colour = r.uptime >= 99 ? 'var(--mint)'
                     : r.uptime >= 90 ? 'var(--lemon)' : 'var(--rose)';
        return `<span class="dash-uptime-bar" style="background:${colour};"
                      title="${escAttr(r.day)} · ${r.uptime}%"></span>`;
      }).join('')}
    </div>
    <div class="dash-stat-label">${mean.toFixed(1)}% over ${rows.length} days</div>`;
}

/* ── Business ──────────────────────────────────────────────────────────── */

function _wLicensed(body, payload) {
  // A gated read answers 402 with an upgrade body rather than data. Reads stay
  // open elsewhere, so this is the widget greying itself rather than erroring.
  if (payload && payload.upgrade_url) {
    body.innerHTML = `<div class="dash-empty muted-note">
      ${escHtml(payload.detail || 'Requires a Business licence')}</div>`;
    return false;
  }
  return true;
}

async function _renderSiteSummary(body) {
  let payload;
  try { payload = await _wCached('/api/v1/sites/'); }
  catch (e) { _wEmpty(body, 'Sites requires a Business licence'); return; }
  if (!_wLicensed(body, payload)) return;
  const rows = _wRows(payload);
  if (!rows.length) { _wEmpty(body, 'No sites yet'); return; }
  body.innerHTML = `<div class="dash-procs">` + rows.map(s => `
    <div class="dash-proc">
      <span class="dash-proc-name">${escHtml(s.name)}</span>
      <span class="dash-proc-value">${s.host_count ?? 0}</span>
    </div>`).join('') + `</div>`;
}

async function _renderAuditTail(body, settings) {
  let payload;
  try { payload = await _wCached('/api/v1/audits/'); }
  catch (e) { _wEmpty(body, 'The audit log requires a Business licence'); return; }
  if (!_wLicensed(body, payload)) return;
  const rows = _wRows(payload).slice(0, Number(settings.limit) || 10);
  if (!rows.length) { _wEmpty(body, 'Nothing audited yet'); return; }
  body.innerHTML = `<div class="dash-alerts">` + rows.map(e => `
    <div class="dash-alert">
      <span class="dash-alert-msg">${escHtml(e.action || e.event || '')}</span>
      <span class="dash-alert-host">${escHtml(e.actor || e.username || '')}</span>
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
  inactive_hosts: _renderInactiveHosts,
  pending_enrollments: _renderPendingEnrollments,
  reboot_required: _renderRebootRequired,
  outdated_agents: _renderOutdatedAgents,
  disk_pressure: _renderDiskPressure,
  network_throughput: _renderNetworkThroughput,
  task_history: _renderTaskHistory,
  wave_status: _renderWaveStatus,
  playbook_coverage: _renderPlaybookCoverage,
  automation_activity: _renderAutomationActivity,
  vuln_findings: _renderVulnFindings,
  firewall_status: _renderFirewallStatus,
  windows_patches: _renderWindowsPatches,
  notes: _renderNotes,
  quick_deploy: _renderQuickDeploy,
  clock: _renderClock,
  uptime_bars: _renderUptimeBars,
  site_summary: _renderSiteSummary,
  audit_tail: _renderAuditTail,
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
