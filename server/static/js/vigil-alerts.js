// vigil-alerts.js
// Owns: Alert lifecycle actions — acknowledge / silence buttons, Docker fix suggestions.
// HTML: templates/pages/_alerts.html
// Depends on: vigil-utils.js (apiPost, showToast), vigil-tasks.js (openDefinitionEditor), vigil-nav.js (navigateTo)
// API: POST /api/v1/alerts/{id}/acknowledge/

async function acknowledgeAlert(alertId) {
  try {
    await apiPost(`/api/v1/alerts/${alertId}/acknowledge/`);
    showToast('Alert acknowledged', 'success');
    setTimeout(() => location.reload(), 800);
  } catch (e) {
    showToast('Failed: ' + e.message, 'error');
  }
}

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
