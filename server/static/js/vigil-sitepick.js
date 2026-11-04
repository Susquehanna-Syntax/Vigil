/* Site picker for create forms (S1 04e-5).
 *
 * Where a new playbook, policy or credential goes decides who may see and
 * change it. The options come from /api/v1/scope/writable-sites/, which uses
 * the same rules the server enforces on save, so the picker never offers a
 * choice the save would refuse. With no sites to choose between (Community,
 * or an install that has not made any) the field stays hidden and nothing is
 * sent. */

let _sitePickInfo = null;

function sitePickLoad() {
  if (!_sitePickInfo) {
    _sitePickInfo = apiJson('/api/v1/scope/writable-sites/')
      .catch(() => ({ enabled: false, global: true, sites: [] }));
  }
  return _sitePickInfo;
}

/* Fill *select* (inside *field*, which is shown or hidden) with the sites the
 * user may create in, *current* selected. When *editing*, the item's own site
 * stays listed even if the user could not create there (Global included), so
 * the form shows the truth; the server still decides the save. */
async function sitePickFill(select, field, current, { editing = false, currentName = '' } = {}) {
  const info = await sitePickLoad();
  const want = current ? String(current) : '';
  const opts = [];
  if (info.global || (editing && want === '')) {
    opts.push(`<option value=""${want === '' ? ' selected' : ''}>Global (every site)</option>`);
  }
  for (const s of info.sites) {
    opts.push(`<option value="${escHtml(s.id)}"${want === s.id ? ' selected' : ''}>${escHtml(s.name)}</option>`);
  }
  if (editing && want && !info.sites.some((s) => s.id === want)) {
    opts.push(`<option value="${escHtml(want)}" selected>${escHtml(currentName || 'Another site')}</option>`);
  }
  select.innerHTML = opts.join('');
  // A user who may only create in one site gets that site, not a choice.
  if (!want && !info.global && info.sites.length) select.value = info.sites[0].id;
  if (field) field.hidden = !info.enabled;
}

/* The picked site for the request body: an id, null for global, or
 * undefined when the field is hidden (send nothing). */
function sitePickValue(select, field) {
  if (!select || (field && field.hidden)) return undefined;
  return select.value || null;
}

/* For buttons that create straight away (a new status page): which site?
 * Resolves the site id, null for global, undefined when there is nothing to
 * choose (send no site_id), or false when the user cancels. Asks only when
 * there is a real choice. */
async function sitePickChoose(title) {
  const info = await sitePickLoad();
  if (!info.enabled) return undefined;
  const choices = (info.global ? [{ id: '', name: 'Global (every site)' }] : []).concat(info.sites);
  if (choices.length === 1) return choices[0].id || null;
  return new Promise((resolve) => {
    const m = mountModal('sitepick', { variant: 'm-pop' });
    m.setBody(`
      <div class="modal-title">
        <span id="sitepick-title"></span>
        <button class="modal-close" id="sitepick-x" aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="form-group">
        <label class="form-label" for="sitepick-select">Site</label>
        <select class="form-control" id="sitepick-select">${choices.map((s) =>
          `<option value="${escHtml(s.id)}">${escHtml(s.name)}</option>`).join('')}</select>
      </div>
      <div class="confirm-actions">
        <button class="btn btn-outline btn-sm" id="sitepick-cancel">Cancel</button>
        <button class="btn btn-mint btn-sm" id="sitepick-ok">Create</button>
      </div>`);
    m.modal.querySelector('#sitepick-title').textContent = title;
    const select = m.modal.querySelector('#sitepick-select');
    const done = (val) => { m.close(); setTimeout(() => resolve(val), 200); };
    m.modal.querySelector('#sitepick-ok').onclick = () => done(select.value || null);
    m.modal.querySelector('#sitepick-cancel').onclick = () => done(false);
    m.modal.querySelector('#sitepick-x').onclick = () => done(false);
    m.overlay.onclick = () => done(false);
    requestAnimationFrame(() => { m.open(); select.focus(); });
  });
}
