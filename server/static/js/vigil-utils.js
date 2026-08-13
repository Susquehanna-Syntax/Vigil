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
    throw new Error(body.detail || body.error || 'Request failed');
  }
  return body;
}

/* ── Formatters ──────────────────────────────────────────────────────── */
/* ── Version comparison ──────────────────────────────────────────────────
   Mirrors _version_key/_is_older in apps/alerts/tasks.py — the alert and the
   badge must agree about what "outdated" means, or the drawer contradicts
   the alert list. Change one, change the other. */

// [2026, 7, 1] for "2026.7.1", or null when it cannot be read. Trailing
// letters are tolerated: Vigil has shipped "2026.1.8b".
function parseVersion(version) {
  if (!version) return null;
  const parts = [];
  for (const chunk of String(version).trim().replace(/^v/i, '').split('.')) {
    const digits = /^\d+/.exec(chunk);
    if (!digits) return null;
    parts.push(parseInt(digits[0], 10));
  }
  return parts.length ? parts : null;
}

// True only when `reported` is provably behind `expected`. An agent NEWER
// than the server is not outdated — rolling the server back used to flag the
// whole fleet, telling operators to upgrade agents already ahead of it.
// Unreadable on either side means unknown, and unknown never warns.
function isOlderVersion(reported, expected) {
  const left = parseVersion(reported);
  const right = parseVersion(expected);
  if (!left || !right) return false;
  const width = Math.max(left.length, right.length);
  for (let i = 0; i < width; i++) {
    const a = left[i] || 0;
    const b = right[i] || 0;
    if (a !== b) return a < b;
  }
  return false;
}

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

// Distro marks. Real logo images rather than hand-drawn SVG paths: the paths
// were hard to author and several were wrong for a long time without anything
// failing — Mint, Debian and Red Hat each drew their glyph in the colour of
// their own disc, so they rendered as flat circles, and Fedora's path drew a
// capital D.
//
// The files are 96px PNGs in static/img/os/, built from Simple Icons
// (CC0-1.0, so no attribution burden) on a brand-coloured disc. Windows and
// Bazzite are drawn by hand in the same idiom — Simple Icons carries neither.
// Bundled, not hot-linked: Vigil runs on networks with no route to the
// internet.
const OS_LOGOS = [
  // Order matters: derivatives before their parents. Linux Mint's os_family
  // is ubuntu and Bazzite's is rhel, so a parent listed first would claim
  // both. Test the specific name first.
  ['mint', 'linuxmint'],
  ['bazzite', 'bazzite'],
  ['zorin', 'zorin'],
  ['pop', 'popos'],
  ['ubuntu', 'ubuntu'],
  ['debian', 'debian'],
  ['fedora', 'fedora'],
  ['rocky', 'rockylinux'],
  ['alma', 'almalinux'],
  ['arch', 'archlinux'],
  ['suse', 'opensuse'],
  ['centos', 'centos'],
  ['red hat', 'redhat'],
  ['redhat', 'redhat'],
  ['rhel', 'redhat'],
  ['windows', 'windows'],
  ['macos', 'apple'],
  ['mac os', 'apple'],
  ['darwin', 'apple'],
];

// The file name for an OS, or 'linux' (Tux) when nothing matches.
function osLogoSlug(name) {
  const n = (name || '').toLowerCase();
  for (const [needle, slug] of OS_LOGOS) {
    if (n.includes(needle)) return slug;
  }
  return 'linux';
}

function osLogoSrc(name) {
  return `/static/img/os/${osLogoSlug(name)}.png`;
}

// An <img> element. Preferred over osLogo(): nothing is parsed, and the src
// comes from the fixed list above, so no caller can inject through it.
function osLogoNode(name, size) {
  const img = document.createElement('img');
  img.className = 'os-logo';
  img.src = osLogoSrc(name);
  img.alt = '';               // decorative; the OS name is always beside it
  img.loading = 'lazy';
  if (size) { img.width = size; img.height = size; }
  return img;
}

// String form for innerHTML call sites. The only interpolated value is a slug
// from the fixed list above.
function osLogo(name) {
  return `<img class="os-logo" src="${osLogoSrc(name)}" alt="" loading="lazy">`;
}

/* ── Modal helper ────────────────────────────────────────────────────────
   The app's modals are a SIBLING overlay + modal, both toggled `.open`
   (a nested modal only opening the overlay renders as a blank blur). This
   helper mounts that pair once per id and returns open/close/setBody. */
function mountModal(id, opts) {
  opts = opts || {};
  let overlay = document.getElementById(id + '-overlay');
  let modal = document.getElementById(id + '-modal');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = id + '-overlay';
    overlay.className = 'modal-overlay';
    modal = document.createElement('div');
    modal.id = id + '-modal';
    modal.className = 'modal' + (opts.wide ? ' modal-wide' : '') + (opts.xwide ? ' modal-xwide' : '');
    document.body.appendChild(overlay);
    document.body.appendChild(modal);
  }
  const close = () => { overlay.classList.remove('open'); modal.classList.remove('open'); };
  overlay.onclick = close;
  const open = () => { overlay.classList.add('open'); modal.classList.add('open'); };
  return { overlay, modal, open, close, setBody: (html) => { modal.innerHTML = html; } };
}

/* ── Custom confirm modal (replaces window.confirm) ──────────────────── */
function confirmModal(message, opts) {
  opts = opts || {};
  return new Promise((resolve) => {
    const m = mountModal('confirm');
    m.setBody(`
      <div class="modal-title">
        <span id="confirm-title"></span>
        <button class="modal-close" id="confirm-x" aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="confirm-msg" id="confirm-msg"></div>
      <div class="confirm-actions">
        <button class="btn btn-outline btn-sm" id="confirm-cancel">Cancel</button>
        <button class="btn btn-sm" id="confirm-ok"></button>
      </div>`);
    m.modal.querySelector('#confirm-title').textContent = opts.title || 'Are you sure?';
    m.modal.querySelector('#confirm-msg').textContent = message;
    const okBtn = m.modal.querySelector('#confirm-ok');
    okBtn.textContent = opts.confirmText || 'Confirm';
    okBtn.className = 'btn btn-sm ' + (opts.danger ? 'btn-rose' : 'btn-mint');
    const done = (val) => { m.close(); setTimeout(() => resolve(val), 200); };
    okBtn.onclick = () => done(true);
    m.modal.querySelector('#confirm-cancel').onclick = () => done(false);
    m.modal.querySelector('#confirm-x').onclick = () => done(false);
    m.overlay.onclick = () => done(false);
    requestAnimationFrame(m.open);
  });
}

/* ── Lightweight YAML syntax coloring (display only) ─────────────────── */
function yamlToHtml(src) {
  const esc = (s) => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  return src.split('\n').map(line => {
    // comment
    const c = line.indexOf('#');
    let comment = '';
    let body = line;
    if (c >= 0 && !line.slice(0, c).includes('"')) { comment = line.slice(c); body = line.slice(0, c); }
    let html = esc(body)
      // list dash
      .replace(/^(\s*)(- )/, '$1<span class="y-dash">- </span>')
      // key:
      .replace(/^(\s*(?:<span class="y-dash">- <\/span>)?)([\w.-]+)(:)/,
               '$1<span class="y-key">$2</span><span class="y-punc">$3</span>')
      // quoted strings
      .replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;)/g, '<span class="y-str">$1</span>')
      // bare numbers after colon
      .replace(/(<span class="y-punc">:<\/span>\s*)(\d+(?:\.\d+)?)(\s*)$/, '$1<span class="y-num">$2</span>$3');
    if (comment) html += '<span class="y-comment">' + esc(comment) + '</span>';
    return html;
  }).join('\n');
}

/* ── Theme toggle (light / dark) ─────────────────────────────────────── */
function _applyThemeIcon(theme) {
  const sun = document.getElementById('theme-icon-sun');
  const moon = document.getElementById('theme-icon-moon');
  if (sun) sun.style.display = theme === 'light' ? 'block' : 'none';
  if (moon) moon.style.display = theme === 'light' ? 'none' : 'block';
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  const next = cur === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('vigil-theme', next); } catch (e) {}
  _applyThemeIcon(next);
}

/* ── Layout density (cozy / compact) ─────────────────────────────────── */
function _applyDensityButtons(mode) {
  const cozy = document.getElementById('density-cozy');
  const compact = document.getElementById('density-compact');
  if (cozy) cozy.classList.toggle('active', mode === 'cozy');
  if (compact) compact.classList.toggle('active', mode === 'compact');
}
function setDensity(mode) {
  const next = mode === 'compact' ? 'compact' : 'cozy';
  document.documentElement.setAttribute('data-density', next);
  try { localStorage.setItem('vigil-density', next); } catch (e) {}
  _applyDensityButtons(next);
}

document.addEventListener('DOMContentLoaded', () => {
  _applyThemeIcon(document.documentElement.getAttribute('data-theme') || 'dark');
  _applyDensityButtons(document.documentElement.getAttribute('data-density') || 'cozy');
});
