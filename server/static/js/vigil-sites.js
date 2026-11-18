// @ts-check
// vigil-sites.js
// Owns: Settings → Sites pane — list/create/rename/delete sites and assign
//   hosts. Reads are open; writes are Business-gated (402 upgrade body).
// HTML: templates/pages/_settings.html (data-pane="sites")
// Depends on: vigil-utils.js (apiJson, showToast, confirmModal, escHtml, mountModal)

/** One row of /api/v1/sites/. */
/** @typedef {{id: string, name: string, slug?: string, description?: string|null, is_global?: boolean, host_count?: number}} Site */
/** A row of /api/v1/hosts/, as far as the assign-hosts modal reads it. */
/** @typedef {{id: string, hostname: string, status: string, site?: string|null, site_name?: string|null, tags?: string[]}} SiteHostRow */

let _sitesLicensed = false;

/** @param {string} s @returns {string} */
function _slugify(s) {
  return (s || '').toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

/** @returns {Promise<boolean>} */
async function _sitesFeatureActive() {
  try {
    const lic = await apiJson('/api/v1/license/');
    const f = (lic.features || []).find((/** @type {{name: string}} */ x) => x.name === 'sites');
    return !!(f && f.active);
  } catch (e) { return false; }
}

async function loadSites() {
  const list = /** @type {HTMLElement} */ (document.getElementById('sites-list'));
  if (!list) return;
  _sitesLicensed = await _sitesFeatureActive();
  const note = document.getElementById('ns-note');
  const createBtn = /** @type {HTMLButtonElement|null} */ (document.getElementById('ns-create'));
  if (createBtn) createBtn.disabled = !_sitesLicensed;
  ['ns-name', 'ns-slug', 'ns-description'].forEach((/** @type {string} */ id) => {
    const el = /** @type {HTMLInputElement|null} */ (document.getElementById(id));
    if (el) el.disabled = !_sitesLicensed;
  });
  if (note) note.textContent = _sitesLicensed
    ? '' : t('Sites is a Business feature — Free installs use the single default site.');

  try {
    const sites = await apiJson('/api/v1/sites/');
    /** @type {Record<string, Site>} */
    const byId = {};
    list.innerHTML = '';
    sites.forEach((/** @type {Site} */ s) => {
      byId[s.id] = s;
      const row = document.createElement('div');
      row.className = 'site-row';
      const actions = _sitesLicensed
        ? `<button class="btn btn-sky btn-sm site-edit" data-id="${s.id}">${escHtml(t('Edit'))}</button>` +
          `<button class="btn btn-sky btn-sm site-assign" data-id="${s.id}">${escHtml(t('Hosts'))}</button>` +
          (s.is_global ? '' : `<button class="btn btn-rose btn-sm site-del" data-id="${s.id}">${escHtml(t('Delete'))}</button>`)
        : '';
      row.innerHTML =
        `<div class="site-row-main">` +
          `<span class="site-row-name">${escHtml(s.name)}${s.is_global ? ` <span class="site-badge">${escHtml(t('global'))}</span>` : ''}</span>` +
          `<span class="site-row-desc muted">${escHtml(s.description || '')}</span>` +
        `</div>` +
        `<div class="site-row-meta">` +
          `<span class="muted">${escHtml(tn('{n} host', '{n} hosts', s.host_count))}</span>` +
          `<div class="site-row-actions">${actions}</div>` +
        `</div>`;
      list.appendChild(row);
    });
    if (!sites.length) list.innerHTML = `<p class="muted">${escHtml(t('No sites yet.'))}</p>`;
    list.querySelectorAll('.site-edit').forEach((/** @type {HTMLElement} */ b) => b.addEventListener('click', () => editSite(byId[b.dataset.id])));
    list.querySelectorAll('.site-assign').forEach((/** @type {HTMLElement} */ b) => b.addEventListener('click', () => assignHosts(byId[b.dataset.id])));
    list.querySelectorAll('.site-del').forEach((/** @type {HTMLElement} */ b) => b.addEventListener('click', () => deleteSite(byId[b.dataset.id])));
  } catch (e) {
    list.innerHTML = `<p class="muted">${escHtml(t('Could not load sites: {message}', { message: e.message }))}</p>`;
  }
}

async function createSite() {
  const name = /** @type {HTMLInputElement} */ (document.getElementById('ns-name')).value.trim();
  let slug = /** @type {HTMLInputElement} */ (document.getElementById('ns-slug')).value.trim();
  const description = /** @type {HTMLInputElement} */ (document.getElementById('ns-description')).value.trim();
  const note = /** @type {HTMLElement} */ (document.getElementById('ns-note'));
  if (!slug) slug = _slugify(name);
  if (!name || !slug) { note.textContent = t('Name is required.'); return; }
  try {
    await apiJson('/api/v1/sites/', {
      method: 'POST', body: JSON.stringify({ name, slug, description }),
    });
    showToast(t('Created {name}', { name }), 'success');
    /** @type {HTMLInputElement} */ (document.getElementById('ns-name')).value = '';
    /** @type {HTMLInputElement} */ (document.getElementById('ns-slug')).value = '';
    /** @type {HTMLInputElement} */ (document.getElementById('ns-description')).value = '';
    note.textContent = '';
    loadSites();
  } catch (e) {
    note.textContent = e.message;   // 402 upgrade body or validation error
  }
}

/** @param {Site|undefined} site @returns {void} */
function editSite(site) {
  if (!site) return;
  const m = mountModal('site-edit');
  m.setBody(`
    <div class="modal-title"><span>${escHtml(t('Edit site'))}</span>
      <button class="modal-close" id="se-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="setting-row"><label>${escHtml(t('Name'))}</label><input type="text" class="form-control" id="se-name"></div>
    <div class="setting-row"><label>${escHtml(t('Description'))}</label><input type="text" class="form-control" id="se-description"></div>
    <p class="muted" id="se-note"></p>
    <div class="confirm-actions"><button class="btn btn-outline btn-sm" id="se-cancel">${escHtml(t('Cancel'))}</button><button class="btn btn-mint btn-sm" id="se-save">${escHtml(t('Save'))}</button></div>`);
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#se-name')).value = site.name;
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#se-description')).value = site.description || '';
  const close = () => m.close();
  /** @type {HTMLElement} */ (m.modal.querySelector('#se-x')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#se-cancel')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#se-save')).onclick = async () => {
    const body = {
      name: /** @type {HTMLInputElement} */ (m.modal.querySelector('#se-name')).value.trim(),
      description: /** @type {HTMLInputElement} */ (m.modal.querySelector('#se-description')).value.trim(),
    };
    try {
      await apiJson(`/api/v1/sites/${site.id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast(t('Site updated'), 'success');
      close();
      loadSites();
    } catch (e) {
      /** @type {HTMLElement} */ (m.modal.querySelector('#se-note')).textContent = e.message;
    }
  };
  requestAnimationFrame(m.open);
}

/** @param {Site|undefined} site @returns {Promise<void>} */
async function deleteSite(site) {
  if (!site || site.is_global) return;
  const ok = await confirmModal(t('Delete site "{name}"? Its hosts return to Global.', { name: site.name }), { danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/sites/${site.id}/`, { method: 'DELETE' });
    showToast(t('Deleted {name}', { name: site.name }), 'success');
    loadSites();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

/** @param {Site|undefined} site @returns {Promise<void>} */
async function assignHosts(site) {
  if (!site) return;

  // Mount and open first, then fetch. Awaiting the network before showing
  // anything left the screen dead from click to render, which reads as lag
  // however fast the request actually is.
  const m = mountModal('site-assign', { wide: true });
  // Same title as the loaded body below, so the skeleton does not flash a bare site name.
  const _saTitle = t('Hosts in {site}', { site: escHtml(site.name) });
  m.setBody(`
    <div class="modal-title"><span>${_saTitle}</span>
      <button class="modal-close" id="sa-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w60"></div>
      <div class="sk-line"></div><div class="sk-line"></div><div class="sk-line sk-w80"></div>
    </div>`);
  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-x')).onclick = () => m.close();
  requestAnimationFrame(m.open);

  /** @type {SiteHostRow[]} */
  let hosts = [];
  try { hosts = await apiJson('/api/v1/hosts/'); }
  catch (e) { m.close(); showToast(t('Could not load hosts'), 'error'); return; }

  // Membership as it stood when the modal opened; Save diffs against this.
  const before = new Set(hosts.filter((/** @type {SiteHostRow} */ h) => h.site === site.id).map((/** @type {SiteHostRow} */ h) => h.id));
  const selected = new Set(before);
  // A local `t` here would hide the translate helper in this scope.
  const allTagNames = [...new Set(hosts.flatMap((/** @type {SiteHostRow} */ h) => h.tags || []))].sort();
  let qText = '';
  let qStatus = '';
  /** @type {Set<string>} */
  const qTags = new Set();

  const chip = (/** @type {string} */ val, /** @type {string} */ label, /** @type {string} */ kind) =>
    `<button type="button" class="sa-chip" data-kind="${kind}" data-val="${escAttr(val)}">${escHtml(label)}</button>`;

  m.setBody(`
    <div class="modal-title"><span>${_saTitle}</span>
      <button class="modal-close" id="sa-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <p class="muted sa-intro">${escHtml(t('A host belongs to exactly one site. Unchecking returns it to Global.'))}</p>
    <input type="text" class="deploy-host-search" id="sa-search" placeholder="${escAttr(t('Search hosts by hostname…'))}">
    <div class="sa-chips" id="sa-chips">
      ${['online', 'offline', 'pending'].map((s) => chip(s, s, 'status')).join('')}
      ${allTagNames.map((tag) => chip(tag, '#' + tag, 'tag')).join('')}
    </div>
    <div class="assign-list" id="sa-rows"></div>
    <p class="muted" id="sa-note"></p>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="sa-cancel">${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" id="sa-save">${escHtml(t('Save changes'))}</button>
    </div>`);

  const rowsEl = /** @type {HTMLElement} */ (m.modal.querySelector('#sa-rows'));

  function visible() {
    return hosts.filter((/** @type {SiteHostRow} */ h) => {
      if (qText && !h.hostname.toLowerCase().includes(qText)) return false;
      if (qStatus && h.status !== qStatus) return false;
      if (qTags.size && !(h.tags || []).some((/** @type {string} */ tag) => qTags.has(tag))) return false;
      return true;
    });
  }

  function note() {
    const add = [...selected].filter((/** @type {string} */ id) => !before.has(id)).length;
    const rm = [...before].filter((/** @type {string} */ id) => !selected.has(id)).length;
    /** @type {HTMLElement} */ (m.modal.querySelector('#sa-note')).textContent =
      add || rm ? t('{add} to add, {remove} to remove.', { add, remove: rm }) : t('No changes.');
  }

  function render() {
    const rows = visible();
    rowsEl.innerHTML = rows.length
      ? rows.map((/** @type {SiteHostRow} */ h) => `
          <label class="assign-row">
            <input type="checkbox" value="${h.id}"${selected.has(h.id) ? ' checked' : ''}>
            <span class="sa-name">${escHtml(h.hostname)}</span>
            <span class="sa-status sa-${escHtml(h.status)}">${escHtml(h.status)}</span>
            <span class="sa-site">${escHtml(h.site_name || t('Global'))}</span>
          </label>`).join('')
      : `<div class="host-pick-empty">${escHtml(t('No hosts match those filters.'))}</div>`;
    rowsEl.querySelectorAll('input[type=checkbox]').forEach((/** @type {HTMLInputElement} */ cb) => {
      cb.addEventListener('change', () => {
        if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
        note();
      });
    });
    note();
  }

  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-search')).addEventListener('input', (/** @type {Event} */ e) => {
    qText = /** @type {HTMLInputElement} */ (e.target).value.trim().toLowerCase();
    render();
  });
  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-chips')).addEventListener('click', (/** @type {MouseEvent} */ e) => {
    const b = /** @type {HTMLElement|null} */ (/** @type {HTMLElement|null} */ (e.target).closest('.sa-chip'));
    if (!b) return;
    if (b.dataset.kind === 'status') {
      const same = qStatus === b.dataset.val;
      qStatus = same ? '' : b.dataset.val;
      m.modal.querySelectorAll('.sa-chip[data-kind=status]')
        .forEach((/** @type {HTMLElement} */ c) => c.classList.toggle('on', !same && c === b));
    } else {
      const v = b.dataset.val;
      if (qTags.has(v)) qTags.delete(v); else qTags.add(v);
      b.classList.toggle('on', qTags.has(v));
    }
    render();
  });

  const close = () => m.close();
  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-x')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-cancel')).onclick = close;
  /** @type {HTMLElement} */ (m.modal.querySelector('#sa-save')).onclick = async () => {
    const add = [...selected].filter((/** @type {string} */ id) => !before.has(id));
    const rm = [...before].filter((/** @type {string} */ id) => !selected.has(id));
    if (!add.length && !rm.length) { close(); return; }
    let ok = 0;
    /** @type {string[]} */
    const failed = [];
    for (const hid of add) {
      try { await apiJson(`/api/v1/sites/${site.id}/hosts/${hid}/`, { method: 'PUT' }); ok++; }
      catch (e) { failed.push(e.message); }
    }
    for (const hid of rm) {
      try { await apiJson(`/api/v1/sites/${site.id}/hosts/${hid}/`, { method: 'DELETE' }); ok++; }
      catch (e) { failed.push(e.message); }
    }
    if (failed.length) {
      /** @type {HTMLElement} */ (m.modal.querySelector('#sa-note')).textContent =
        t('{ok} applied, {failed} failed: {reason}', { ok, failed: failed.length, reason: failed[0] });
      loadSites();
      return;
    }
    showToast(t('Updated {name}', { name: site.name }), 'success');
    close();
    loadSites();
  };

  render();
}

document.addEventListener('DOMContentLoaded', () => {
  const nm = /** @type {HTMLInputElement|null} */ (document.getElementById('ns-name'));
  const sl = /** @type {HTMLInputElement|null} */ (document.getElementById('ns-slug'));
  // Auto-fill slug from name until the user edits the slug directly.
  if (nm && sl) {
    let slugEdited = false;
    sl.addEventListener('input', () => { slugEdited = true; });
    nm.addEventListener('input', () => { if (!slugEdited) sl.value = _slugify(nm.value); });
  }
  if (document.getElementById('sites-list')) loadSites();
});
