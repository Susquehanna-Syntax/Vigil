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
  syncTimer: null, syncTicks: 0, syncAt: null,
  loaded: false, poll: null, current: null, status: null,
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

/** Why one platform's tab is empty, read off the last status: the lists Vigil
 *  would download for that platform, and what each of them did. An empty tab
 *  is not "nothing matched" — it is a list that never downloaded. */
function _storeEmptyPlatform(status, platform) {
  const grid = _storeEl('store-grid');
  if (!grid) return;
  const wanted = ((status && status.wanted) || []).filter(w => w.platform === platform);
  if (!wanted.length) {
    const sentence = platform === 'windows'
      ? "No Windows machines are enrolled, so Vigil has not downloaded winget's list."
      : "No Linux machines are enrolled, so Vigil has not downloaded Flathub's or apt's lists.";
    grid.innerHTML = `<div class="store-empty">
        <div class="store-empty-title">Nothing to show yet</div>
        <div class="store-empty-text">${escHtml(sentence)}</div>
      </div>`;
    return;
  }
  const rows = wanted.map((w) => {
    const sync = (status.syncs || []).find(s => s.source === w.source && (!w.release || s.release === w.release))
      || (status.syncs || []).find(s => s.source === w.source);
    const detail = sync && sync.error
      ? `<span class="store-empty-err">${escHtml(sync.error)}</span>`
      : sync
        ? `<span class="store-empty-text">${(sync.count || 0).toLocaleString()} packages · ${escHtml(timeAgo(sync.synced_at) || 'unknown')}</span>`
        : '<span class="store-empty-text">not synced yet</span>';
    return `<div class="store-empty-src">
        <span class="store-empty-src-name">${escHtml(w.source_label)}${w.release ? ' · ' + escHtml(w.release) : ''}</span>
        ${detail}</div>`;
  }).join('');
  grid.innerHTML = `<div class="store-empty">
      <div class="store-empty-title">Nothing to show yet</div>
      <div class="store-empty-text">Vigil downloads these package lists for the machines you have.</div>
      ${rows}
    </div>`;
}

/** The Store page offers Sync now to everyone, as it offers every write: the
 *  server answers 403 to a Viewer and apiJson's message is what the toast
 *  says. vigil-config.js carries the username but no role. */
function _storeStatusButtons(el, failed) {
  if (failed.length) {
    el.appendChild(_statusButton('store-status-warn',
      `${failed.length} sync problem${failed.length === 1 ? '' : 's'}`, 'storeSyncInfo'));
  }
  el.appendChild(_statusButton('btn btn-outline btn-xs', 'Sync now', 'storeSync'));
}

function _renderStoreStatus(status) {
  const el = _storeEl('store-status');
  if (!el) return;
  storeState.status = status;
  const ok = status.syncs.filter(s => s.synced_at);
  const newest = ok.map(s => s.synced_at).sort().pop();
  el.textContent = status.packages
    ? `${status.packages.toLocaleString()} packages · ${[...new Set(ok.map(s => s.source_label))].join(', ')}`
      + (newest && typeof timeAgo === 'function' ? ` · synced ${timeAgo(newest)}` : '')
    : '';
  _storeStatusButtons(el, status.syncs.filter(s => s.error));
}

/** The status line's buttons, wired through delegateClick — CSP forbids an
 *  onclick attribute in a generated string. */
function _statusButton(className, text, datasetKey) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = className;
  btn.dataset[datasetKey] = '';
  btn.textContent = text;
  return btn;
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
    await refreshStoreStatus();
    _storeEmpty(storeState.status || { enabled: body.enabled, syncs: [], packages: 0 });
    return;
  }
  storeState.cards = storeState.cards.concat(body.results);
  if (storeState.cards.length) {
    grid.innerHTML = storeState.cards.map(_storeCard).join('');
  } else if (!storeState.q && storeState.platform) {
    // A tab with no packages at all is not "nothing matched": the list for
    // that platform was never downloaded, or the download failed.
    _storeEmptyPlatform(storeState.status, storeState.platform);
  } else {
    grid.innerHTML = `<div class="apps-empty-state">Nothing matched${storeState.q ? ` “${escHtml(storeState.q)}”` : ''}.</div>`;
  }
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
  try {
    _renderStoreStatus(await apiJson('/api/v1/catalog/status/'));
  } catch { /* the status line is decoration; the grid is the page */ }
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
      <button class="btn btn-outline btn-sm" type="button" data-store-save>Save as task</button>
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

/** The same task Install… would deploy, saved and opened in the task editor
 *  instead of being sent now: the operator adds the hosts and the window. */
async function storeSaveTask() {
  const picked = _storePicked();
  if (!picked.length) { showToast('Pick at least one package manager', 'error'); return; }
  const name = picked.length === 1 ? picked[0].name : `${picked.length} packages`;
  try {
    const res = await apiJson('/api/v1/catalog/install-task/', {
      method: 'POST', body: JSON.stringify({ packages: picked.map(p => p.id) }) });
    _closeStoreApp();
    navigateTo('tasks');
    if (typeof openDefinitionEditor === 'function') await openDefinitionEditor(res.definition_id);
    showToast(`Saved “${name}” to your tasks`, 'success');
  } catch (e) { showToast(e.message, 'error'); }
}

/* ── Sync problems, seen from the Store page ─────────────────────────── */
const STORE_SYNC_POLL_MS = 5000;
const STORE_SYNC_TICKS = 24;

function _newestSyncedAt(status) {
  return ((status && status.syncs) || []).map(s => s.synced_at).filter(Boolean).sort().pop() || null;
}

function storeSyncNow() {
  apiJson('/api/v1/catalog/sync/', { method: 'POST', body: '{}' })
    .then(() => {
      showToast('Syncing the package lists', 'success');
      // The 202 body is the status before the thread got anywhere, so the
      // moment to watch for is the newest time on the page right now.
      storeState.syncAt = _newestSyncedAt(storeState.status);
      _storeSyncPoll();
    })
    .catch((e) => showToast(e.message, 'error'));
}

/** The run happens in a background thread, so watch the status until its
 *  newest sync time moves — then show the apps that arrived. */
function _storeSyncPoll() {
  if (storeState.syncTimer !== null) clearPollingInterval(storeState.syncTimer);
  storeState.syncTicks = 0;
  storeState.syncTimer = pollingInterval(async () => {
    if (++storeState.syncTicks > STORE_SYNC_TICKS) {
      clearPollingInterval(storeState.syncTimer);
      storeState.syncTimer = null;
      return;
    }
    await refreshStoreStatus();
    if (_newestSyncedAt(storeState.status) !== storeState.syncAt) {
      clearPollingInterval(storeState.syncTimer);
      storeState.syncTimer = null;
      loadStore(true);
    }
  }, STORE_SYNC_POLL_MS);
}

function openStoreSyncInfo() {
  const status = storeState.status;
  if (!status) return;
  const m = mountModal('store-sync-info', {});
  const failed = status.syncs.filter(s => s.error);
  const rows = (list, note) => `<div class="store-sync-rows">${list.map(s => `
      <div class="store-sync-row">
        <div class="store-sync-name">${escHtml(s.source_label)}${s.release ? ' · ' + escHtml(s.release) : ''}</div>
        <div class="store-sync-meta">${note(s)}</div>
      </div>`).join('')}</div>`;
  m.setBody(`<div class="modal-title">
      <span>Package lists</span>
      <button class="btn btn-ghost btn-sm" type="button" id="store-sync-info-x" aria-label="Close">Close</button>
    </div>
    <div class="store-sync-lead">${failed.length
      ? escHtml(`${failed.length} list${failed.length === 1 ? '' : 's'} did not download, so their apps are missing from the Store. An admin can press Sync now to try again.`)
      : escHtml('Every package list downloaded on its last run.')}</div>
    ${status.syncs.length ? rows(status.syncs, s => `${(s.count || 0).toLocaleString()} packages · ${escHtml(timeAgo(s.synced_at) || 'never')}`
      + (s.error ? `<div class="store-empty-err">${escHtml(s.error)}</div>` : '')) : ''}
    ${(status.wanted || []).filter(w => !status.syncs.some(s => s.source === w.source)).length
      ? `<div class="store-app-label">Not synced yet</div>${
          rows((status.wanted || []).filter(w => !status.syncs.some(s => s.source === w.source)),
            () => 'not synced yet')}` : ''}`);
  m.modal.querySelector('#store-sync-info-x').onclick = m.close;
  m.open();
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

delegateClick('[data-store-open]', (el) => openStoreApp(el.dataset.storeOpen));
delegateClick('[data-store-install]', () => storeInstall());
delegateClick('[data-store-save]', () => storeSaveTask());
delegateClick('[data-store-policy]', () => storeAddToPolicy());
delegateClick('[data-store-sync]', () => storeSyncNow());
delegateClick('[data-store-sync-info]', () => openStoreSyncInfo());
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
