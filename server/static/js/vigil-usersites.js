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
    <div class="modal-title"><span>Site access — ${escHtml(user.username)}</span>
      <button class="modal-close" id="us-boot-x"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
    </div>
    <div class="modal-skeleton">
      <div class="sk-line sk-w80"></div>
      <div class="sk-line"></div><div class="sk-line sk-w60"></div>
    </div>`);
  m.modal.querySelector('#us-boot-x').onclick = () => m.close();
  requestAnimationFrame(m.open);

  let sites = [];
  let grants = [];
  try {
    [sites, grants, CAPABILITY_MATRIX] = await Promise.all([
      apiJson('/api/v1/sites/'),
      apiJson(`/api/v1/sites/roles/?user=${encodeURIComponent(user.id)}`),
      apiJson('/api/v1/accounts/capabilities/'),
    ]);
  } catch (e) {
    m.close();
    showToast('Could not load site roles', 'error');
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
           <strong>${escHtml(user.username)}</strong> currently has
           <strong>${escHtml(ROLE_LABELS[user.role] || user.role)}</strong>
           access to every site through their profile. Granting a site role
           will limit them to only the sites listed here.
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
              <button class="btn btn-outline btn-sm us-revoke" data-gid="${g.id}">Revoke</button>
            </div>
            ${g.role === 'operator' ? capabilityGrid(g) : `
              <p class="muted us-note">${g.role === 'admin'
                ? 'Full control of this site.'
                : 'Read-only in this site.'}</p>`}
          </div>`).join('')
      : '<p class="muted">No site roles. Access comes from their profile role.</p>';

    m.setBody(`
      <div class="modal-title"><span>Site access — ${escHtml(user.username)}</span>
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
          <button class="btn btn-mint btn-sm" id="us-add">Grant</button>
        </div>` : ''}
      <p class="muted" id="us-note"></p>
      <div class="confirm-actions">
        <button class="btn btn-outline btn-sm" id="us-close">Done</button>
      </div>`);
    wire();
  }

  function capabilityGrid(g) {
    const held = new Set((g.capabilities || []).map((c) => c.join(':')));
    return `<div class="us-matrix">` +
      Object.entries(CAPABILITY_MATRIX).map(([app, verbs]) => `
        <div class="us-matrix-row">
          <span class="us-app">${escHtml(app)}</span>
          <span class="us-verbs">${verbs.map((v) => `
            <label class="us-verb">
              <input type="checkbox" data-gid="${g.id}" data-app="${escHtml(app)}"
                     data-verb="${escHtml(v)}"${held.has(`${app}:${v}`) ? ' checked' : ''}>
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
    m.modal.querySelector('#us-x').onclick = () => m.close();
    m.modal.querySelector('#us-close').onclick = () => m.close();

    m.modal.querySelectorAll('.us-role').forEach((sel) => {
      sel.addEventListener('change', async () => {
        const g = grants.find((x) => String(x.id) === sel.dataset.gid);
        if (!g) return;
        // Changing away from operator drops the matrix, which is what the
        // server does too — admin and viewer never consult it.
        const caps = sel.value === 'operator' ? (g.capabilities || []) : [];
        if (await save(g, sel.value, caps)) render();
      });
    });

    m.modal.querySelectorAll('.us-matrix input[type=checkbox]').forEach((cb) => {
      cb.addEventListener('change', async () => {
        const g = grants.find((x) => String(x.id) === cb.dataset.gid);
        if (!g) return;
        const boxes = m.modal.querySelectorAll(
          `.us-matrix input[data-gid="${g.id}"]:checked`);
        const caps = Array.from(boxes).map((b) => [b.dataset.app, b.dataset.verb]);
        if (!await save(g, g.role, caps)) cb.checked = !cb.checked;
      });
    });

    m.modal.querySelectorAll('.us-revoke').forEach((btn) => {
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
        const site = m.modal.querySelector('#us-new-site').value;
        const role = m.modal.querySelector('#us-new-role').value;
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
