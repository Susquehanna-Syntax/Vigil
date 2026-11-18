// @ts-check
// vigil-agentprofiles.js
// Owns: Reprovision → Agent profiles — the list, and the editor for one.
// HTML: templates/pages/_reprovision.html #agent-profiles-list
// Depends on: vigil-utils.js (apiJson, showToast, escHtml, mountModal,
//   confirmModal), vigil-agentprofile-form.js (apfRender/apfRead/apfShowErrors)
// API: GET|POST /api/v1/agent-profiles/  PATCH|DELETE /<id>/
//
// A profile shapes agent.yml at install and rebuild only. Nothing here can
// raise the privileges of an agent that is already running, which is why an
// admin may delete their own profiles freely but not the builtins — those are
// the wizard's starting points, and it has nothing to copy from without them.

const APF_LABELS = { monitor: 'Monitor', managed: 'Managed', full_control: 'Full control' };

/** One agent-profile row from /api/v1/agent-profiles/. */
/** @typedef {{id?: string|number, name?: string, mode?: string, builtin?: boolean, site_id?: string|number|null, description?: string, primary_relay?: string|number|null, allow_remote_shell?: boolean, allow_remote_desktop?: boolean, install_profiles?: Array<string|{name?: string}>, [k: string]: any}} AgentProfile */

/** @returns {Promise<void>} */
async function loadAgentProfiles() {
  const list = document.getElementById('agent-profiles-list');
  if (!list) return;
  let profiles;
  try {
    [profiles] = await Promise.all([apiJson('/api/v1/agent-profiles/'), _apLoadRelayNames()]);
  } catch (e) {
    list.replaceChildren();
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = t('Agent profiles require an admin account.');
    list.appendChild(p);
    return;
  }
  list.replaceChildren();
  if (!Array.isArray(profiles) || !profiles.length) {
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = t('No agent profiles yet.');
    list.appendChild(p);
    return;
  }
  for (const profile of profiles) {
    if (profile && typeof profile === 'object') list.appendChild(_apCard(profile));
  }
}

function _apSummary(profile) {
  const parts = [APF_LABELS[profile.mode] || String(profile.mode || '')];
  if (profile.mode === 'managed') parts.push(`${(profile.allowlist || []).length} actions`);
  if (profile.primary_relay) parts.push(`via ${_apRelayName(profile.primary_relay)}`);
  parts.push((profile.allow_remote_shell || profile.allow_remote_desktop) ? 'shells on' : 'shells off');
  if ((profile.tags || []).length) parts.push(`tags ${(profile.tags || []).join(', ')}`);
  return parts.filter(Boolean).join(' · ');
}

// The list endpoint carries no relay names: one lookup per list load, read
// synchronously by every card.
let _apRelaysById = {};
async function _apLoadRelayNames() {
  try {
    const body = await apiJson('/api/v1/relays/');
    const relays = Array.isArray(body) ? body : (body.relays || []);
    _apRelaysById = Object.fromEntries(relays.map(r => [String(r.id), r.name]));
  } catch (e) {
    _apRelaysById = {};
  }
}

function _apRelayName(id) {
  return _apRelaysById[String(id)] || `relay ${String(id).slice(0, 8)}`;
}

/** @param {AgentProfile|null=} profile @returns {HTMLElement} */
function _apCard(profile) {
  const card = document.createElement('div');
  card.className = 'site-row';

  const main = document.createElement('div');
  main.className = 'site-row-main';

  const title = document.createElement('span');
  title.className = 'site-row-name';
  title.textContent = profile.name || ''; // operator-authored — textContent
  main.appendChild(title);

  if (profile.builtin) {
    const badge = document.createElement('span');
    badge.className = 'chip chip-muted';
    badge.textContent = t('built-in');
    main.appendChild(badge);
  }

  const desc = document.createElement('span');
  desc.className = 'site-row-desc muted';
  desc.textContent = _apSummary(profile); // built from API fields — textContent
  main.appendChild(desc);

  const used = document.createElement('span');
  used.className = 'site-row-desc muted';
  const by = profile.install_profiles || [];
  used.textContent = by.length ? t('Used by: {profiles}', { profiles: by.join(', ') }) : t('Used by: no install profiles');
  main.appendChild(used);

  card.appendChild(main);

  const meta = document.createElement('div');
  meta.className = 'site-row-meta';

  const edit = document.createElement('button');
  edit.className = 'btn btn-outline btn-sm';
  edit.type = 'button';
  edit.textContent = t('Edit');
  edit.addEventListener('click', () => openAgentProfileEditor(profile));
  meta.appendChild(edit);

  // No delete for builtins: the API refuses it, and the wizard needs them.
  if (!profile.builtin) {
    const del = document.createElement('button');
    del.className = 'btn btn-outline btn-sm img-del';
    del.type = 'button';
    del.textContent = t('Delete');
    del.addEventListener('click', () => deleteAgentProfile(profile));
    meta.appendChild(del);
  }

  card.appendChild(meta);
  return card;
}

/** @param {AgentProfile|null=} profile @returns {Promise<void>} */
async function openAgentProfileEditor(profile) {
  const m = mountModal('agentprofile', { wide: true });
  const editing = profile && profile.id ? String(profile.id) : '';
  m.setBody(`
    <div class="modal-title"><span>${escHtml(editing ? t('Edit agent profile') : t('New agent profile'))}</span>
      <button class="modal-close" id="ap-x" aria-label="${escAttr(t('Close'))}">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="setting-row"><label>${escHtml(t('Name'))}</label>
      <input type="text" class="form-control" id="ap-name" maxlength="80" value=""></div>
    <div class="setting-row" id="ap-site-field" hidden><label for="ap-site">${escHtml(t('Site'))}</label>
      <select class="form-control" id="ap-site"></select></div>
    <div class="setting-row"><label>${escHtml(t('Description'))}</label>
      <input type="text" class="form-control" id="ap-description" maxlength="300" value=""></div>
    <div class="apf-error" id="ap-name-error" hidden></div>
    <div id="ap-form"></div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="ap-cancel">${escHtml(t('Cancel'))}</button>
      <button class="btn btn-mint btn-sm" id="ap-save">${escHtml(t('Save'))}</button>
    </div>`);

  /** @type {HTMLElement} */
  const form = m.modal.querySelector('#ap-form');
  // Mount and open first: apfLoadContext is a request, and a dialog that
  // appears only once it settles reads as lag (see commit 57470af).
  form.innerHTML = '<div class="modal-skeleton"><div class="sk-line sk-w60"></div>'
    + '<div class="sk-line"></div><div class="sk-line sk-w80"></div></div>';
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#ap-name')).value = (profile && profile.name) || '';
  /** @type {HTMLInputElement} */ (m.modal.querySelector('#ap-description')).value = (profile && profile.description) || '';
  sitePickFill(/** @type {HTMLSelectElement} */ (m.modal.querySelector('#ap-site')),
    /** @type {HTMLElement} */ (m.modal.querySelector('#ap-site-field')),
    profile ? profile.site_id : null, { editing: !!editing });
  requestAnimationFrame(m.open);

  let ctx;
  try {
    ctx = await apfLoadContext();
  } catch (e) {
    form.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted-note';
    p.textContent = e.message || t('Could not load the agent actions.'); // throws — textContent
    form.appendChild(p);
    return;
  }
  apfRender(form, profile, /** @type {any} */ (ctx));

  /** @type {HTMLElement} */
  const nameError = m.modal.querySelector('#ap-name-error');
  const close = () => m.close();
  /** @type {HTMLButtonElement} */ (m.modal.querySelector('#ap-x')).addEventListener('click', close);
  /** @type {HTMLButtonElement} */ (m.modal.querySelector('#ap-cancel')).addEventListener('click', close);
  /** @type {HTMLButtonElement} */ (m.modal.querySelector('#ap-save')).addEventListener('click', async () => {
    const name = /** @type {HTMLInputElement} */ (m.modal.querySelector('#ap-name')).value.trim();
    // The form holds the agent settings; the two fields above are the profile's
    // own identity, which is why apfShowErrors cannot place a complaint here.
    if (!name) {
      nameError.textContent = t('Give the profile a name');
      nameError.hidden = false;
      return;
    }
    nameError.hidden = true;
    apfShowErrors(form, null);
    /** @type {{name: string, description: string, site_id?: string|number|null}} */
    const body = {
      name,
      description: /** @type {HTMLInputElement} */ (m.modal.querySelector('#ap-description')).value.trim(),
      ...apfRead(form),
    };
    const siteId = sitePickValue(/** @type {HTMLSelectElement} */ (m.modal.querySelector('#ap-site')),
      /** @type {HTMLElement} */ (m.modal.querySelector('#ap-site-field')));
    if (siteId !== undefined) body.site_id = siteId;
    /** @type {HTMLButtonElement} */
    const btn = m.modal.querySelector('#ap-save');
    btn.disabled = true;
    try {
      if (editing) {
        await apiJson(`/api/v1/agent-profiles/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
      } else {
        await apiJson('/api/v1/agent-profiles/', { method: 'POST', body: JSON.stringify(body) });
      }
      showToast(editing ? t('Agent profile saved') : t('Created {name}', { name }), 'success');
      close();
      loadAgentProfiles();
    } catch (e) {
      const errors = _apErrorFields(e);
      apfShowErrors(form, errors);
      if (errors && errors.name) {
        nameError.textContent = t('Name {reason}', { reason: errors.name });
        nameError.hidden = false;
      } else {
        showToast(e.message || t('Could not save the profile'), 'error');
      }
    } finally {
      btn.disabled = false;
    }
  });
}

/* QA-62 answers a refused setting with {errors: {field: message}} and no
   detail, so apiJson's message alone would say "Request failed" over a form of
   two dozen fields. */
function _apErrorFields(err) {
  const body = err && err.body;
  return (body && typeof body.errors === 'object' && body.errors) || null;
}

async function deleteAgentProfile(profile) {
  if (!profile || !profile.id || profile.builtin) return;
  const ok = await confirmModal(
    t('Delete agent profile “{name}”? Hosts already installed keep their settings.',
      { name: profile.name || profile.id }), // i18n-ignore — the checker cuts a literal at its apostrophe, so this wrapped sentence still reads as bare; re-wrap in double quotes once it learns not to.
    { confirmText: t('Delete'), danger: true });
  if (!ok) return;
  try {
    await apiJson(`/api/v1/agent-profiles/${profile.id}/`, { method: 'DELETE' });
    showToast(t('Agent profile deleted'), 'success');
    loadAgentProfiles();
  } catch (e) {
    showToast(e.message || t('Could not delete the profile'), 'error');
  }
}
