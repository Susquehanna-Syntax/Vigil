// vigil-deeplink.js
// Opens '#open/<type>/<id>' — the link shape Vigil mints for itself, and the
// one a Jackil helpdesk ticket hands to an agent clicking "Open in Vigil".
// Nothing here decides what is visible: each opener calls exactly what the
// page's own UI calls, so a link to a row you cannot see fails the way
// clicking through the page would fail, with that page's own error.
// HTML: none — it runs over whatever the SPA already has mounted.
// Depends on: vigil-utils.js (showToast), and the page modules' globals
// (navigateTo, selectMonitorHost, openDefinitionEditor, _startEdit,
// openTaskDetail, loadAutomations, _openAutoEditor), each guarded with a
// typeof test because script order in base.html is not a contract.

const DEEP_LINK = /^#open\/(host|task|playbook|run|automation)\/([0-9A-Za-z-]{1,64})$/;

/** Parses '#open/<type>/<id>'; returns {type, id} or null. */
function parseDeepLink(hash) {
  const match = DEEP_LINK.exec(hash || '');
  return match ? { type: match[1], id: match[2] } : null;
}

async function openVigilItem(type, id) {
  switch (type) {
    case 'host':
      if (typeof navigateTo === 'function') navigateTo('monitor');
      if (typeof selectMonitorHost === 'function') selectMonitorHost(id);
      break;
    case 'task':
      if (typeof openDefinitionEditor === 'function') openDefinitionEditor(id);
      break;
    case 'playbook':
      if (typeof navigateTo === 'function') navigateTo('playbooks');
      if (typeof _startEdit === 'function') _startEdit(id);
      break;
    case 'run':
      if (typeof navigateTo === 'function') navigateTo('tasks');
      if (typeof openTaskDetail === 'function') openTaskDetail(id);
      break;
    case 'automation': {
      // An automation has no page of its own: the editor is opened from the
      // row in the Playbooks pane's Automation sub-tab, and the editor needs
      // the whole row rather than an id. So: reveal the tab, load the list,
      // find the one named in the link.
      if (typeof navigateTo === 'function') navigateTo('playbooks');
      const tab = document.querySelector('#page-playbooks .sub-tab[data-subtab="auto-panel"]');
      if (tab) tab.click();
      if (typeof loadAutomations !== 'function') break;
      const automations = (await loadAutomations()) || [];
      const found = automations.find((a) => String(a.id) === String(id));
      if (!found) { showToast('That automation is not available', 'error'); break; }
      if (typeof _openAutoEditor === 'function') _openAutoEditor(found);
      break;
    }
    default:
      break;
  }
}

/** Opens what the hash names and then drops the hash, so a reload does not
    reopen the same thing over whatever the user navigated to next. */
async function _runDeepLink() {
  const link = parseDeepLink(location.hash);
  if (!link) return;
  history.replaceState(null, '', location.pathname + location.search);
  await openVigilItem(link.type, link.id);
}

document.addEventListener('DOMContentLoaded', _runDeepLink);
window.addEventListener('hashchange', _runDeepLink);
