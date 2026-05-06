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
    vulnsLoaded = true;
  } catch {
    if (loadingEl) loadingEl.style.display = 'none';
    if (notConfEl) notConfEl.style.display = 'block';
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
  }
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

  if (!summaries.length) {
    content.innerHTML = `<div style="background:var(--s1);border-radius:var(--r-md);padding:40px;text-align:center;color:var(--text-3);font-size:13px;">
      No scan results yet. Configure Nessus credentials and the data will appear after the next hourly sync.
    </div>`;
    return;
  }

  const latestSync = summaries[0]?.synced_at
    ? new Date(summaries[0].synced_at).toLocaleString()
    : 'Unknown';

  content.innerHTML = `
    <table class="vuln-table">
      <thead><tr>
        <th>Host</th><th>IP</th>
        <th style="color:var(--rose);">Critical</th>
        <th style="color:var(--coral);">High</th>
        <th style="color:var(--lemon);">Medium</th>
        <th style="color:var(--sky);">Low</th>
        <th style="color:var(--text-3);">Info</th>
        <th>Last Scan</th>
      </tr></thead>
      <tbody>${summaries.map(s => `<tr>
        <td style="font-weight:600;">${escHtml(s.host_hostname)}</td>
        <td class="mono" style="color:var(--text-2);">${escHtml(s.host_ip || '—')}</td>
        <td><span class="vuln-count ${s.critical > 0 ? 'c-rose'  : 'c-dim'}">${s.critical}</span></td>
        <td><span class="vuln-count ${s.high     > 0 ? 'c-coral' : 'c-dim'}">${s.high}</span></td>
        <td><span class="vuln-count ${s.medium   > 0 ? 'c-lemon' : 'c-dim'}">${s.medium}</span></td>
        <td><span class="vuln-count ${s.low      > 0 ? 'c-sky'   : 'c-dim'}">${s.low}</span></td>
        <td><span class="vuln-count c-dim">${s.info}</span></td>
        <td style="color:var(--text-3);font-size:12px;">${s.last_scan_at ? new Date(s.last_scan_at).toLocaleDateString() : '—'}</td>
      </tr>`).join('')}</tbody>
    </table>
    <div class="vuln-sync-note">Last synced: ${latestSync}</div>`;

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
