// vigil-store.js
// Owns: the Store page (QA-26) — the package catalog as an app store: search,
//   platform tabs, category chips, app cards, and an app's page with
//   Install… (a task, opened in the Deploy dialog) and Add to policy….
// HTML: #page-store in templates/pages/_store.html.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr, showToast,
//   mountModal, delegateClick, timeAgo), vigil-deploy.js (openDeployModal),
//   vigil-policies.js (policyFromApp), vigil-apps.js (POLICY_APP_SOURCES).
// API: GET /api/v1/catalog/?q=&platform=&category=&page=, /categories/,
//   /status/, /<id>/, /<id>/icon/; POST /install-task/, /enable/

const storeState = {
  q: '', platform: '', category: '', page: 1, seq: 0, timer: null, cards: [],
  loaded: false, poll: null, current: null,
};

const _STORE_TINTS = ['mint', 'sky', 'lavender', 'peach', 'rose', 'lemon'];

function _storeEl(id) { return document.getElementById(id); }

/** A card's icon: the cached image, or a tinted tile with the first letter. */
function _storeIcon(c, size) {
  const letter = escHtml((c.name || '?').trim().charAt(0).toUpperCase() || '?');
  let h = 0;
  for (const ch of c.package_id || c.name || '') h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const tint = _STORE_TINTS[h % _STORE_TINTS.length];
  const tile = `<span class="store-tile store-tile-${tint}" style="--sz:${size}px" aria-hidden="true">${letter}</span>`;
  if (!c.icon) return tile;
  return `<span class="store-icon-wrap" style="--sz:${size}px">${tile}
    <img class="store-icon" src="/api/v1/catalog/${encodeURIComponent(c.id)}/icon/" alt="" loading="lazy" width="${size}" height="${size}"></span>`;
}

/** Images that fail fall back to their tile (no inline onerror: CSP). */
function _wireIcons(root) {
  root.querySelectorAll('img.store-icon:not([data-wired])').forEach((img) => {
    img.dataset.wired = '1';
    img.addEventListener('error', () => img.remove());
    img.addEventListener('load', () => img.classList.add('is-loaded'));
  });
}

function _storeCard(c) {
  const where = c.platform === 'windows' ? 'Windows' : c.platform === 'macos' ? 'macOS' : 'Linux';
  return `<button class="store-card" type="button" data-store-open="${escAttr(c.id)}">
      ${_storeIcon(c, 48)}
      <span class="store-card-body">
        <span class="store-card-name">${escHtml(c.name)}</span>
        <span class="store-card-pub">${escHtml(c.publisher || c.package_id)}</span>
        <span class="store-card-sum">${escHtml(c.summary || '')}</span>
        <span class="store-card-foot">
          <span class="chip chip-muted">${escHtml(where)} · ${escHtml(c.source_label)}</span>
          ${c.installed ? `<span class="chip store-chip-have">on ${c.installed} machine${c.installed === 1 ? '' : 's'}</span>` : ''}
        </span>
      </span>
    </button>`;
}

function _storeEmpty(status) {
  const grid = _storeEl('store-grid');
  if (status.enabled) {
    const errors = status.syncs.filter(s => s.error);
    grid.innerHTML = `<div class="store-empty">
        <div class="store-empty-title">Fetching the catalog…</div>
        <div class="store-empty-text">Vigil is downloading the package lists your machines use. This takes a minute;
          the Store fills in by itself.</div>
        ${errors.map(e => `<div class="store-empty-err">${escHtml(e.source_label)}${e.release ? ' ' + escHtml(e.release) : ''}: ${escHtml(e.error)}</div>`).join('')}
      </div>`;
    if (!storeState.poll) {
      storeState.poll = pollingInterval(async () => {
        const s = await apiJson('/api/v1/catalog/status/').catch(() => null);
        if (s && s.packages) {
          clearPollingInterval(storeState.poll);
          storeState.poll = null;
          loadStoreCategories();
          loadStore(true);
        } else if (s) {
          _storeEmpty(s);
        }
      }, 5000);
    }
    return;
  }
  grid.innerHTML = `<div class="store-empty">
      <div class="store-empty-title">The Store needs the package catalog</div>
      <div class="store-empty-text">To show what your machines can install, Vigil downloads the public package
        lists they use, once a day: winget for Windows, Flathub for Linux desktops, and the repositories of the
        Ubuntu and Debian releases you run. Vigil does not reach out on its own, so an admin turns this on.
        ${status.locked ? '<br>It is set by VIGIL_CATALOG_SYNC in the environment; change it there.' : ''}</div>
      ${status.locked ? '' : '<button class="btn btn-mint btn-sm" type="button" data-store-enable>Turn on the catalog</button>'}
    </div>`;
}

function _renderStoreStatus(status) {
  const el = _storeEl('store-status');
  if (!el) return;
  const ok = status.syncs.filter(s => s.synced_at);
  const newest = ok.map(s => s.synced_at).sort().pop();
  el.textContent = status.packages
    ? `${status.packages.toLocaleString()} packages · ${[...new Set(ok.map(s => s.source_label))].join(', ')}`
      + (newest && typeof timeAgo === 'function' ? ` · synced ${timeAgo(newest)}` : '')
    : '';
}

async function loadStore(reset) {
  const grid = _storeEl('store-grid');
  if (!grid) return;
  if (reset) { storeState.page = 1; storeState.cards = []; }
  const seq = ++storeState.seq;
  const params = new URLSearchParams({ page: storeState.page });
  if (storeState.q) params.set('q', storeState.q);
  if (storeState.platform) params.set('platform', storeState.platform);
  if (storeState.category) params.set('category', storeState.category);
  if (reset) grid.innerHTML = '<div class="store-loading">Loading…</div>';
  let body;
  try {
    body = await apiJson(`/api/v1/catalog/?${params}`);
  } catch (e) {
    if (seq === storeState.seq) grid.innerHTML = `<div class="apps-empty-state">${escHtml(e.message)}</div>`;
    return;
  }
  if (seq !== storeState.seq) return;
  if (body.empty) {
    const status = await apiJson('/api/v1/catalog/status/').catch(() => ({ enabled: body.enabled, syncs: [], packages: 0 }));
    _storeEmpty(status);
    return;
  }
  storeState.cards = storeState.cards.concat(body.results);
  grid.innerHTML = storeState.cards.length
    ? storeState.cards.map(_storeCard).join('')
    : `<div class="apps-empty-state">Nothing matched${storeState.q ? ` “${escHtml(storeState.q)}”` : ''}.</div>`;
  _wireIcons(grid);
  _storeEl('store-more').hidden = storeState.cards.length >= body.count;
}

async function loadStoreCategories() {
  const wrap = _storeEl('store-cats');
  if (!wrap) return;
  let cats = [];
  try {
    const qs = storeState.platform ? `?platform=${encodeURIComponent(storeState.platform)}` : '';
    cats = (await apiJson(`/api/v1/catalog/categories/${qs}`)).results || [];
  } catch { cats = []; }
  wrap.innerHTML = cats.length
    ? [`<button class="chip store-cat${storeState.category ? '' : ' is-on'}" type="button" data-store-cat="">All</button>`,
       ...cats.map(c => `<button class="chip store-cat${storeState.category === c.name ? ' is-on' : ''}" type="button"
           data-store-cat="${escAttr(c.name)}">${escHtml(c.name)}</button>`)].join('')
    : '';
}

async function refreshStoreStatus() {
  const status = await apiJson('/api/v1/catalog/status/').catch(() => null);
  if (status) _renderStoreStatus(status);
}

/* ── An app's page ───────────────────────────────────────────────────── */

function _storeChoices(d) {
  // This package first, then the same app from other package managers.
  return [{ id: d.id, source: d.source, source_label: d.source_label, package_id: d.package_id,
            platform: d.platform, version: d.version, installed: d.installed }, ...d.also];
}

async function openStoreApp(id) {
  let d;
  try { d = await apiJson(`/api/v1/catalog/${encodeURIComponent(id)}/`); } catch (e) { showToast(e.message, 'error'); return; }
  storeState.current = d;
  const m = mountModal('store', { wide: true, variant: 'm-pop' });
  const choices = _storeChoices(d);
  const where = { windows: 'Windows', linux: 'Linux', macos: 'macOS' };
  const releases = d.releases.length > 1
    ? `<div class="store-app-rel">${d.releases.map(r => `<span class="chip chip-muted">${escHtml(r.release.replace(':', ' '))} ${escHtml(r.version)}</span>`).join('')}</div>`
    : '';
  m.setBody(`
    <div class="modal-title">
      <span>${escHtml(d.name)}</span>
      <button class="modal-close" id="store-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="store-app-head">
      ${_storeIcon(d, 72)}
      <div>
        <div class="store-app-pub">${escHtml(d.publisher || '')}</div>
        <div class="store-app-sum">${escHtml(d.summary || '')}</div>
        <div class="store-app-meta">
          ${d.license ? `<span>${escHtml(d.license)}</span>` : ''}
          ${d.homepage && /^https?:\/\//.test(d.homepage) ? `<a href="${escAttr(d.homepage)}" target="_blank" rel="noopener noreferrer">${escHtml(d.homepage.replace(/^https?:\/\//, '').replace(/\/$/, ''))}</a>` : ''}
        </div>
      </div>
    </div>
    ${d.description ? `<div class="store-app-desc">${escHtml(d.description)}</div>` : ''}
    ${releases}
    <div class="store-app-label">Install from</div>
    <div class="store-app-sources">
      ${choices.map((c, i) => `<label class="store-src">
          <input type="checkbox" data-store-pick="${escAttr(c.id)}"${i === 0 || c.installed ? ' checked' : ''}>
          <span class="store-src-name">${escHtml(c.source_label)} <code>${escHtml(c.package_id)}</code></span>
          <span class="store-src-meta">${escHtml(where[c.platform] || c.platform)}${c.version ? ' · ' + escHtml(c.version) : ''}${c.installed ? ` · on ${c.installed} machine${c.installed === 1 ? '' : 's'}` : ''}</span>
        </label>`).join('')}
    </div>
    <div class="store-app-hint">Each package manager is a step that runs only on machines that have it, so one
      task or policy covers Windows and Linux together.</div>
    <div class="modal-actions">
      <button class="btn btn-outline btn-sm" type="button" data-store-policy>Add to policy…</button>
      <button class="btn btn-mint btn-sm" type="button" data-store-install>Install…</button>
    </div>`);
  m.modal.querySelector('#store-x').onclick = m.close;
  _wireIcons(m.modal);
  m.open();
}

function _storePicked() {
  const ids = [...document.querySelectorAll('#store-modal [data-store-pick]:checked')].map(i => Number(i.dataset.storePick));
  return _storeChoices(storeState.current).filter(c => ids.includes(c.id));
}

function _closeStoreApp() {
  document.getElementById('store-overlay')?.classList.remove('open');
  document.getElementById('store-modal')?.classList.remove('open');
}

async function storeInstall() {
  const picked = _storePicked();
  if (!picked.length) { showToast('Pick at least one package manager', 'error'); return; }
  try {
    const res = await apiJson('/api/v1/catalog/install-task/', {
      method: 'POST', body: JSON.stringify({ packages: picked.map(p => p.id) }) });
    _closeStoreApp();
    openDeployModal(res.definition_id);
  } catch (e) { showToast(e.message, 'error'); }
}

function storeAddToPolicy() {
  const picked = _storePicked().filter(p => POLICY_APP_SOURCES.includes(p.source));
  if (!picked.length) { showToast('Pick at least one package manager', 'error'); return; }
  _closeStoreApp();
  policyFromApp(storeState.current.name, picked.map(p => ({ app: p.package_id, source: p.source })));
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

delegateClick('[data-store-open]', (el) => openStoreApp(el.dataset.storeOpen));
delegateClick('[data-store-install]', () => storeInstall());
delegateClick('[data-store-policy]', () => storeAddToPolicy());
delegateClick('[data-store-more]', () => { storeState.page += 1; loadStore(false); });
delegateClick('[data-store-cat]', (el) => {
  storeState.category = el.dataset.storeCat;
  loadStoreCategories();
  loadStore(true);
});
delegateClick('[data-store-platform]', (el) => {
  storeState.platform = el.dataset.storePlatform;
  storeState.category = '';
  document.querySelectorAll('[data-store-platform]').forEach((t) => {
    const on = t === el;
    t.classList.toggle('is-on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  loadStoreCategories();
  loadStore(true);
});
delegateClick('[data-store-enable]', async (el) => {
  el.disabled = true;
  try {
    const status = await apiJson('/api/v1/catalog/enable/', { method: 'POST', body: '{}' });
    showToast('Catalog on: fetching the package lists now', 'success');
    _storeEmpty(status);
  } catch (e) {
    el.disabled = false;
    showToast(e.message, 'error');
  }
});

document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'store-q') return;
  clearTimeout(storeState.timer);
  storeState.timer = setTimeout(() => {
    storeState.q = ev.target.value.trim();
    loadStore(true);
  }, 250);
});

/* The page loads its catalog the first time it is shown. */
(function () {
  const previous = window.navigateTo;
  if (typeof previous !== 'function') return;
  window.navigateTo = function (pageName) {
    previous.apply(this, arguments);
    if (pageName !== 'store') return;
    refreshStoreStatus();
    if (!storeState.loaded) {
      storeState.loaded = true;
      loadStoreCategories();
      loadStore(true);
    }
    setTimeout(() => _storeEl('store-q')?.focus(), 0);
  };
})();
