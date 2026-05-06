// vigil-alerts.js
// Owns: Alert lifecycle actions — acknowledge / silence buttons.
// HTML: templates/pages/_alerts.html
// Depends on: vigil-utils.js (apiPost, showToast)
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
