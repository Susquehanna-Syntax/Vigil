/* Header site switcher (S1 04g).
 *
 * Someone in more than one site ticks the sites they want to look at; every
 * page then lists only those. The choice is saved on their profile
 * (/api/v1/scope/selection/), so it follows them to another browser, and the
 * server applies it to every scoped read. It is a view, not a permission:
 * what they may do does not change. Hidden for anyone with one site, and on
 * Community. */

let _siteSwitch = null;

async function siteSwitchInit() {
  const btn = document.getElementById('site-switch');
  if (!btn) return;
  try {
    _siteSwitch = await apiJson('/api/v1/scope/selection/');
  } catch (e) {
    return; // hidden: Community, or an account the switcher does not apply to
  }
  if (!_siteSwitch.enabled) return;
  _siteSwitchLabel();
  btn.hidden = false;
}

function _siteSwitchTicked() {
  const sel = _siteSwitch.selected;
  return sel.length ? new Set(sel) : new Set(_siteSwitch.sites.map((s) => s.id));
}

function _siteSwitchLabel() {
  const ticked = _siteSwitchTicked();
  const all = ticked.size === _siteSwitch.sites.length;
  const one = ticked.size === 1 ? _siteSwitch.sites.find((s) => ticked.has(s.id)) : null;
  const text = all ? 'All my sites' : one ? one.name : `${ticked.size} sites`;
  const btn = document.getElementById('site-switch');
  btn.dataset.tip = all ? 'Sites: all of yours' : `Sites: ${text}`;
  btn.classList.toggle('site-switch-narrowed', !all);
  document.getElementById('site-switch-label').textContent = text;
  const count = document.getElementById('site-switch-count');
  count.textContent = all ? '' : String(ticked.size);
  count.hidden = all;
}

function openSiteSwitch() {
  if (!_siteSwitch || !_siteSwitch.enabled) return;
  const ticked = _siteSwitchTicked();
  const m = mountModal('siteswitch', { variant: 'm-pop' });
  m.setBody(`
    <div class="modal-title"><span>Sites to show</span>
      <button class="modal-close" id="ss-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <p class="muted ss-note">Every page lists only the ticked sites. What you may do does not change.</p>
    ${_siteSwitch.sites.length > 6 ? `<input class="form-control ss-search" id="ss-search" type="search"
      placeholder="Find a site" autocomplete="off" aria-label="Find a site">` : ''}
    <div class="ss-list" id="ss-list">${_siteSwitch.sites.map((s) => `
      <label class="ss-site" data-name="${escAttr(s.name.toLowerCase())}">
        <input type="checkbox" value="${escAttr(s.id)}"${ticked.has(s.id) ? ' checked' : ''}>
        <span>${escHtml(s.is_global ? 'Global (unassigned hosts)' : s.name)}</span>
      </label>`).join('')}</div>
    <div class="confirm-actions">
      <button class="btn btn-outline btn-sm" id="ss-all">All my sites</button>
      <button class="btn btn-mint btn-sm" id="ss-apply">Show these</button>
    </div>`);

  const boxes = () => [...m.modal.querySelectorAll('#ss-list input[type=checkbox]')];
  // The last ticked box cannot be cleared: an empty view would show nothing.
  const guard = () => {
    const on = boxes().filter((b) => b.checked);
    boxes().forEach((b) => { b.disabled = b.checked && on.length === 1; });
  };
  boxes().forEach((b) => b.addEventListener('change', guard));
  guard();

  const search = m.modal.querySelector('#ss-search');
  if (search) {
    search.addEventListener('input', () => {
      const q = search.value.trim().toLowerCase();
      m.modal.querySelectorAll('.ss-site').forEach((row) => {
        row.hidden = q !== '' && !row.dataset.name.includes(q);
      });
    });
  }

  const save = async (body) => {
    try {
      _siteSwitch = await apiJson('/api/v1/scope/selection/', { method: 'PUT', body: JSON.stringify(body) });
    } catch (e) {
      showToast(e.message || 'Could not change the sites shown', 'error');
      return;
    }
    m.close();
    // Every list on the page was drawn for the old selection.
    window.location.reload();
  };
  m.modal.querySelector('#ss-x').onclick = () => m.close();
  m.modal.querySelector('#ss-all').onclick = () => save({ all: true });
  m.modal.querySelector('#ss-apply').onclick = () =>
    save({ selected: boxes().filter((b) => b.checked).map((b) => b.value) });
  requestAnimationFrame(() => { m.open(); (search || m.modal.querySelector('#ss-apply')).focus(); });
}

document.addEventListener('DOMContentLoaded', siteSwitchInit);
