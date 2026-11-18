// @ts-check
// vigil-usersites.js
// Owns: Settings → Users & Roles → "Sites" — per-site role grants and, for
// operators, the per-app capability matrix.
//
// Backend: GET/POST /api/v1/sites/roles/, DELETE /api/v1/sites/roles/<id>/.
// Granting is gated on rbac_advanced and answers 402; reading and revoking
// never are.

// Mirrors CAPABILITIES in apps/accounts/permissions.py. Kept in sync by
// tests_rbac.CapabilityVocabularyTests on the server side; a drift here shows
// up as a 400 from the grant endpoint rather than a silent wrong grant.
// Fetched from the server, never hardcoded. This used to be a duplicate of
// CAPABILITIES in apps/accounts/permissions.py, which went silently stale the
// moment a new app was added there — its verbs simply could not be granted
// through this UI. apps/accounts/test_capabilities.py fails if the literal
// comes back.
let CAPABILITY_MATRIX = {};

const USER_SITE_ROLES = ['viewer', 'operator', 'admin'];

async function openUserSites(user) {
  if (!user) return;

  // Open first, fetch second. Two requests behind an unopened modal meant
  // nothing appeared on click until both had landed.
  const m = mountModal('user-sites', { wide: true });
  m.setBody(`
    <div class="modal-title"><span>${escHtml(t('Site access — {username}', { username: user.username }))}</span>
      <button class="modal-close" id="us-boot-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w80"></div>
      <div class="sk-line"></div><div class="sk-line sk-w60"></div>
    </div>`);
  /** @type {HTMLElement} */ (m.modal.querySelector('#us-boot-x')).onclick = () => m.close();
  requestAnimationFrame(m.open);

  let sites = [];
  let grants = [];
  // Company-wide module grants (S1 04f): only instance admins may read them,
  // so for anyone else this stays null and the section is not drawn.
  let companyWide = null;
  try {
    [sites, grants, CAPABILITY_MATRIX, companyWide] = await Promise.all([
      apiJson('/api/v1/sites/'),
      apiJson(`/api/v1/sites/roles/?user=${encodeURIComponent(user.id)}`),
      apiJson('/api/v1/accounts/capabilities/'),
      apiJson('/api/v1/sites/grants/').catch(() => null),
    ]);
  } catch (e) {
    m.close();
    showToast(t('Could not load site roles'), 'error');
    return;
  }

  function render() {
    const granted = new Set(grants.map((g) => g.site));
    const ungranted = sites.filter((s) => !granted.has(s.id));

    // The sharp edge from docs/rbac.md §1.1: a user with no grants inherits
    // their profile role everywhere, so the FIRST grant narrows them. Say so
    // here, at the moment it happens, rather than in documentation.
    const warning = grants.length === 0
      ? `<div class="us-warning">
           ${t('{username} currently has {role} access to every site through their profile. Granting a site role will limit them to only the sites listed here.', {
             // The sentence comes from the catalog and is trusted; each value is
             // escaped before it goes in, so the bold survives and nothing else does.
             username: `<strong>${escHtml(user.username)}</strong>`,
             role: `<strong>${escHtml(ROLE_LABELS[user.role] || user.role)}</strong>`,
           })}
         </div>`
      : '';

    const rows = grants.length
      ? grants.map((g) => `
          <div class="us-grant" data-gid="${g.id}">
            <div class="us-grant-head">
              <span class="us-site">${escHtml(g.site_name)}</span>
              <select class="form-control us-role" data-gid="${g.id}">
                ${USER_SITE_ROLES.map((r) =>
                  `<option value="${r}"${g.role === r ? ' selected' : ''}>${escHtml(ROLE_LABELS[r] || r)}</option>`).join('')}
              </select>
              <button class="btn btn-rose btn-sm us-revoke" data-gid="${g.id}">${escHtml(t('Revoke'))}</button>
            </div>
            ${g.role === 'operator' ? capabilityGrid(g) : `
              <p class="muted us-note">${escHtml(g.role === 'admin' ? t('Full control of this site.') : t('Read-only in this site.'))}</p>`}
          </div>`).join('')
      : `<p class="muted">${escHtml(t('No site roles. Access comes from their profile role.'))}</p>`;

    m.setBody(`
      <div class="modal-title"><span>${escHtml(t('Site access — {username}', { username: user.username }))}</span>
        <button class="modal-close" id="us-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
      </div>
      ${warning}
      <div class="us-grants">${rows}</div>
      ${ungranted.length ? `
        <div class="us-add">
          <select class="form-control" id="us-new-site">
            ${ungranted.map((s) => `<option value="${s.id}">${escHtml(s.name)}</option>`).join('')}
          </select>
          <select class="form-control" id="us-new-role">
            ${USER_SITE_ROLES.map((r) => `<option value="${r}">${escHtml(ROLE_LABELS[r] || r)}</option>`).join('')}
          </select>
          <button class="btn btn-mint btn-sm" id="us-add">${escHtml(t('Grant'))}</button>
        </div>` : ''}
      ${companyWideSection()}
      <p class="muted" id="us-note"></p>
      <div class="confirm-actions">
        <button class="btn btn-mint btn-sm" id="us-close">${escHtml(t('Done'))}</button>
      </div>`);
    wire();
  }

  function companyWideSection() {
    if (!companyWide) return '';
    const held = new Set(companyWide.results.filter((g) => String(g.user_id) === String(user.id))
      .map((g) => `${g.app}:${g.verb}`));
    return `<div class="us-grant">
        <div class="us-grant-head"><span class="us-site">${escHtml(t('Company-wide access'))}</span></div>
        <p class="muted us-note">${escHtml(t('Lets {username} use these modules across every site, beyond their own sites. View widens what they can read there; the other verbs let them act on other sites\' items in that module only. Global items stay with administrators of the whole install.', { username: user.username }))}</p>
        <div class="us-matrix">` +
      Object.entries(companyWide.grantable).map(([app, verbs]) => `
          <div class="us-matrix-row">
            <span class="us-app">${escHtml(app)}</span>
            <span class="us-verbs">${verbs.map((v) => `
              <label class="us-verb">
                <input type="checkbox" data-cw-app="${escAttr(app)}" data-cw-verb="${escAttr(v)}"${held.has(`${app}:${v}`) ? ' checked' : ''}>
                ${escHtml(v)}
              </label>`).join('')}</span>
          </div>`).join('') +
      `</div></div>`;
  }

  async function toggleCompanyWide(box) {
    const { cwApp: app, cwVerb: verb } = box.dataset;
    try {
      if (box.checked) {
        const row = await apiJson('/api/v1/sites/grants/', { method: 'POST',
          body: JSON.stringify({ user_id: user.id, app, verb }) });
        companyWide.results.push(row);
      } else {
        const row = companyWide.results.find((g) => String(g.user_id) === String(user.id)
          && g.app === app && g.verb === verb);
        if (row) await apiJson(`/api/v1/sites/grants/${row.id}/`, { method: 'DELETE' });
        companyWide.results = companyWide.results.filter((g) => g !== row);
      }
      note(`${app}: ${verb} everywhere is ${box.checked ? 'on' : 'off'}`);
    } catch (e) {
      box.checked = !box.checked;
      note('Could not change company-wide access: ' + e.message, true);
    }
  }

  function capabilityGrid(g) {
    const held = new Set((g.capabilities || []).map((c) => c.join(':')));
    return `<div class="us-matrix">` +
      Object.entries(CAPABILITY_MATRIX).map(([app, verbs]) => `
        <div class="us-matrix-row">
          <span class="us-app">${escHtml(app)}</span>
          <span class="us-verbs">${verbs.map((v) => `
            <label class="us-verb">
              <input type="checkbox" data-gid="${g.id}" data-app="${escAttr(app)}"
                     data-verb="${escAttr(v)}"${held.has(`${app}:${v}`) ? ' checked' : ''}>
              ${escHtml(v)}
            </label>`).join('')}</span>
        </div>`).join('') +
      `</div>`;
  }

  function note(msg, bad) {
    const el = m.modal.querySelector('#us-note');
    if (el) { el.textContent = msg || ''; el.classList.toggle('us-bad', !!bad); }
  }

  async function save(grant, role, capabilities) {
    try {
      const saved = await apiJson('/api/v1/sites/roles/', {
        method: 'POST',
        body: JSON.stringify({
          user: user.id, site: grant.site, role,
          capabilities: capabilities,
        }),
      });
      Object.assign(grant, saved);
      note('Saved.');
      return true;
    } catch (e) {
      // 402 carries the upgrade body; show its detail rather than a raw code.
      note(e.message || 'Could not save', true);
      return false;
    }
  }

  function wire() {
    /** @type {HTMLElement} */ (m.modal.querySelector('#us-x')).onclick = () => m.close();
    /** @type {HTMLElement} */ (m.modal.querySelector('#us-close')).onclick = () => m.close();
    m.modal.querySelectorAll('[data-cw-app]').forEach((/** @type {HTMLInputElement} */ box) =>
      box.addEventListener('change', () => toggleCompanyWide(box)));

    m.modal.querySelectorAll('.us-role').forEach((/** @type {HTMLSelectElement} */ sel) => {
      sel.addEventListener('change', async () => {
        const g = grants.find((x) => String(x.id) === sel.dataset.gid);
        if (!g) return;
        // Changing away from operator drops the matrix, which is what the
        // server does too — admin and viewer never consult it.
        const caps = sel.value === 'operator' ? (g.capabilities || []) : [];
        if (await save(g, sel.value, caps)) render();
      });
    });

    m.modal.querySelectorAll('.us-matrix input[type=checkbox]').forEach((/** @type {HTMLInputElement} */ cb) => {
      cb.addEventListener('change', async () => {
        const g = grants.find((x) => String(x.id) === cb.dataset.gid);
        if (!g) return;
        const boxes = m.modal.querySelectorAll(
          `.us-matrix input[data-gid="${g.id}"]:checked`);
        const caps = Array.from(boxes).map((/** @type {HTMLInputElement} */ b) => [b.dataset.app, b.dataset.verb]);
        if (!await save(g, g.role, caps)) cb.checked = !cb.checked;
      });
    });

    m.modal.querySelectorAll('.us-revoke').forEach((/** @type {HTMLElement} */ btn) => {
      btn.addEventListener('click', async () => {
        try {
          await apiJson(`/api/v1/sites/roles/${btn.dataset.gid}/`, { method: 'DELETE' });
          grants = grants.filter((g) => String(g.id) !== btn.dataset.gid);
          render();
        } catch (e) { note(e.message, true); }
      });
    });

    const add = m.modal.querySelector('#us-add');
    if (add) {
      add.addEventListener('click', async () => {
        const site = /** @type {HTMLSelectElement} */ (m.modal.querySelector('#us-new-site')).value;
        const role = /** @type {HTMLSelectElement} */ (m.modal.querySelector('#us-new-role')).value;
        try {
          const saved = await apiJson('/api/v1/sites/roles/', {
            method: 'POST',
            body: JSON.stringify({ user: user.id, site, role, capabilities: [] }),
          });
          grants.push(saved);
          render();
        } catch (e) { note(e.message, true); }
      });
    }
  }

  render();
}
