// vigil-sites.js
// Owns: Settings → Sites pane — list/create/rename/delete sites and assign
//   hosts. Reads are open; writes are Business-gated (402 upgrade body).
// HTML: templates/pages/_settings.html (data-pane="sites")
// Depends on: vigil-utils.js (apiJson, showToast, confirmModal, escHtml, mountModal)

let _sitesLicensed = false;

function _slugify(s) {
  return (s || '').toLowerCase().trim()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
}

async function _sitesFeatureActive() {
  try {
    const lic = await apiJson('/api/v1/license/');
    const f = (lic.features || []).find((x) => x.name === 'sites');
    return !!(f && f.active);
  } catch (e) { return false; }
}

async function loadSites() {
  const list = document.getElementById('sites-list');
  if (!list) return;
  _sitesLicensed = await _sitesFeatureActive();
  const note = document.getElementById('ns-note');
  const createBtn = document.getElementById('ns-create');
  if (createBtn) createBtn.disabled = !_sitesLicensed;
  ['ns-name', 'ns-slug', 'ns-description'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = !_sitesLicensed;
  });
  if (note) note.textContent = _sitesLicensed
    ? '' : 'Sites is a Business feature — Free installs use the single default site.';

  try {
    const sites = await apiJson('/api/v1/sites/');
    const byId = {};
    list.innerHTML = '';
    sites.forEach((s) => {
      byId[s.id] = s;
      const row = document.createElement('div');
      row.className = 'site-row';
      const actions = _sitesLicensed
        ? `<button class="btn btn-outline btn-sm site-edit" data-id="${s.id}">Edit</button>` +
          `<button class="btn btn-outline btn-sm site-assign" data-id="${s.id}">Hosts</button>` +
          (s.is_global ? '' : `<button class="btn btn-outline btn-sm site-del" data-id="${s.id}">Delete</button>`)
        : '';
      row.innerHTML =
        `<div class="site-row-main">` +
          `<span class="site-row-name">${escHtml(s.name)}${s.is_global ? ' <span class="site-badge">global</span>' : ''}</span>` +
          `<span class="site-row-desc muted">${escHtml(s.description || '')}</span>` +
        `</div>` +
        `<div class="site-row-meta">` +
          `<span class="muted">${s.host_count} host${s.host_count === 1 ? '' : 's'}</span>` +
          `<div class="site-row-actions">${actions}</div>` +
        `</div>`;
      list.appendChild(row);
    });
    if (!sites.length) list.innerHTML = '<p class="muted">No sites yet.</p>';
    list.querySelectorAll('.site-edit').forEach((b) => b.addEventListener('click', () => editSite(byId[b.dataset.id])));
    list.querySelectorAll('.site-assign').forEach((b) => b.addEventListener('click', () => assignHosts(byId[b.dataset.id])));
    list.querySelectorAll('.site-del').forEach((b) => b.addEventListener('click', () => deleteSite(byId[b.dataset.id])));
  } catch (e) {
    list.innerHTML = `<p class="muted">Could not load sites: ${escHtml(e.message)}</p>`;
  }
}

async function createSite() {
  const name = document.getElementById('ns-name').value.trim();
  let slug = document.getElementById('ns-slug').value.trim();
  const description = document.getElementById('ns-description').value.trim();
  const note = document.getElementById('ns-note');
  if (!slug) slug = _slugify(name);
  if (!name || !slug) { note.textContent = 'Name is required.'; return; }
  try {
    await apiJson('/api/v1/sites/', {
      method: 'POST', body: JSON.stringify({ name, slug, description }),
    });
    showToast(`Created ${name}`, 'success');
    document.getElementById('ns-name').value = '';
    document.getElementById('ns-slug').value = '';
    document.getElementById('ns-description').value = '';
    note.textContent = '';
    loadSites();
  } catch (e) {
    note.textContent = e.message;   // 402 upgrade body or validation error
  }
}

function editSite(site) {
  if (!site) return;
  const m = mountModal('site-edit');
  m.setBody(`
    <div class="modal-title"><span>Edit site</span>
      <button class="modal-close" id="se-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="setting-row"><label>Name</label><input type="text" class="form-control" id="se-name"></div>
    <div class="setting-row"><label>Description</label><input type="text" class="form-control" id="se-description"></div>
    <p class="muted" id="se-note"></p>
    <div class="confirm-actions"><button class="btn btn-outline btn-sm" id="se-cancel">Cancel</button><button class="btn btn-mint btn-sm" id="se-save">Save</button></div>`);
  m.modal.querySelector('#se-name').value = site.name;
  m.modal.querySelector('#se-description').value = site.description || '';
  const close = () => m.close();
  m.modal.querySelector('#se-x').onclick = close;
  m.modal.querySelector('#se-cancel').onclick = close;
  m.modal.querySelector('#se-save').onclick = async () => {
    const body = {
      name: m.modal.querySelector('#se-name').value.trim(),
      description: m.modal.querySelector('#se-description').value.trim(),
    };
    try {
      await apiJson(`/api/v1/sites/${site.id}/`, { method: 'PATCH', body: JSON.stringify(body) });
      showToast('Site updated', 'success');
      close();
      loadSites();
    } catch (e) {
      m.modal.querySelector('#se-note').textContent = e.message;
    }
  };
  requestAnimationFrame(m.open);
}

async function deleteSite(site) {
  if (!site || site.is_global) return;
  const ok = await confirmModal(`Delete site "${site.name}"? Its hosts return to Global.`, { danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/sites/${site.id}/`, { method: 'DELETE' });
    showToast(`Deleted ${site.name}`, 'success');
    loadSites();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function assignHosts(site) {
  if (!site) return;

  // Mount and open first, then fetch. Awaiting the network before showing
  // anything left the screen dead from click to render, which reads as lag
  // however fast the request actually is.
  const m = mountModal('site-assign', { wide: true });
  m.setBody(`
    <div class="modal-title"><span>Hosts in ${escHtml(site.name)}</span>
      <button class="modal-close" id="sa-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w60"></div>
      <div class="sk-line"></div><div class="sk-line"></div><div class="sk-line sk-w80"></div>
    </div>`);
  m.modal.querySelector('#sa-x').onclick = () => m.close();
  requestAnimationFrame(m.open);

  let hosts = [];
  try { hosts = await apiJson('/api/v1/hosts/'); }
  catch (e) { m.close(); showToast('Could not load hosts', 'error'); return; }

  // Membership as it stood when the modal opened; Save diffs against this.
  const before = new Set(hosts.filter((h) => h.site === site.id).map((h) => h.id));
  const selected = new Set(before);
  const allTags = [...new Set(hosts.flatMap((h) => h.tags || []))].sort();
  let qText = '';
  let qStatus = '';
  const qTags = new Set();

  const chip = (val, label, kind) =>
    `<button type="button" class="sa-chip" data-kind="${kind}" data-val="${escHtml(val)}">${escHtml(label)}</button>`;

  m.setBody(`
    <div class="modal-title"><span>Hosts in ${escHtml(site.name)}</span>
      <button class="modal-close" id="sa-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <p class="muted" style="margin-bottom:10px;">A host belongs to exactly one site. Unchecking returns it to Global.</p>
    <input type="text" class="deploy-host-search" id="sa-search" placeholder="Search hosts by hostname\u2026">
    <div class="sa-chips" id="sa-chips">
      ${['online', 'offline', 'pending'].map((s) => chip(s, s, 'status')).join('')}
      ${allTags.map((t) => chip(t, '#' + t, 'tag')).join('')}
    </div>
    <div class="assign-list" id="sa-rows"></div>
    <p class="muted" id="sa-note"></p>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="sa-cancel">Cancel</button>
      <button class="btn btn-mint btn-sm" id="sa-save">Save changes</button>
    </div>`);

  const rowsEl = m.modal.querySelector('#sa-rows');

  function visible() {
    return hosts.filter((h) => {
      if (qText && !h.hostname.toLowerCase().includes(qText)) return false;
      if (qStatus && h.status !== qStatus) return false;
      if (qTags.size && !(h.tags || []).some((t) => qTags.has(t))) return false;
      return true;
    });
  }

  function note() {
    const add = [...selected].filter((id) => !before.has(id)).length;
    const rm = [...before].filter((id) => !selected.has(id)).length;
    m.modal.querySelector('#sa-note').textContent =
      add || rm ? `${add} to add, ${rm} to remove.` : 'No changes.';
  }

  function render() {
    const rows = visible();
    rowsEl.innerHTML = rows.length
      ? rows.map((h) => `
          <label class="assign-row">
            <input type="checkbox" value="${h.id}"${selected.has(h.id) ? ' checked' : ''}>
            <span class="sa-name">${escHtml(h.hostname)}</span>
            <span class="sa-status sa-${escHtml(h.status)}">${escHtml(h.status)}</span>
            <span class="sa-site">${escHtml(h.site_name || 'Global')}</span>
          </label>`).join('')
      : '<div class="host-pick-empty">No hosts match those filters.</div>';
    rowsEl.querySelectorAll('input[type=checkbox]').forEach((cb) => {
      cb.addEventListener('change', () => {
        if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
        note();
      });
    });
    note();
  }

  m.modal.querySelector('#sa-search').addEventListener('input', (e) => {
    qText = e.target.value.trim().toLowerCase();
    render();
  });
  m.modal.querySelector('#sa-chips').addEventListener('click', (e) => {
    const b = e.target.closest('.sa-chip');
    if (!b) return;
    if (b.dataset.kind === 'status') {
      const same = qStatus === b.dataset.val;
      qStatus = same ? '' : b.dataset.val;
      m.modal.querySelectorAll('.sa-chip[data-kind=status]')
        .forEach((c) => c.classList.toggle('on', !same && c === b));
    } else {
      const v = b.dataset.val;
      if (qTags.has(v)) qTags.delete(v); else qTags.add(v);
      b.classList.toggle('on', qTags.has(v));
    }
    render();
  });

  const close = () => m.close();
  m.modal.querySelector('#sa-x').onclick = close;
  m.modal.querySelector('#sa-cancel').onclick = close;
  m.modal.querySelector('#sa-save').onclick = async () => {
    const add = [...selected].filter((id) => !before.has(id));
    const rm = [...before].filter((id) => !selected.has(id));
    if (!add.length && !rm.length) { close(); return; }
    let ok = 0;
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
      m.modal.querySelector('#sa-note').textContent =
        `${ok} applied, ${failed.length} failed: ${failed[0]}`;
      loadSites();
      return;
    }
    showToast(`Updated ${site.name}`, 'success');
    close();
    loadSites();
  };

  render();
}

document.addEventListener('DOMContentLoaded', () => {
  const nm = document.getElementById('ns-name');
  const sl = document.getElementById('ns-slug');
  // Auto-fill slug from name until the user edits the slug directly.
  if (nm && sl) {
    let slugEdited = false;
    sl.addEventListener('input', () => { slugEdited = true; });
    nm.addEventListener('input', () => { if (!slugEdited) sl.value = _slugify(nm.value); });
  }
  if (document.getElementById('sites-list')) loadSites();
});
