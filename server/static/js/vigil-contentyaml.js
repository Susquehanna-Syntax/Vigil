/* "Edit as YAML" and "Submit to Community" for baselines and automations.
 *
 * Tasks have had this since the beginning, because a task *is* its YAML — the
 * editor stores the source. A baseline and an automation are rows, so their
 * YAML is generated on the server and parsed back there too: the document
 * names tasks and baselines by slug rather than by id, and only the server can
 * resolve a slug against the operator's library.
 *
 * That is why every button here is a round trip rather than a string built in
 * the browser. It also means the YAML you read is exactly the document a
 * submission would carry — there is no second rendering to drift from it.
 */

const CONTENT_YAML_ENDPOINTS = {
  baseline: { get: id => `/api/v1/baselines/${id}/yaml/`, put: '/api/v1/baselines/yaml/',
              idField: 'baseline_id', dir: 'baselines' },
  automation: { get: id => `/api/v1/automations/${id}/yaml/`, put: '/api/v1/automations/yaml/',
                idField: 'automation_id', dir: 'automations' },
};

const contentYamlState = { kind: null, id: null, filename: '' };

function _cyEl(suffix) { return document.getElementById('content-yaml-' + suffix); }

function _cyError(message) {
  const box = _cyEl('error');
  if (!box) return;
  box.textContent = message || '';
  box.classList.toggle('show', Boolean(message));
}

function closeContentYaml() {
  _cyEl('overlay')?.classList.remove('open');
  _cyEl('modal')?.classList.remove('open');
  _cyError('');
}

/**
 * Open the YAML view for a saved baseline or automation.
 *
 * Only for saved items: the document is generated server-side from the stored
 * row, so there is nothing to show for an item that has never been saved. The
 * callers disable the button in that state rather than opening an empty box.
 */
async function openContentYaml(kind, id) {
  const cfg = CONTENT_YAML_ENDPOINTS[kind];
  if (!cfg || !id) return;
  try {
    const data = await apiJson(cfg.get(id));
    contentYamlState.kind = kind;
    contentYamlState.id = id;
    contentYamlState.filename = data.filename || '';
    _cyEl('text').value = data.yaml || '';
    _cyEl('title').textContent = `Edit ${kind} as YAML`;
    _cyEl('filename').textContent =
      `Shared as ${cfg.dir}/${data.filename} in the community repo.`;
    _cyError('');
    _cyEl('overlay').classList.add('open');
    _cyEl('modal').classList.add('open');
  } catch (e) {
    // The honest failure here is an automation pinned to one host, which
    // cannot be represented as a shareable document at all. The server says
    // why and what to change.
    showToast(e.message || 'Could not read this as YAML', 'error');
  }
}

async function saveContentYaml() {
  const cfg = CONTENT_YAML_ENDPOINTS[contentYamlState.kind];
  if (!cfg) return;
  const btn = _cyEl('save');
  btn.disabled = true; btn.style.opacity = '0.6';
  try {
    const body = { yaml: _cyEl('text').value };
    body[cfg.idField] = contentYamlState.id;
    await apiJson(cfg.put, { method: 'POST', body: JSON.stringify(body) });
    showToast('Saved', 'success');
    closeContentYaml();
    if (typeof loadBaselines === 'function') loadBaselines();
    if (typeof loadAutomations === 'function') loadAutomations();
  } catch (e) {
    _cyError(e.message || 'Save failed');
  } finally {
    btn.disabled = false; btn.style.opacity = '1';
  }
}

async function copyContentYaml() {
  try {
    await navigator.clipboard.writeText(_cyEl('text').value);
    showToast('YAML copied to clipboard', 'success');
  } catch {
    showToast('Clipboard blocked — select the text and copy it', 'error');
  }
}

/**
 * Open a prefilled "new file" page on the community repo.
 *
 * Same flow tasks use: GitHub opens its editor with the filename and body
 * already in place, and the contributor opens the PR. Nothing is pushed from
 * here — Vigil has no credentials on that repo and should not.
 */
function submitContentYaml() {
  const cfg = CONTENT_YAML_ENDPOINTS[contentYamlState.kind];
  if (!cfg) return;
  const yaml = _injectCommunityMetadata((_cyEl('text').value || '').trim());
  if (!yaml) { showToast('Nothing to submit', 'error'); return; }
  const filename = contentYamlState.filename || `${contentYamlState.kind}.yaml`;
  const url = `${COMMUNITY_REPO_URL}/new/main/${cfg.dir}`
    + `?filename=${encodeURIComponent(filename)}&value=${encodeURIComponent(yaml)}`;
  window.open(url, '_blank', 'noopener');
  showToast('Opening GitHub — review it there, then open the pull request', 'success');
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  _cyEl('cancel')?.addEventListener('click', closeContentYaml);
  _cyEl('overlay')?.addEventListener('click', closeContentYaml);
  _cyEl('save')?.addEventListener('click', saveContentYaml);
  _cyEl('copy')?.addEventListener('click', copyContentYaml);
  _cyEl('submit')?.addEventListener('click', submitContentYaml);

  // The editors carry the id of the row they are editing in data-editing.
  // Empty means "new", and there is no server-side document for that yet.
  document.getElementById('bl-yaml-btn')?.addEventListener('click', () => {
    const id = document.getElementById('bl-editor-modal')?.dataset.editing;
    if (!id) { showToast('Save the baseline first, then edit it as YAML', 'error'); return; }
    openContentYaml('baseline', id);
  });
  document.getElementById('auto-yaml-btn')?.addEventListener('click', () => {
    const id = document.getElementById('auto-editor-modal')?.dataset.editing;
    if (!id) { showToast('Save the automation first, then edit it as YAML', 'error'); return; }
    openContentYaml('automation', id);
  });
});
