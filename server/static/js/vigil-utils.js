// vigil-utils.js
// Owns: shared helpers used across all other vigil-*.js files.
// Depends on: nothing — must load FIRST.
// API: GET/POST helpers for /api/v1/* endpoints.
//
// Includes:
//   getCsrf, showToast, apiPost, apiJson  — HTTP / UI primitives
//   escHtml, formatBytes, _formatBytesPerSec  — formatters
//   groupByLabel, computeRates  — metric aggregation helpers used by
//                                  monitor + host-cards detail charts.

/* ── HTTP / CSRF ─────────────────────────────────────────────────────── */
function getCsrf() {
  const el = document.querySelector('[name=csrfmiddlewaretoken]');
  return el ? el.value : '';
}

function showToast(message, type) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast ' + (type || '');
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

async function apiPost(url) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': getCsrf(),
    },
    credentials: 'same-origin',
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || 'Request failed');
  }
  return resp.json();
}

async function apiJson(url, opts) {
  const resp = await fetch(url, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    ...opts,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(body.error || 'Request failed');
  }
  return body;
}

/* ── Formatters ──────────────────────────────────────────────────────── */
function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function _formatBytesPerSec(value) {
  if (!isFinite(value) || value <= 0) return '0 B/s';
  const units = ['B/s','KB/s','MB/s','GB/s'];
  let v = value, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) + ' ' + units[i];
}

/* ── Metric helpers (used by monitor + host-cards detail charts) ─────── */
function groupByLabel(points, labelKey) {
  const groups = {};
  for (const p of points) {
    const key = (p.labels || {})[labelKey] || '_default';
    if (!groups[key]) groups[key] = [];
    groups[key].push(p);
  }
  return groups;
}

function computeRates(points) {
  const rates = [];
  for (let i = 1; i < points.length; i++) {
    const dt = (new Date(points[i].time) - new Date(points[i-1].time)) / 1000;
    if (dt <= 0) continue;
    const rate = Math.max(0, (points[i].value - points[i-1].value) / dt);
    rates.push({ x: new Date(points[i].time), y: rate });
  }
  return rates;
}

// Returns an inline SVG logo string for a given OS name, or a generic Linux icon.
function osLogo(name) {
  const n = (name || '').toLowerCase();
  const s = (p, f) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="${f}" style="flex-shrink:0;vertical-align:middle">${p}</svg>`;
  if (n.includes('ubuntu'))
    return s('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4" fill="#1E1E2E"/><circle cx="12" cy="4.5" r="2" fill="#1E1E2E"/><circle cx="19.1" cy="16.3" r="2" fill="#1E1E2E"/><circle cx="4.9" cy="16.3" r="2" fill="#1E1E2E"/>', '#E95420');
  if (n.includes('zorin'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M7 9h10l-10 6h10" stroke="#1E1E2E" stroke-width="2" stroke-linecap="round" fill="none"/>', '#15A6F0');
  if (n.includes('pop') || n.includes('pop!_os'))
    return s('<circle cx="12" cy="12" r="10"/><text x="12" y="16" text-anchor="middle" font-size="11" font-weight="bold" fill="#1E1E2E">P!</text>', '#48B9C7');
  if (n.includes('mint'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M12 5c-1 3-5 5-5 9a5 5 0 0010 0c0-4-4-6-5-9z" fill="#87CF3E"/><circle cx="12" cy="14" r="2" fill="#1E1E2E"/>', '#87CF3E');
  if (n.includes('fedora'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M12 6a6 6 0 010 12V6zm0 0v12" stroke="#1E1E2E" stroke-width="2" fill="none"/>', '#51A2DA');
  if (n.includes('arch'))
    return s('<path d="M12 3l8.5 15H3.5z"/><path d="M12 8l4.5 8H7.5z" fill="#1E1E2E"/>', '#1793D1');
  if (n.includes('suse') || n.includes('opensuse'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M7 12a5 5 0 0110 0 5 5 0 01-5 5 3 3 0 000-6 3 3 0 015.2-2" stroke="#1E1E2E" stroke-width="1.5" fill="none" stroke-linecap="round"/>', '#73BA25');
  if (n.includes('debian'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M14 6a6 6 0 00-2 11.5A6 6 0 0014 6z" fill="#A80030"/>', '#A80030');
  if (n.includes('red hat') || n.includes('rhel') || n.includes('redhat'))
    return s('<circle cx="12" cy="12" r="10"/><path d="M7 14s1-4 5-4 5 4 5 4H7z" fill="#EE0000"/><ellipse cx="12" cy="9" rx="4" ry="3" fill="#EE0000"/>', '#EE0000');
  if (n.includes('centos'))
    return s('<path d="M12 2l10 10-10 10L2 12z"/><path d="M12 2v20M2 12h20" stroke="#1E1E2E" stroke-width="1" fill="none"/>', '#932279');
  if (n.includes('windows'))
    return s('<rect x="3" y="3" width="8.5" height="8.5" rx="1"/><rect x="12.5" y="3" width="8.5" height="8.5" rx="1"/><rect x="3" y="12.5" width="8.5" height="8.5" rx="1"/><rect x="12.5" y="12.5" width="8.5" height="8.5" rx="1"/>', '#0078D4');
  if (n.includes('macos') || n.includes('mac os') || n.includes('darwin'))
    return s('<path d="M17 2a4 4 0 00-3 1.5A4 4 0 0017 7a4 4 0 003-1.5A4 4 0 0017 2zM7 6C4.8 6 3 7.8 3 10c0 5 4 11 7 11 1.1 0 2-.6 3-.6s1.9.6 3 .6c3 0 7-6 7-11 0-2.2-1.8-4-4-4-1.2 0-2.2.5-3 .5S8.2 6 7 6z"/>', '#999999');
  // Generic Linux penguin outline
  return s('<ellipse cx="12" cy="9" rx="4" ry="5"/><ellipse cx="12" cy="9" rx="2" ry="3" fill="#1E1E2E"/><ellipse cx="12" cy="17" rx="5" ry="4"/><ellipse cx="12" cy="17" rx="3" ry="2.5" fill="#1E1E2E"/><circle cx="10.5" cy="7.5" r="1" fill="#F0C040"/><circle cx="13.5" cy="7.5" r="1" fill="#F0C040"/>', '#F0C040');
}
