// vigil-vulns.js
// Owns: Vulnerabilities page — Nessus scan results table + host card vuln badges.
// HTML: templates/pages/_vulns.html
// Depends on: vigil-utils.js (escHtml). Also writes badges into .host-card
//   elements rendered by vigil-host-cards.js.
// API: GET /api/v1/vulns/

let vulnsLoaded = false;

async function refreshVulns() {
  const btn = document.getElementById('vulns-refresh-btn');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.5'; }

  const loadingEl = document.getElementById('vulns-loading');
  const notConfEl = document.getElementById('vulns-not-configured');
  if (loadingEl) loadingEl.style.display = 'block';
  if (notConfEl) notConfEl.style.display = 'none';

  try {
    const resp = await fetch('/api/v1/vulns/', { credentials: 'same-origin' });
    if (!resp.ok) throw new Error('Request failed');
    const data = await resp.json();
    renderVulns(data);
    refreshVulnScans();  // also refresh scan history
    vulnsLoaded = true;
  } catch {
    if (loadingEl) loadingEl.style.display = 'none';
    if (notConfEl) notConfEl.style.display = 'block';
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
  }
}

const _VULN_SCAN_DOT = {
  requested: 'pending', launched: 'pending', running: 'pending',
  completed: 'online', failed: 'offline', aborted: 'offline',
};

function _buildVulnScanRow(s) {
  const row = document.createElement('div');
  row.className = 'task-item';

  const dot = document.createElement('div');
  dot.className = 'status-dot ' + (_VULN_SCAN_DOT[s.state] || '');
  dot.style.width = '8px';
  dot.style.height = '8px';
  row.appendChild(dot);

  const content = document.createElement('div');
  content.className = 'task-content';

  const title = document.createElement('div');
  title.className = 'task-action-name';
  title.textContent = 'Nessus scan · ' + (s.host_hostname || '');
  content.appendChild(title);

  const detail = document.createElement('div');
  detail.className = 'task-detail';
  const requester = s.requested_via_task
    ? 'requested by agent task'
    : (s.requested_by_username ? 'by ' + s.requested_by_username : 'by system');
  const when = s.requested_at ? new Date(s.requested_at).toLocaleString() : '';
  detail.textContent = `${requester} · ${when}` + (s.target ? ' · target ' + s.target : '');
  content.appendChild(detail);

  row.appendChild(content);

  const badge = document.createElement('span');
  badge.className = 'state-badge state-' + s.state;
  badge.textContent = s.state;
  row.appendChild(badge);
  return row;
}

async function refreshVulnScans() {
  const section = document.getElementById('vuln-scans-section');
  const list = document.getElementById('vuln-scans-list');
  if (!section || !list) return;
  try {
    const scans = await apiJson('/api/v1/vulns/scans/');
    list.replaceChildren();
    if (!scans.length) { section.style.display = 'none'; return; }
    section.style.display = '';
    for (const s of scans) list.appendChild(_buildVulnScanRow(s));
  } catch {
    section.style.display = 'none';
  }
}

async function startVulnScan(hostId, btn) {
  const totp = (window.prompt('Enter your TOTP code to launch a Nessus scan:') || '').trim();
  if (!totp) return;
  try {
    btn.disabled = true; btn.style.opacity = '0.5';
    await apiJson(`/api/v1/vulns/scans/${hostId}/`, {
      method: 'POST',
      body: JSON.stringify({ totp }),
    });
    showToast('Scan queued — Nessus will pick it up shortly', 'success');
    setTimeout(refreshVulnScans, 600);
  } catch (e) {
    showToast('Scan request failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
}

function _vulnCountCell(value, colorClass) {
  const td = document.createElement('td');
  const span = document.createElement('span');
  span.className = 'vuln-count ' + (value > 0 ? colorClass : 'c-dim');
  span.textContent = value;
  td.appendChild(span);
  return td;
}

function _cell(text, opts) {
  const td = document.createElement('td');
  if (opts && opts.bold) td.style.fontWeight = '600';
  if (opts && opts.mono) { td.className = 'mono'; td.style.color = 'var(--text-2)'; }
  if (opts && opts.muted) { td.style.color = 'var(--text-3)'; td.style.fontSize = '12px'; }
  td.textContent = text;
  return td;
}

function renderVulns(summaries) {
  const loadingEl = document.getElementById('vulns-loading');
  const content   = document.getElementById('vulns-content');
  if (loadingEl) loadingEl.style.display = 'none';

  const hostsWithCrit  = summaries.filter(s => s.critical > 0).length;
  const totalCritical  = summaries.reduce((n, s) => n + s.critical, 0);
  const totalHigh      = summaries.reduce((n, s) => n + s.high, 0);

  const e = id => document.getElementById(id);
  if (e('vuln-stat-critical-hosts'))  e('vuln-stat-critical-hosts').textContent  = hostsWithCrit;
  if (e('vuln-stat-total-critical'))  e('vuln-stat-total-critical').textContent  = totalCritical;
  if (e('vuln-stat-total-high'))      e('vuln-stat-total-high').textContent      = totalHigh;

  content.replaceChildren();

  if (!summaries.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'background:var(--s1);border-radius:var(--r-md);padding:40px;text-align:center;color:var(--text-3);font-size:13px;';
    empty.textContent = 'No scan results yet. Configure Nessus credentials and the data will appear after the next hourly sync.';
    content.appendChild(empty);
    return;
  }

  const table = document.createElement('table');
  table.className = 'vuln-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  const headers = [
    ['Host', null], ['IP', null],
    ['Critical', 'var(--rose)'], ['High', 'var(--coral)'],
    ['Medium', 'var(--lemon)'], ['Low', 'var(--sky)'],
    ['Info', 'var(--text-3)'], ['Last Scan', null], ['', null],
  ];
  for (const [label, color] of headers) {
    const th = document.createElement('th');
    th.textContent = label;
    if (color) th.style.color = color;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const s of summaries) {
    const tr = document.createElement('tr');
    tr.appendChild(_cell(s.host_hostname, { bold: true }));
    tr.appendChild(_cell(s.host_ip || '—', { mono: true }));
    tr.appendChild(_vulnCountCell(s.critical, 'c-rose'));
    tr.appendChild(_vulnCountCell(s.high,     'c-coral'));
    tr.appendChild(_vulnCountCell(s.medium,   'c-lemon'));
    tr.appendChild(_vulnCountCell(s.low,      'c-sky'));
    tr.appendChild(_vulnCountCell(s.info,     'c-dim'));
    tr.appendChild(_cell(
      s.last_scan_at ? new Date(s.last_scan_at).toLocaleDateString() : '—',
      { muted: true }
    ));
    const actTd = document.createElement('td');
    const btn = document.createElement('button');
    btn.className = 'btn btn-outline btn-sm';
    btn.textContent = 'Scan now';
    btn.title = 'Launch a Nessus scan against this host';
    btn.addEventListener('click', () => startVulnScan(s.host, btn));
    actTd.appendChild(btn);
    tr.appendChild(actTd);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  content.appendChild(table);

  const note = document.createElement('div');
  note.className = 'vuln-sync-note';
  note.textContent = 'Last synced: ' + (
    summaries[0]?.synced_at ? new Date(summaries[0].synced_at).toLocaleString() : 'Unknown'
  );
  content.appendChild(note);

  updateHostCardVulnBadges(summaries);
}

function updateHostCardVulnBadges(summaries) {
  const byHost = {};
  summaries.forEach(s => { byHost[s.host] = s; });

  document.querySelectorAll('.host-card').forEach(card => {
    const hostId = card.dataset.id;
    if (!hostId) return;
    const prev = card.querySelector('.host-vuln-badge');
    if (prev) prev.remove();

    const s = byHost[hostId];
    if (!s) return;

    let badge = null;
    if (s.critical > 0) {
      badge = document.createElement('span');
      badge.className = 'host-vuln-badge vuln-badge vuln-critical';
      badge.textContent = s.critical + 'C';
      badge.title = `${s.critical} critical, ${s.high} high vulnerabilities`;
    } else if (s.high > 0) {
      badge = document.createElement('span');
      badge.className = 'host-vuln-badge vuln-badge vuln-high';
      badge.textContent = s.high + 'H';
      badge.title = `${s.high} high vulnerabilities`;
    }
    if (badge) {
      const footer = card.querySelector('.host-footer');
      if (footer) footer.prepend(badge);
    }
  });
}

// Silently populate host card vuln badges on load (no error if Nessus not configured)
(async () => {
  try {
    const resp = await fetch('/api/v1/vulns/', { credentials: 'same-origin' });
    if (resp.ok) updateHostCardVulnBadges(await resp.json());
  } catch {}
})();
