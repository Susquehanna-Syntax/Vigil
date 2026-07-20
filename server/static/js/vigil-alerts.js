// vigil-alerts.js
// Owns: Alerts page — client-side rendered firing/acknowledged/resolved lists,
//       ack / un-ack (single + bulk multi-select), ack duration menu,
//       Docker fix suggestions. Actions update in place; no page reload.
// HTML: templates/pages/_alerts.html
// Depends on: vigil-utils.js (apiJson, showToast, escHtml),
//             vigil-tasks.js (openDefinitionEditor), vigil-nav.js (navigateTo)
// API: GET  /api/v1/alerts/?state=...
//      POST /api/v1/alerts/{id}/acknowledge/  /unacknowledge/
//      POST /api/v1/alerts/bulk/

/* ── State ───────────────────────────────────────────────────────────── */
const alertsCache = { firing: [], acknowledged: [], resolved: [] };
const alertSelection = new Set();

/* ── Time formatting ─────────────────────────────────────────────────── */
function _alertRelTime(iso) {
  if (!iso) return '';
  const diff = Date.now() - new Date(iso).getTime();
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60000);
  let span;
  if (mins < 1) span = 'moments';
  else if (mins < 60) span = `${mins}m`;
  else if (mins < 1440) span = `${Math.round(mins / 60)}h`;
  else span = `${Math.round(mins / 1440)}d`;
  return diff >= 0 ? `${span} ago` : `in ${span}`;
}

/* ── Rendering ───────────────────────────────────────────────────────── */
function _alertDotColor(alert, tab) {
  if (tab === 'ack') return 'var(--lemon)';
  if (tab === 'resolved') return 'var(--mint)';
  return alert.severity === 'critical' ? 'var(--rose)'
    : alert.severity === 'warning' ? 'var(--lemon)' : 'var(--lavender)';
}

function _alertItemHtml(alert, tab) {
  const dot = _alertDotColor(alert, tab);
  const checked = alertSelection.has(alert.id) ? 'checked' : '';
  const selectable = tab !== 'resolved';
  const checkbox = selectable
    ? `<input type="checkbox" class="alert-check" data-alert-id="${alert.id}" ${checked} aria-label="Select alert">`
    : '';

  let sub, time, actions = '';
  if (tab === 'firing') {
    sub = `${escHtml(alert.host_hostname || '—')} · ${escHtml(alert.severity)}` +
          (alert.metric_value != null ? ` · Value: ${alert.metric_value}` : '');
    time = `${_alertRelTime(alert.fired_at)}`;
    const fixBtn = `<button class="btn btn-sm btn-outline" style="color:var(--mint);" data-alert-action="suggest-fix" data-alert-id="${alert.id}">Suggest Fix</button>`;
    actions = `
      <div class="alert-actions">
        ${fixBtn}
        <div class="ack-menu-wrap">
          <button class="btn btn-sm btn-outline" onclick="toggleAckMenu(event, '${alert.id}')">
            Ack
            <svg viewBox="0 0 24 24" style="width:11px;height:11px;"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="ack-menu" id="ack-menu-${alert.id}">
            <div class="ack-menu-item" onclick="acknowledgeAlert('${alert.id}', 3600)">For 1 hour</div>
            <div class="ack-menu-item" onclick="acknowledgeAlert('${alert.id}', 28800)">For 8 hours</div>
            <div class="ack-menu-item" onclick="acknowledgeAlert('${alert.id}', 86400)">For 24 hours</div>
            <div class="ack-menu-item" onclick="acknowledgeAlert('${alert.id}', 604800)">For 7 days</div>
            <div class="ack-menu-divider"></div>
            <div class="ack-menu-item" onclick="acknowledgeAlert('${alert.id}')">Permanently</div>
          </div>
        </div>
      </div>`;
  } else if (tab === 'ack') {
    const refire = alert.acknowledged_until
      ? `Re-fires ${_alertRelTime(alert.acknowledged_until)}`
      : 'Permanent';
    sub = `${escHtml(alert.host_hostname || '—')} · Ack'd ${_alertRelTime(alert.acknowledged_at)} · ${refire}`;
    time = `Fired ${_alertRelTime(alert.fired_at)}`;
    actions = `
      <div class="alert-actions">
        <button class="btn btn-sm btn-outline" onclick="unacknowledgeAlert('${alert.id}')">Un-ack</button>
      </div>`;
  } else {
    sub = `${escHtml(alert.host_hostname || '—')} · Resolved ${_alertRelTime(alert.resolved_at)}`;
    time = `Fired ${_alertRelTime(alert.fired_at)}`;
  }

  const titleStyle = tab === 'resolved' ? ' style="color: var(--text-2);"' : '';
  const dotStyle = tab === 'resolved' ? `background: ${dot}; opacity: 0.5;` : `background: ${dot};`;
  return `
    <div class="alert-item" data-alert-id="${alert.id}">
      ${checkbox}
      <div class="alert-dot" style="${dotStyle}"></div>
      <div class="alert-content">
        <div class="alert-title"${titleStyle}>${escHtml(alert.message)}</div>
        <div class="alert-sub">${sub}</div>
      </div>
      <div class="alert-time">${time}</div>
      ${actions}
    </div>`;
}

const _ALERT_EMPTY = {
  firing: `
    <div class="empty-state">
      <div class="empty-state-icon" style="background: rgba(126,221,181,0.1);">
        <svg viewBox="0 0 24 24" style="stroke: var(--mint);"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <div class="empty-state-title">All clear</div>
      <div class="empty-state-desc">No alerts are currently firing. Everything looks healthy.</div>
    </div>`,
  ack: `
    <div class="empty-state">
      <div class="empty-state-icon" style="background: rgba(226,212,120,0.1);">
        <svg viewBox="0 0 24 24" style="stroke: var(--lemon);"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
      </div>
      <div class="empty-state-title">No acknowledged alerts</div>
      <div class="empty-state-desc">Alerts you acknowledge move here until they resolve or re-fire.</div>
    </div>`,
  resolved: `
    <div class="empty-state">
      <div class="empty-state-icon" style="background: rgba(126,221,181,0.1);">
        <svg viewBox="0 0 24 24" style="stroke: var(--mint);"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
      </div>
      <div class="empty-state-title">No resolved alerts yet</div>
      <div class="empty-state-desc">Resolved alerts keep their history here for reference.</div>
    </div>`,
};

function _renderAlertList(tab, alerts) {
  const el = document.getElementById(`alerts-list-${tab}`);
  if (!el) return;
  el.innerHTML = alerts.length
    ? alerts.map(a => _alertItemHtml(a, tab)).join('')
    : _ALERT_EMPTY[tab];
}

function _renderAlertCounts() {
  const firing = document.getElementById('alerts-count-firing');
  const ack = document.getElementById('alerts-count-ack');
  if (firing) firing.textContent = alertsCache.firing.length ? `(${alertsCache.firing.length})` : '';
  if (ack) ack.textContent = alertsCache.acknowledged.length ? `(${alertsCache.acknowledged.length})` : '';
}

async function refreshAlerts() {
  try {
    const [firing, acked, resolved] = await Promise.all([
      apiJson('/api/v1/alerts/?state=firing'),
      apiJson('/api/v1/alerts/?state=acknowledged'),
      apiJson('/api/v1/alerts/?state=resolved&limit=20'),
    ]);
    alertsCache.firing = firing;
    alertsCache.acknowledged = acked;
    alertsCache.resolved = resolved;

    // Drop selections for alerts that changed state out from under us
    const live = new Set([...firing, ...acked].map(a => a.id));
    for (const id of alertSelection) if (!live.has(id)) alertSelection.delete(id);

    _renderAlertList('firing', firing);
    _renderAlertList('ack', acked);
    _renderAlertList('resolved', resolved);
    _renderAlertCounts();
    _updateBulkBar();
  } catch (e) {
    showToast('Failed to load alerts: ' + e.message, 'error');
  }
}

/* ── Selection + bulk actions ────────────────────────────────────────── */
function _updateBulkBar() {
  const bar = document.getElementById('alerts-bulk-bar');
  if (!bar) return;
  const n = alertSelection.size;
  bar.hidden = n === 0;
  const count = document.getElementById('alerts-bulk-count');
  if (count) count.textContent = `${n} selected`;
}

function clearAlertSelection() {
  alertSelection.clear();
  document.querySelectorAll('.alert-check:checked').forEach(cb => { cb.checked = false; });
  _updateBulkBar();
}

async function bulkAlertAction(action, durationSeconds = null) {
  closeAckMenus();
  if (!alertSelection.size) return;
  try {
    const body = { ids: [...alertSelection], action };
    if (action === 'acknowledge' && durationSeconds) body.duration_seconds = durationSeconds;
    const res = await apiJson('/api/v1/alerts/bulk/', { method: 'POST', body: JSON.stringify(body) });
    const verb = action === 'acknowledge' ? 'acknowledged' : 're-fired';
    showToast(`${res.updated} alert${res.updated === 1 ? '' : 's'} ${verb}` +
              (res.skipped ? ` (${res.skipped} skipped)` : ''), 'success');
    alertSelection.clear();
    refreshAlerts();
  } catch (e) {
    showToast('Bulk action failed: ' + e.message, 'error');
  }
}

/* ── Single-alert actions (no reload — lists re-render in place) ─────── */
async function acknowledgeAlert(alertId, durationSeconds = null) {
  closeAckMenus();
  try {
    const body = durationSeconds ? { duration_seconds: durationSeconds } : {};
    await apiJson(`/api/v1/alerts/${alertId}/acknowledge/`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    showToast(durationSeconds ? 'Acknowledged — will re-fire if still open' : 'Alert acknowledged', 'success');
    refreshAlerts();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function unacknowledgeAlert(alertId) {
  try {
    await apiJson(`/api/v1/alerts/${alertId}/unacknowledge/`, { method: 'POST' });
    showToast('Alert re-fired', 'success');
    refreshAlerts();
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

/* ── Ack duration menu ───────────────────────────────────────────────── */
function closeAckMenus() {
  document.querySelectorAll('.ack-menu.open').forEach(m => m.classList.remove('open'));
  // Drop the z-index lift from whichever card owned the open menu
  document.querySelectorAll('.alert-item.menu-open').forEach(el => el.classList.remove('menu-open'));
}

function toggleAckMenu(event, alertId) {
  event.stopPropagation();
  const menu = document.getElementById('ack-menu-' + alertId);
  if (!menu) return;
  const wasOpen = menu.classList.contains('open');
  closeAckMenus();
  if (!wasOpen) {
    menu.classList.add('open');
    // Lift the owning card above its siblings so the menu isn't painted
    // under the next alert card (hover transforms create stacking contexts)
    const item = menu.closest('.alert-item');
    if (item) item.classList.add('menu-open');
  }
}

document.addEventListener('click', closeAckMenus);

/* ── Event delegation for rendered content ───────────────────────────── */
document.addEventListener('change', (e) => {
  const cb = e.target.closest('.alert-check');
  if (!cb) return;
  if (cb.checked) alertSelection.add(cb.dataset.alertId);
  else alertSelection.delete(cb.dataset.alertId);
  _updateBulkBar();
});

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-alert-action="suggest-fix"]');
  if (!btn) return;
  e.stopPropagation();
  const alert = alertsCache.firing.find(a => a.id === btn.dataset.alertId);
  if (!alert) return;
  // Ask the operator's own model for a real remediation. Falls back to the
  // static template only when AI isn't wired (the modal handles the 409 with
  // a link to Settings).
  if (typeof suggestFixForAlert === 'function') {
    const sub = `${alert.host_hostname || ''} · ${alert.message || ''}`.trim();
    suggestFixForAlert(alert.id, sub);
  } else if (alert.fix_context && alert.fix_context.container_name) {
    suggestDockerFix(alert.host, alert.fix_context.container_name, alert.fix_context.image);
  }
});

/* ── Fix suggestions ─────────────────────────────────────────────────── */
function suggestAgentUpdate(hostId) {
  const yaml = [
    `name: "Update Vigil Agent"`,
    `description: "Download the latest Vigil agent binary from the server and restart the service"`,
    `actions:`,
    `  - id: update`,
    `    type: update_agent`,
    `    params: {}`,
  ].join('\n');
  openDefinitionEditor(null, yaml);
}

function suggestDockerFix(hostId, containerName, image) {
  // recreate_container (not restart_container): a restart keeps the container
  // on its original image, so the pulled update would never actually apply.
  const yaml = [
    `name: "Update Docker Image: ${image}"`,
    `description: "Pull the latest ${image} and recreate ${containerName} on it"`,
    `actions:`,
    `  - id: pull_new_image`,
    `    type: pull_image`,
    `    params:`,
    `      image: "${image}"`,
    `  - id: recreate`,
    `    type: recreate_container`,
    `    params:`,
    `      container_name: "${containerName}"`,
    `      image: "${image}"`,
  ].join('\n');
  openDefinitionEditor(null, yaml);
}

/* ── Load triggers ───────────────────────────────────────────────────── */
// Refresh whenever the user lands on the Alerts page, and once at startup
// so the tab counts are populated before the first visit.
if (typeof navigateTo === 'function') {
  const _origNavigateAlerts = navigateTo;
  navigateTo = function (pageName) {
    _origNavigateAlerts(pageName);
    if (pageName === 'alerts') refreshAlerts();
  };
}
document.addEventListener('DOMContentLoaded', refreshAlerts);
