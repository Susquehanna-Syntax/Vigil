// vigil-vulns.js
// Owns: Vulnerabilities page — fleet face headline, per-host summary table
//   with score column + inline-expandable findings rows, scan history, and
//   host card vuln badges on the dashboard.
// HTML: templates/pages/_vulns.html
// Depends on: vigil-utils.js (escHtml, apiJson, showToast). Also writes
//   badges into .host-card elements rendered by vigil-host-cards.js.
// API: GET /api/v1/vulns/, /vulns/score/, /vulns/findings/?host=…,
//      /vulns/scans/, POST /vulns/scans/<host_id>/

let vulnsLoaded = false;

// ── Score → SVG face ─────────────────────────────────────────────────────
// Mouth path's quadratic control-point Y picks the expression. The face
// circle, eye dots, and color all switch with the tier. Coordinate system
// is a 80×80 viewBox with the face centered at (40, 40).
//
// Lower controlY = bigger smile; higher controlY = bigger frown. Score
// is no longer floored — negative scores get progressively sadder tiers:
//   90+ smile, 70–89 small smile, 50–69 flat, 30–49 frown, 10–29 deep
//   frown, 0–9 distraught, –1..–39 single tear, –40..–99 crying + brow,
//   ≤ –100 X eyes + open mouth (meter broken).
//
// Returns an SVG markup string. The content is entirely numeric +
// CSS-variable literals (no user input ever flows in), parsed via
// DOMParser by the caller — same pattern vigil-host-cards.js uses for
// OS logos.
function _faceFor(score) {
  let mouthY, color, extras = '', eyes;
  const eyeDot = (cx, cy) => `<circle cx="${cx}" cy="${cy}" r="3" fill="${color}"/>`;
  if (score <= -100) {
    color = 'var(--coral)';
    eyes = `
      <line x1="25" y1="29" x2="33" y2="37" stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
      <line x1="33" y1="29" x2="25" y2="37" stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
      <line x1="47" y1="29" x2="55" y2="37" stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
      <line x1="55" y1="29" x2="47" y2="37" stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>`;
    extras = `<ellipse cx="40" cy="55" rx="9" ry="6" fill="none" stroke="${color}" stroke-width="2.5"/>`;
    return _faceSvg(color, eyes, '', extras);
  }
  if (score <= -40) {
    color = 'var(--coral)';
    mouthY = 26;
    extras = `
      <path d="M30,40 L34,46 L27,46 Z" fill="${color}" opacity="0.7"/>
      <line x1="22" y1="22" x2="30" y2="26" stroke="${color}" stroke-width="2" stroke-linecap="round"/>
      <line x1="58" y1="22" x2="50" y2="26" stroke="${color}" stroke-width="2" stroke-linecap="round"/>`;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else if (score <= -1) {
    color = 'var(--rose)';
    mouthY = 28;
    extras = `<path d="M30,40 L34,46 L27,46 Z" fill="${color}" opacity="0.7"/>`;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else if (score <= 9) {
    color = 'var(--rose)';
    mouthY = 32;
    extras = `
      <line x1="24" y1="27" x2="32" y2="29" stroke="${color}" stroke-width="2" stroke-linecap="round"/>
      <line x1="56" y1="27" x2="48" y2="29" stroke="${color}" stroke-width="2" stroke-linecap="round"/>`;
    eyes = eyeDot(29, 34) + eyeDot(51, 34);
  } else if (score <= 29) {
    color = 'var(--rose)';
    mouthY = 38;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else if (score <= 49) {
    color = 'var(--peach)';
    mouthY = 44;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else if (score <= 69) {
    color = 'var(--lemon)';
    mouthY = 50;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else if (score <= 89) {
    color = 'var(--mint)';
    mouthY = 56;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  } else {
    color = 'var(--mint)';
    mouthY = 62;
    eyes = eyeDot(29, 33) + eyeDot(51, 33);
  }
  const mouth = `<path d="M 25 50 Q 40 ${mouthY} 55 50" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round"/>`;
  return _faceSvg(color, eyes, mouth, extras);
}

function _faceSvg(color, eyes, mouth, extras) {
  return `<svg viewBox="0 0 80 80" width="80" height="80" xmlns="http://www.w3.org/2000/svg">
    <circle cx="40" cy="40" r="32" fill="none" stroke="${color}" stroke-width="3"/>
    ${eyes}
    ${mouth}
    ${extras}
  </svg>`;
}

// Parse the SVG string and attach to a host element. Same pattern as
// vigil-host-cards.js osLogo() — no innerHTML on user-touchable nodes.
function _renderFaceInto(el, score) {
  if (!el) return;
  el.replaceChildren();
  const svgDoc = new DOMParser().parseFromString(_faceFor(score), 'image/svg+xml');
  el.appendChild(document.adoptNode(svgDoc.documentElement));
}

// Mirrors _scoreColor in vigil-host-cards.js — keep in sync.
function _scoreColorLocal(score) {
  if (score <= -40) return 'var(--coral)';
  if (score <= 29) return 'var(--rose)';
  if (score <= 49) return 'var(--peach)';
  if (score <= 69) return 'var(--lemon)';
  return 'var(--mint)';
}

async function _renderFleetHeadline() {
  try {
    const d = await apiJson('/api/v1/vulns/score/');
    _renderFaceInto(document.getElementById('vuln-fleet-face'), d.score);
    const scoreEl = document.getElementById('vuln-fleet-score');
    const worstEl = document.getElementById('vuln-fleet-worst');
    if (scoreEl) {
      scoreEl.textContent = d.score;
      scoreEl.style.color = _scoreColorLocal(d.score);
    }
    if (worstEl) {
      worstEl.replaceChildren();
      if (d.worst) {
        worstEl.append('Worst offender: ');
        const name = document.createElement('strong');
        name.style.color = 'var(--text-1)';
        name.textContent = d.worst.hostname;
        worstEl.append(name, ` · score ${d.worst.score}`);
      } else if (d.host_count) {
        worstEl.textContent = `${d.host_count} host${d.host_count === 1 ? '' : 's'} scanned · nothing flagged`;
      } else {
        worstEl.textContent = 'No scanned hosts yet.';
      }
    }
  } catch {
    // Endpoint may 401 / 500 — placeholders stay so the page still renders.
  }
}

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
    refreshVulnScans();           // also refresh scan history
    _renderFleetHeadline();       // fleet face + score + worst offender
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
  const engineLabel = { nessus: 'Nessus', greenbone: 'Greenbone', trivy: 'Trivy' }[s.scanner] || 'Network';
  title.textContent = engineLabel + ' scan · ' + (s.host_hostname || '');
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
    ['', null],                            // expander chevron column
    ['Host', null], ['IP', null],
    ['Score', null],
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
    tr.dataset.hostId = s.host;
    tr.style.cursor = 'pointer';

    // Expander chevron — click anywhere on the row toggles.
    const chevTd = document.createElement('td');
    chevTd.style.width = '20px';
    chevTd.style.color = 'var(--text-3)';
    chevTd.textContent = '›';
    chevTd.className = 'vuln-row-chev';
    tr.appendChild(chevTd);

    tr.appendChild(_cell(s.host_hostname, { bold: true }));
    tr.appendChild(_cell(s.host_ip || '—', { mono: true }));

    // Score cell — numeric, color-coded by tier. Negative scores display
    // as-is (e.g. -47) so the meter being broken is visible at a glance.
    const scoreTd = document.createElement('td');
    const scoreSpan = document.createElement('span');
    scoreSpan.className = 'mono';
    scoreSpan.style.fontWeight = '600';
    scoreSpan.style.color = _scoreColorLocal(s.score);
    scoreSpan.textContent = s.score;
    scoreTd.appendChild(scoreSpan);
    tr.appendChild(scoreTd);

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
    actTd.style.textAlign = 'right';
    const btn = document.createElement('button');
    btn.className = 'btn btn-outline btn-sm';
    btn.textContent = 'Scan now';
    btn.title = 'Launch a Nessus scan against this host';
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();         // don't expand the row
      startVulnScan(s.host, btn);
    });
    actTd.appendChild(btn);
    tr.appendChild(actTd);

    // Click row → toggle findings drawer underneath.
    tr.addEventListener('click', () => _toggleFindingsRow(tr, s));

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

// ── Inline-expandable findings rows ──────────────────────────────────────
// Each summary row has a click handler that opens (or closes) a second
// <tr> right beneath it carrying the findings list for that host. One
// expanded row at a time keeps the table scannable.

const SEVERITY_ORDER = { critical: 0, high: 1, medium: 2, low: 3, info: 4 };
const SEVERITY_LABEL = {
  critical: 'CRIT', high: 'HIGH', medium: 'MED', low: 'LOW', info: 'INFO',
};
const SEVERITY_COLOR = {
  critical: 'var(--rose)', high: 'var(--coral)', medium: 'var(--lemon)',
  low: 'var(--sky)', info: 'var(--text-3)',
};

function _toggleFindingsRow(tr, summary) {
  const next = tr.nextElementSibling;
  // If the next row is already this host's findings drawer, close it.
  if (next && next.classList.contains('vuln-findings-row') && next.dataset.hostId === summary.host) {
    next.remove();
    const chev = tr.querySelector('.vuln-row-chev');
    if (chev) chev.textContent = '›';
    return;
  }
  // Close any other open drawer first.
  document.querySelectorAll('.vuln-findings-row').forEach(r => r.remove());
  document.querySelectorAll('.vuln-row-chev').forEach(c => { c.textContent = '›'; });

  const drawer = document.createElement('tr');
  drawer.className = 'vuln-findings-row';
  drawer.dataset.hostId = summary.host;
  const td = document.createElement('td');
  td.colSpan = 11;
  td.style.background = 'var(--s2)';
  td.style.padding = '14px 18px';
  const loading = document.createElement('div');
  loading.style.color = 'var(--text-3)';
  loading.style.fontSize = '12px';
  loading.textContent = 'Loading findings…';
  td.appendChild(loading);
  drawer.appendChild(td);
  tr.parentNode.insertBefore(drawer, tr.nextSibling);

  const chev = tr.querySelector('.vuln-row-chev');
  if (chev) chev.textContent = '⌄';

  _loadFindingsInto(td, summary);
}

async function _loadFindingsInto(container, summary) {
  let findings;
  try {
    findings = await apiJson(`/api/v1/vulns/findings/?host=${summary.host}&state=open`);
  } catch (e) {
    container.replaceChildren();
    const err = document.createElement('div');
    err.style.color = 'var(--text-3)';
    err.style.fontSize = '12px';
    err.textContent = 'Failed to load findings: ' + e.message;
    container.appendChild(err);
    return;
  }

  container.replaceChildren();

  if (!findings.length) {
    const empty = document.createElement('div');
    empty.style.color = 'var(--text-3)';
    empty.style.fontSize = '12px';
    empty.textContent = 'No open findings for this host yet. (Nessus per-host detail lands on the next sync cycle.)';
    container.appendChild(empty);
    return;
  }

  findings.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);

  const list = document.createElement('div');
  list.style.display = 'flex';
  list.style.flexDirection = 'column';
  list.style.gap = '6px';

  for (const f of findings) {
    const row = document.createElement('div');
    row.style.display = 'grid';
    row.style.gridTemplateColumns = '60px 80px 1fr auto';
    row.style.gap = '12px';
    row.style.alignItems = 'center';
    row.style.padding = '6px 0';
    row.style.borderBottom = '1px solid var(--s3)';

    const sev = document.createElement('span');
    sev.className = 'mono';
    sev.style.fontSize = '10px';
    sev.style.fontWeight = '700';
    sev.style.letterSpacing = '0.5px';
    sev.style.color = SEVERITY_COLOR[f.severity] || 'var(--text-3)';
    sev.textContent = SEVERITY_LABEL[f.severity] || f.severity;
    row.appendChild(sev);

    const scanner = document.createElement('span');
    scanner.className = 'mono';
    scanner.style.fontSize = '11px';
    scanner.style.color = 'var(--text-3)';
    scanner.textContent = `${f.scanner}#${f.plugin_id_or_oid}`;
    row.appendChild(scanner);

    const title = document.createElement('div');
    title.style.fontSize = '12px';
    title.style.color = 'var(--text-1)';
    title.style.overflow = 'hidden';
    title.style.textOverflow = 'ellipsis';
    title.style.whiteSpace = 'nowrap';
    title.title = f.title || '';
    const titleText = f.title || '(no title)';
    title.textContent = f.cve_id ? `${f.cve_id} · ${titleText}` : titleText;
    row.appendChild(title);

    const fix = document.createElement('button');
    fix.className = 'btn btn-sm btn-outline';
    fix.textContent = 'Suggest Fix';
    fix.title = 'Coming in a later PR — Trivy/Greenbone findings get package-aware fixes';
    fix.disabled = true;
    fix.style.opacity = '0.45';
    row.appendChild(fix);

    list.appendChild(row);
  }

  container.appendChild(list);
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
      // Sits in the name row beside the mode badge (the card has no
      // footer element — a .host-footer selector here matches nothing).
      const row = card.querySelector('.host-card-id-row');
      if (row) row.appendChild(badge);
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
