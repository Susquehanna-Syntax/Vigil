/* Settings for site admins (S1 04h).
 *
 * A site admin sees install-wide settings the way /api/v1/accounts/
 * settings-access/ says: "hidden" panes leave the nav, "greyed" panes stay
 * readable with every control disabled and a note saying who can change them.
 * The server enforces the same map; this only keeps the page honest. */

const _SETTINGS_GREYED_NOTE = 'Only an administrator of the whole install can change these settings. '
  + 'You can read them.';

async function applySettingsAccess() {
  const nav = document.getElementById('settings-nav');
  if (!nav) return;
  let access;
  try {
    access = await apiJson('/api/v1/accounts/settings-access/');
  } catch (e) {
    return;
  }
  Object.entries(access).forEach(([page, how]) => {
    const item = nav.querySelector(`.settings-nav-item[data-pane="${page}"]`);
    const pane = document.querySelector(`#page-settings .settings-pane[data-pane="${page}"]`);
    if (how === 'hidden') {
      if (item) item.hidden = true;
      if (pane) pane.remove();
    } else if (how === 'greyed' && pane) {
      _greySettingsPane(pane);
    }
  });
}

function _greySettingsPane(pane) {
  pane.classList.add('settings-greyed');
  if (!pane.querySelector('.settings-greyed-note')) {
    const note = document.createElement('p');
    note.className = 'settings-greyed-note';
    note.textContent = _SETTINGS_GREYED_NOTE;
    pane.prepend(note);
  }
  // Panes draw their lists after load (relays, images, users), so disable
  // controls as they appear, not just the ones present now.
  const disable = () => pane.querySelectorAll('input, select, textarea, button').forEach((el) => {
    if (!el.disabled) el.disabled = true;
  });
  disable();
  new MutationObserver(disable).observe(pane, { childList: true, subtree: true });
}

document.addEventListener('DOMContentLoaded', applySettingsAccess);
