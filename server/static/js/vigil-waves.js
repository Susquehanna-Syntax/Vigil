/* Wave management — the Deployments › Waves panel.
 *
 * Built to the same shape as playbooks and automations: a toolbar with search
 * and a New button, a list of cards, and a modal editor. Nothing here edits a
 * host's tags. A wave only chooses which tags it matches; tags stay owned by
 * the host, so there is exactly one place a host's membership is decided.
 */

let _allWaves = [];
let _waveGroups = [];
let _editingWaveId = null;

/* ── List ─────────────────────────────────────────────────────────────── */

function _filterWaves() {
  const q = (document.getElementById('wave-search')?.value || '').trim().toLowerCase();
  if (!q) return _allWaves;
  return _allWaves.filter(w =>
    (w.name || '').toLowerCase().includes(q) ||
    (w.tags || []).some(t => (t || '').toLowerCase().includes(q)));
}

function _waveGroupName(w) {
  const g = (_waveGroups || []).find(x => x.id === w.group);
  return g ? g.name : '';
}

function _waveCard(w) {
  const tags = (w.tags || []).length
    ? (w.tags || []).map(t => `<span class="chip">${escHtml(t)}</span>`).join(' ')
    : '<span style="color:var(--rose);">no tags — matches nothing</span>';

  // Matching vs actually-patched. They differ when an earlier wave already
  // claimed a host, and that difference is the thing operators trip over.
  const overlap = w.host_count - w.exclusive_host_count;
  const hosts = w.host_count === 0
    ? '<span style="color:var(--text-3);">no hosts match</span>'
    : `${w.exclusive_host_count} host${w.exclusive_host_count === 1 ? '' : 's'}` +
      (overlap > 0
        ? ` <span style="color:var(--text-3);">(${overlap} also in an earlier wave)</span>`
        : '');

  return `<div class="bl-card">
    <div class="bl-card-head">
      <div>
        <span class="bl-name">${escHtml(w.order)} &middot; ${escHtml(w.name)}</span>
        <span class="bl-badge ${w.enabled ? 'on' : 'off'}">${w.enabled ? 'enabled' : 'off'}</span>
        ${_waveGroupName(w) ? `<span class="chip">${escHtml(_waveGroupName(w))}</span>` : ''}
      </div>
      <div style="display:flex;gap:6px;">
        <button class="btn btn-sky btn-sm" onclick="openWaveEditor(${w.id})">Edit</button>
        <button class="btn btn-ghost btn-sm" style="color:var(--rose);" onclick="deleteWave(${w.id})">Delete</button>
      </div>
    </div>
    <div class="bl-card-body" style="color:var(--text-2);font-size:12px;line-height:1.7;">
      <div>${hosts} &middot; validation window ${escHtml(w.validation_hours)}h</div>
      <div style="margin-top:4px;">${tags}</div>
    </div>
  </div>`;
}

function _renderWaves() {
  const box = document.getElementById('waves-list');
  if (!box) return;
  const items = _filterWaves();
  if (!items.length) {
    const searching = _allWaves.length > 0;
    box.innerHTML = `<div class="empty-block">
      <h4>${searching ? 'No waves match that search' : 'No waves yet'}</h4>
      <p>${searching
        ? 'Clear the search to see them all.'
        : 'A wave is a group of machines, matched by tag, that patch together. Create a small first wave of spare machines, then a wider one — a bad update shows up on the spares before it reaches everything.'}</p>
    </div>`;
    return;
  }
  box.innerHTML = items.map(_waveCard).join('');
}

async function loadWaves() {
  const box = document.getElementById('waves-list');
  if (!box) return;
  box.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    [_allWaves, _waveGroups] = await Promise.all([
      apiJson('/api/v1/waves/'),
      apiJson('/api/v1/wave-groups/').catch(() => []),
    ]);
    _renderWaves();
  } catch (e) {
    box.innerHTML = `<div class="empty-block"><h4>Couldn't load waves</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

/* ── Editor ───────────────────────────────────────────────────────────── */

function openWaveEditor(waveId) {
  _editingWaveId = waveId || null;
  const w = waveId ? _allWaves.find(x => x.id === waveId) : null;
  document.getElementById('wave-editor-title').textContent = w ? 'Edit Wave' : 'New Wave';
  document.getElementById('wave-name').value = w ? w.name : '';
  // Default a new wave to the end of the order rather than colliding with 1.
  document.getElementById('wave-order').value = w
    ? w.order
    : (_allWaves.reduce((m, x) => Math.max(m, x.order), 0) + 1);
  document.getElementById('wave-validation').value = w ? w.validation_hours : 24;
  document.getElementById('wave-tags').value = w ? (w.tags || []).join(', ') : '';
  document.getElementById('wave-enabled').checked = w ? w.enabled : true;
  const groupSel = document.getElementById('wave-group');
  if (groupSel) {
    const opts = _waveGroups.map(g =>
      `<option value="${escHtml(g.id)}">${escHtml(g.name)}</option>`).join('');
    // A fresh install has no group until the first wave creates one, so offer
    // the default by name rather than showing an empty select.
    groupSel.innerHTML = opts || '<option value="">Default</option>';
    if (w && w.group) groupSel.value = w.group;
  }
  _updateWaveMatchPreview();
  document.getElementById('wave-editor-overlay').classList.add('open');
  document.getElementById('wave-editor-modal').classList.add('open');
}

function closeWaveEditor() {
  document.getElementById('wave-editor-overlay').classList.remove('open');
  document.getElementById('wave-editor-modal').classList.remove('open');
  _editingWaveId = null;
}

function _waveTagsFromInput() {
  return (document.getElementById('wave-tags').value || '')
    .split(',').map(t => t.trim()).filter(Boolean);
}

async function _updateWaveMatchPreview() {
  const el = document.getElementById('wave-match-preview');
  if (!el) return;
  const tags = _waveTagsFromInput();
  if (!tags.length) { el.textContent = ''; return; }
  try {
    const hosts = await apiJson('/api/v1/hosts/');
    const list = Array.isArray(hosts) ? hosts : (hosts.hosts || []);
    const want = new Set(tags.map(t => t.toLowerCase()));
    const n = list.filter(h => (h.tags || []).some(t => want.has((t || '').toLowerCase()))).length;
    el.textContent = n === 0
      ? 'No hosts currently carry these tags.'
      : `${n} host${n === 1 ? ' currently carries' : 's currently carry'} these tags.`;
  } catch {
    el.textContent = '';   // a preview is not worth surfacing an error over
  }
}

async function saveWave() {
  const btn = document.getElementById('wave-save-btn');
  const payload = {
    name: (document.getElementById('wave-name').value || '').trim(),
    order: parseInt(document.getElementById('wave-order').value, 10) || 1,
    validation_hours: parseInt(document.getElementById('wave-validation').value, 10) || 0,
    tags: _waveTagsFromInput(),
    enabled: document.getElementById('wave-enabled').checked,
  };
  const groupId = (document.getElementById('wave-group') || {}).value;
  // Omitted rather than sent null: the model puts a wave with no group into
  // the default one, and a null would defeat the per-group order constraint.
  if (groupId) payload.group = groupId;
  if (!payload.name) { showToast('Give the wave a name', 'error'); return; }
  if (!payload.tags.length) { showToast('A wave needs at least one tag', 'error'); return; }

  btn.disabled = true;
  try {
    const url = _editingWaveId ? `/api/v1/waves/${_editingWaveId}/` : '/api/v1/waves/';
    // apiJson spreads opts straight into fetch, so body must already be a
    // string — passing the object serialises it as "[object Object]".
    await apiJson(url, {
      method: _editingWaveId ? 'PATCH' : 'POST',
      body: JSON.stringify(payload),
    });
    showToast(_editingWaveId ? 'Wave updated' : 'Wave created', 'success');
    closeWaveEditor();
    await loadWaves();
  } catch (e) {
    showToast(e.message || 'Could not save the wave', 'error');
  } finally {
    btn.disabled = false;
  }
}

async function deleteWave(waveId) {
  const w = _allWaves.find(x => x.id === waveId);
  if (!w) return;
  if (!confirm(`Delete wave "${w.name}"? Rollouts already finished keep their history.`)) return;
  try {
    await apiJson(`/api/v1/waves/${waveId}/`, { method: 'DELETE' });
    showToast('Wave deleted', 'success');
    await loadWaves();
  } catch (e) {
    showToast(e.message || 'Could not delete the wave', 'error');
  }
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('wave-search')?.addEventListener('input', _renderWaves);
  document.getElementById('wave-new-btn')?.addEventListener('click', () => openWaveEditor(null));
  document.getElementById('wave-tags')?.addEventListener('input', _updateWaveMatchPreview);


  document.querySelectorAll('.sub-tab[data-subtab]').forEach(tab => {
    tab.addEventListener('click', () => {
      if (tab.dataset.subtab === 'wave-panel') loadWaves();
    });
  });
});


document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('wave-editor-modal')?.classList.contains('open')) {
    closeWaveEditor();
  }
});
