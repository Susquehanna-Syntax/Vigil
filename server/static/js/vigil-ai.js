// vigil-ai.js
// Owns: the shared "AI suggested fix" modal used by Alerts and Docker, plus
// the AI-endpoint settings card. Suggestions come back as validated task
// YAML; the user reviews and clicks "Open in editor" — nothing auto-runs.
// Depends on: vigil-utils.js (apiJson, showToast, escHtml), vigil-tasks.js
//             (openDefinitionEditor).

/* ── Suggestion modal ────────────────────────────────────────────────── */
function _ensureAiModal() {
  let modal = document.getElementById('ai-suggest-modal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'ai-suggest-modal';
  modal.className = 'ai-modal-overlay';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="ai-modal">
      <div class="ai-modal-head">
        <span class="ai-modal-title">Suggested fixes</span>
        <button class="ai-modal-close" aria-label="Close">&times;</button>
      </div>
      <div class="ai-modal-sub" id="ai-modal-sub"></div>
      <div class="ai-modal-body" id="ai-modal-body"></div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal || e.target.closest('.ai-modal-close')) modal.hidden = true;
  });
  return modal;
}

function _openAiModal(subtitle) {
  const modal = _ensureAiModal();
  document.getElementById('ai-modal-sub').textContent = subtitle || '';
  document.getElementById('ai-modal-body').innerHTML =
    '<div class="ai-loading">Asking your model… local endpoints can take a minute.</div>';
  modal.hidden = false;
}

function _renderSuggestions(suggestions) {
  const body = document.getElementById('ai-modal-body');
  if (!suggestions || !suggestions.length) {
    body.innerHTML = '<div class="ai-loading">No usable suggestions came back. ' +
      'The model may have proposed nothing valid — try again or refine manually.</div>';
    return;
  }
  body.innerHTML = suggestions.map((s, i) => {
    const risk = (s.parsed && s.parsed.risk) || 'standard';
    const name = (s.parsed && s.parsed.name) || `Suggestion ${i + 1}`;
    return `<div class="ai-suggestion">
      <div class="ai-suggestion-head">
        <span class="ai-suggestion-name">${escHtml(name)}</span>
        <span class="risk-pill risk-${escHtml(risk)}">${escHtml(risk)}</span>
      </div>
      <pre class="ai-suggestion-yaml">${escHtml(s.yaml)}</pre>
      <div class="ai-suggestion-actions">
        <button class="btn btn-sm btn-outline" data-ai-copy="${i}">Copy YAML</button>
        <button class="btn btn-sm btn-mint" data-ai-open="${i}">Open in editor</button>
      </div>
    </div>`;
  }).join('');
  body._suggestions = suggestions;
  body.querySelectorAll('[data-ai-open]').forEach(btn => {
    btn.addEventListener('click', () => {
      const s = suggestions[+btn.dataset.aiOpen];
      document.getElementById('ai-suggest-modal').hidden = true;
      if (typeof openDefinitionEditor === 'function') openDefinitionEditor(null, s.yaml);
    });
  });
  body.querySelectorAll('[data-ai-copy]').forEach(btn => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(suggestions[+btn.dataset.aiCopy].yaml);
      showToast('YAML copied', 'success');
    });
  });
}

async function requestAiSuggestions(url, subtitle) {
  _openAiModal(subtitle);
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
      credentials: 'same-origin',
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409) {
      document.getElementById('ai-modal-body').innerHTML =
        `<div class="ai-loading">${escHtml(data.detail || 'AI is not configured.')}
         <br><br><button class="btn btn-sm btn-mint" onclick="document.getElementById('ai-suggest-modal').hidden=true;navigateTo('settings');">Configure in Settings</button></div>`;
      return;
    }
    if (!resp.ok) {
      document.getElementById('ai-modal-body').innerHTML =
        `<div class="ai-loading">Suggestion failed: ${escHtml(data.detail || 'error')}</div>`;
      return;
    }
    _renderSuggestions(data.suggestions);
  } catch (e) {
    document.getElementById('ai-modal-body').innerHTML =
      `<div class="ai-loading">Request failed: ${escHtml(e.message)}</div>`;
  }
}

function suggestFixForAlert(alertId, subtitle) {
  requestAiSuggestions(`/api/v1/ai/suggest/alert/${alertId}/`, subtitle);
}

function suggestFixForContainer(hostId, containerId, subtitle) {
  requestAiSuggestions(
    `/api/v1/ai/suggest/docker/${hostId}/${encodeURIComponent(containerId)}/`, subtitle);
}

/* ── AI endpoint settings card ───────────────────────────────────────── */
async function loadAiSettings() {
  const card = document.getElementById('ai-settings-card');
  if (!card) return;
  try {
    const d = await apiJson('/api/v1/ai/settings/');
    document.getElementById('ai-provider').value = d.provider || 'openai';
    document.getElementById('ai-base-url').value = d.base_url || '';
    document.getElementById('ai-model').value = d.model || '';
    document.getElementById('ai-enabled').checked = !!d.enabled;
    document.getElementById('ai-key-state').textContent =
      d.api_key_set ? 'A key is stored.' : 'No key stored (fine for keyless local endpoints).';
  } catch (e) { /* card simply stays blank if unreachable */ }
}

async function saveAiSettings() {
  const body = {
    provider: document.getElementById('ai-provider').value,
    base_url: document.getElementById('ai-base-url').value.trim(),
    model: document.getElementById('ai-model').value.trim(),
    enabled: document.getElementById('ai-enabled').checked,
  };
  const key = document.getElementById('ai-key').value;
  if (key) body.api_key = key;
  try {
    await apiJson('/api/v1/ai/settings/', { method: 'POST', body: JSON.stringify(body) });
    document.getElementById('ai-key').value = '';
    showToast('AI settings saved', 'success');
    loadAiSettings();
  } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  const save = document.getElementById('ai-save-btn');
  if (save) save.addEventListener('click', saveAiSettings);
  loadAiSettings();
});
