// vigil-alerts.js
// Owns: Alert lifecycle actions — acknowledge / un-acknowledge buttons, ack
//       duration menu, Docker fix suggestions.
// HTML: templates/pages/_alerts.html
// Depends on: vigil-utils.js (apiJson, showToast), vigil-tasks.js (openDefinitionEditor), vigil-nav.js (navigateTo)
// API: POST /api/v1/alerts/{id}/acknowledge/  POST /api/v1/alerts/{id}/unacknowledge/

/* ── Acknowledge (with optional duration) ────────────────────────────── */

// durationSeconds: null = acknowledge permanently; otherwise the ack lapses
// after this many seconds and the alert re-fires.
async function acknowledgeAlert(alertId, durationSeconds = null) {
  closeAckMenus();
  try {
    const body = durationSeconds ? { duration_seconds: durationSeconds } : {};
    await apiJson(`/api/v1/alerts/${alertId}/acknowledge/`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    showToast(durationSeconds ? 'Acknowledged — will re-fire if still open' : 'Alert acknowledged', 'success');
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

async function unacknowledgeAlert(alertId) {
  try {
    await apiJson(`/api/v1/alerts/${alertId}/unacknowledge/`, { method: 'POST' });
    showToast('Alert re-fired', 'success');
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

/* ── Ack duration menu ───────────────────────────────────────────────── */

function closeAckMenus() {
  document.querySelectorAll('.ack-menu.open').forEach(m => m.classList.remove('open'));
}

function toggleAckMenu(event, alertId) {
  event.stopPropagation();
  const menu = document.getElementById('ack-menu-' + alertId);
  if (!menu) return;
  const wasOpen = menu.classList.contains('open');
  closeAckMenus();
  if (!wasOpen) menu.classList.add('open');
}

document.addEventListener('click', closeAckMenus);

function suggestAgentUpdate(hostId) {
  const yaml = [
    `name: "Update Vigil Agent"`,
    `description: "Download the latest Vigil agent binary from the server and restart the service"`,
    `actions:`,
    `  - id: update`,
    `    type: update_agent`,
    `    params: {}`,
  ].join('\n');
  openDefinitionEditor(null, yaml);
}

function suggestDockerFix(hostId, containerName, image) {
  const yaml = [
    `name: "Update Docker Image: ${image}"`,
    `description: "Pull the latest ${image} and restart ${containerName}"`,
    `actions:`,
    `  - id: pull_new_image`,
    `    type: pull_image`,
    `    params:`,
    `      image: "${image}"`,
    `  - id: restart_container`,
    `    type: restart_container`,
    `    params:`,
    `      container_name: "${containerName}"`,
  ].join('\n');
  openDefinitionEditor(null, yaml);
}
