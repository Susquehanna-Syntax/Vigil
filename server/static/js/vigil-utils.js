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
