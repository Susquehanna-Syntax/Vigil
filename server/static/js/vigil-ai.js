// vigil-ai.js
// Owns: the multi-provider "Suggested fixes" modal (Alerts + Docker) and the
// AI providers manager on Settings. A request fans out to every selected
// provider in parallel; each shows its own loading bar + elapsed timer and
// resolves independently into a comparison column. A heuristic marks a "best"
// pick, but the human always chooses — LLM output is untrusted and nothing
// runs without going through the task editor.
// Depends on: vigil-utils.js, vigil-tasks.js (openDefinitionEditor).

let _aiProviders = [];        // cached enabled providers for the picker
const RISK_RANK = { low: 0, standard: 1, high: 2 };

/* ── Modal shell ─────────────────────────────────────────────────────── */
function _ensureAiModal() {
  let overlay = document.getElementById('ai-overlay');
  if (overlay) return overlay;
  overlay = document.createElement('div');
  overlay.id = 'ai-overlay';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal ai-modal">
      <div class="modal-title">
        Suggested fixes
        <button class="modal-close" id="ai-close" aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="page-sub" id="ai-context" style="margin:-14px 0 18px;"></div>
      <div id="ai-picker-wrap">
        <div class="section-label" style="margin-top:0;">Ask which models</div>
        <div class="ai-provider-picker" id="ai-picker"></div>
        <button class="btn btn-mint btn-sm" id="ai-run" style="margin-top:12px;">Get suggestions</button>
      </div>
      <div class="ai-compare" id="ai-compare"></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#ai-close').onclick = () => _closeAi();
  overlay.onclick = (e) => { if (e.target === overlay) _closeAi(); };
  return overlay;
}

function _closeAi() {
  const o = document.getElementById('ai-overlay');
  if (o) o.classList.remove('open');
}

async function _openAi(context, runFn) {
  const overlay = _ensureAiModal();
  document.getElementById('ai-context').textContent = context || '';
  document.getElementById('ai-compare').innerHTML = '';
  const picker = document.getElementById('ai-picker');
  document.getElementById('ai-picker-wrap').style.display = '';
  picker.innerHTML = '<span class="ai-empty">Loading providers…</span>';
  requestAnimationFrame(() => overlay.classList.add('open'));

  try {
    _aiProviders = (await apiJson('/api/v1/ai/providers/')).filter(p => p.enabled && p.configured);
  } catch { _aiProviders = []; }

  if (!_aiProviders.length) {
    document.getElementById('ai-picker-wrap').innerHTML =
      `<div class="ai-empty">No AI providers are configured.
       <button class="btn btn-mint btn-sm" style="margin-left:8px;" onclick="_closeAi();navigateTo('settings');">Add one in Settings</button></div>`;
    return;
  }
  picker.innerHTML = _aiProviders.map(p => `
    <label class="ai-pick on" data-pid="${p.id}">
      <input type="checkbox" checked>
      <span>${escHtml(p.name)}</span>
      <span class="ai-pick-model">${escHtml(p.model)}</span>
    </label>`).join('');
  picker.querySelectorAll('.ai-pick').forEach(el => {
    const cb = el.querySelector('input');
    cb.addEventListener('change', () => el.classList.toggle('on', cb.checked));
  });
  document.getElementById('ai-run').onclick = () => {
    const ids = [...picker.querySelectorAll('.ai-pick')].filter(el => el.querySelector('input').checked)
      .map(el => +el.dataset.pid);
    if (!ids.length) return showToast('Pick at least one model', 'error');
    document.getElementById('ai-picker-wrap').style.display = 'none';
    _runComparison(ids, runFn);
  };
}

/* ── Fan-out + compare ───────────────────────────────────────────────── */
function _runComparison(providerIds, runFn) {
  const grid = document.getElementById('ai-compare');
  grid.style.gridTemplateColumns = providerIds.length > 1 ? '1fr 1fr' : '1fr';
  grid.innerHTML = providerIds.map(id => {
    const p = _aiProviders.find(x => x.id === id);
    return `<div class="ai-col" id="ai-col-${id}">
      <div class="ai-col-head"><span class="ai-col-name">${escHtml(p.name)}</span>
        <span class="ai-col-time" id="ai-time-${id}">0.0s</span></div>
      <div class="ai-progress"><div class="ai-progress-bar"></div></div>
      <div class="ai-loading-row"><span>${escHtml(p.model)}</span><span>thinking…</span></div>
    </div>`;
  }).join('');

  const results = {};
  providerIds.forEach(id => {
    const t0 = performance.now();
    const timer = setInterval(() => {
      const el = document.getElementById(`ai-time-${id}`);
      if (el) el.textContent = ((performance.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
    runFn(id)
      .then(data => { results[id] = data; })
      .catch(err => { results[id] = { error: err.message }; })
      .finally(() => {
        clearInterval(timer);
        _renderColumn(id, results[id]);
        _rankBest(results, providerIds);
      });
  });
}

function _renderColumn(id, data) {
  const col = document.getElementById(`ai-col-${id}`);
  if (!col) return;
  const p = _aiProviders.find(x => x.id === id);
  const time = data && data.elapsed_ms != null ? (data.elapsed_ms / 1000).toFixed(1) + 's' : '';
  let body;
  if (!data || data.error) {
    body = `<div class="ai-err">${escHtml((data && data.error) || 'failed')}</div>`;
  } else if (!data.suggestions || !data.suggestions.length) {
    body = '<div class="ai-empty">No valid suggestions returned.</div>';
  } else {
    body = data.suggestions.map((s, i) => `
      <div class="ai-sug" data-pid="${id}" data-idx="${i}">
        <div class="ai-sug-head">
          <span class="ai-sug-name">${escHtml((s.parsed && s.parsed.name) || 'Suggestion')}</span>
          <span class="risk-badge risk-${escHtml(s.risk || 'standard')}">${escHtml(s.risk || 'standard')}</span>
        </div>
        <pre class="ai-sug-yaml">${escHtml(s.yaml)}</pre>
        <div class="ai-sug-actions">
          <button class="btn btn-outline btn-xs" data-ai-copy>Copy</button>
          <button class="btn btn-mint btn-xs" data-ai-open>Use this</button>
        </div>
      </div>`).join('');
  }
  col.innerHTML = `<div class="ai-col-head">
      <span class="ai-col-name">${escHtml(p.name)}</span>
      <span class="ai-col-time">${time}</span></div>${body}`;
  col.querySelectorAll('.ai-sug').forEach(node => {
    const s = data.suggestions[+node.dataset.idx];
    node.querySelector('[data-ai-open]').addEventListener('click', () => {
      _closeAi();
      if (typeof openDefinitionEditor === 'function') openDefinitionEditor(null, s.yaml);
    });
    node.querySelector('[data-ai-copy]').addEventListener('click', () => {
      navigator.clipboard.writeText(s.yaml); showToast('YAML copied', 'success');
    });
  });
}

// Heuristic "best": a provider that returned at least one valid suggestion,
// preferring the lowest-risk top suggestion, then the fastest. Marks the
// column and its leading suggestion — the human still clicks to use it.
function _rankBest(results, ids) {
  const done = ids.filter(id => results[id]);
  if (done.length < ids.length) return;  // wait for all before ranking
  let best = null, bestKey = null;
  for (const id of ids) {
    const d = results[id];
    if (!d || d.error || !d.suggestions || !d.suggestions.length) continue;
    const topRisk = Math.min(...d.suggestions.map(s => RISK_RANK[s.risk] ?? 1));
    const key = [topRisk, d.elapsed_ms || 1e9];
    if (!bestKey || key[0] < bestKey[0] || (key[0] === bestKey[0] && key[1] < bestKey[1])) {
      bestKey = key; best = id;
    }
  }
  document.querySelectorAll('.ai-col').forEach(c => c.classList.remove('best'));
  document.querySelectorAll('.ai-best-tag').forEach(t => t.remove());
  if (best != null) {
    const col = document.getElementById(`ai-col-${best}`);
    col.classList.add('best');
    const head = col.querySelector('.ai-col-head');
    const tag = document.createElement('span');
    tag.className = 'ai-best-tag'; tag.textContent = 'best pick';
    head.appendChild(tag);
    const firstSug = col.querySelector('.ai-sug');
    if (firstSug) firstSug.classList.add('top');
  }
}

/* ── Public entry points ─────────────────────────────────────────────── */
function suggestFixForAlert(alertId, context) {
  _openAi(context, (pid) => apiJson(`/api/v1/ai/suggest/alert/${alertId}/`,
    { method: 'POST', body: JSON.stringify({ provider_id: pid }) }));
}

function suggestFixForContainer(hostId, containerId, name) {
  _openAi(`Container: ${name || containerId}`, (pid) =>
    apiJson(`/api/v1/ai/suggest/docker/${hostId}/${encodeURIComponent(containerId)}/`,
      { method: 'POST', body: JSON.stringify({ provider_id: pid }) }));
}

/* ── Providers manager (Settings) ────────────────────────────────────── */
async function loadAiProviders() {
  const list = document.getElementById('ai-providers-list');
  if (!list) return;
  try {
    const rows = await apiJson('/api/v1/ai/providers/');
    list.innerHTML = rows.length ? rows.map(p => `
      <div class="provider-row">
        <div class="provider-row-main">
          <span class="provider-row-name">${escHtml(p.name)}
            <span class="bl-badge ${p.enabled ? 'on' : 'off'}">${p.enabled ? 'enabled' : 'off'}</span>
            ${p.configured ? '' : '<span class="bl-badge off">needs setup</span>'}</span>
          <span class="provider-row-sub">${escHtml(p.kind)} · ${escHtml(p.model || 'no model')} · ${escHtml(p.base_url || 'default endpoint')}${p.api_key_set ? ' · key set' : ''}</span>
        </div>
        <div class="card-actions">
          <button class="btn btn-outline btn-xs" data-ap-edit="${p.id}">Edit</button>
          <button class="btn btn-outline btn-xs" data-ap-toggle="${p.id}" data-en="${p.enabled}">${p.enabled ? 'Disable' : 'Enable'}</button>
          <button class="btn btn-outline btn-xs" style="color:var(--rose);" data-ap-del="${p.id}">Delete</button>
        </div>
      </div>`).join('') : '<p class="muted-note">No providers yet. Add your first model endpoint below.</p>';
    list.querySelectorAll('[data-ap-del]').forEach(b => b.addEventListener('click', async () => {
      if (!(await confirmModal('Delete this AI provider?', { danger: true, confirmText: 'Delete' }))) return;
      await fetch(`/api/v1/ai/providers/${b.dataset.apDel}/`, { method: 'DELETE', headers: { 'X-CSRFToken': getCsrf() }, credentials: 'same-origin' });
      loadAiProviders();
    }));
    list.querySelectorAll('[data-ap-toggle]').forEach(b => b.addEventListener('click', async () => {
      await apiJson(`/api/v1/ai/providers/${b.dataset.apToggle}/`, { method: 'PATCH', body: JSON.stringify({ enabled: b.dataset.en !== 'true' }) });
      loadAiProviders();
    }));
    list.querySelectorAll('[data-ap-edit]').forEach(b => b.addEventListener('click', () => _editProvider(rows.find(r => r.id == b.dataset.apEdit))));
  } catch (e) { /* card stays as-is */ }
}

function _editProvider(p) {
  document.getElementById('ap-form').dataset.editing = p ? p.id : '';
  document.getElementById('ap-form-title').textContent = p ? 'Edit provider' : 'Add a provider';
  document.getElementById('ap-name').value = p ? p.name : '';
  document.getElementById('ap-kind').value = p ? p.kind : 'openai';
  document.getElementById('ap-url').value = p ? p.base_url : '';
  document.getElementById('ap-model').value = p ? p.model : '';
  document.getElementById('ap-key').value = '';
  document.getElementById('ap-key-note').textContent = p && p.api_key_set ? 'A key is stored — leave blank to keep it.' : '';
  document.getElementById('ap-form').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function _saveProvider() {
  const body = {
    name: document.getElementById('ap-name').value.trim() || 'Provider',
    kind: document.getElementById('ap-kind').value,
    base_url: document.getElementById('ap-url').value.trim(),
    model: document.getElementById('ap-model').value.trim(),
  };
  const key = document.getElementById('ap-key').value;
  if (key) body.api_key = key;
  const editing = document.getElementById('ap-form').dataset.editing;
  try {
    if (editing) await apiJson(`/api/v1/ai/providers/${editing}/`, { method: 'PATCH', body: JSON.stringify(body) });
    else await apiJson('/api/v1/ai/providers/', { method: 'POST', body: JSON.stringify(body) });
    showToast('Provider saved', 'success');
    _editProvider(null);
    loadAiProviders();
  } catch (e) { showToast('Save failed: ' + e.message, 'error'); }
}

document.addEventListener('DOMContentLoaded', () => {
  const save = document.getElementById('ap-save');
  if (save) save.addEventListener('click', _saveProvider);
  const add = document.getElementById('ap-new');
  if (add) add.addEventListener('click', () => _editProvider(null));
  loadAiProviders();
});
