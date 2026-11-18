// @ts-check
/* Wave management — the Deployments › Waves panel.
 *
 * Built to the same shape as playbooks and automations: a toolbar with search
 * and a New button, a list of cards, and a modal editor. Nothing here edits a
 * host's tags. A wave only chooses which tags it matches; tags stay owned by
 * the host, so there is exactly one place a host's membership is decided.
 */

/** @type {any[]} */
let _allWaves = [];
/** @type {any[]} */
let _waveGroups = [];
/** @type {any} */
let _editingWaveId = null;

/* ── List ─────────────────────────────────────────────────────────────── */

function _filterWaves() {
  const q = (/** @type {HTMLInputElement} */ (document.getElementById('wave-search'))?.value || '').trim().toLowerCase();
  if (!q) return _allWaves;
  return _allWaves.filter(w =>
    (w.name || '').toLowerCase().includes(q) ||
    (w.tags || []).some(t => (t || '').toLowerCase().includes(q)));
}

/** @param {any} waveRow @returns {string} */
function _waveCard(waveRow) {
  const tags = (waveRow.tags || []).length
    ? (waveRow.tags || []).map(tg => `<span class="chip">${escHtml(tg)}</span>`).join(' ')
    : `<span class="rose-note">${escHtml(t('no tags — matches nothing'))}</span>`;

  // Matching vs actually-patched. They differ when an earlier wave already
  // claimed a host, and that difference is the thing operators trip over.
  const overlap = waveRow.host_count - waveRow.exclusive_host_count;
  const hosts = waveRow.host_count === 0
    ? `<span class="muted-span">${escHtml(t('no hosts match'))}</span>`
    : tn('{n} host', '{n} hosts', waveRow.exclusive_host_count) +
      (overlap > 0
        ? ` <span class="muted-span">(${overlap} also in an earlier wave)</span>`
        : '');

  return `<div class="bl-card">
    <div class="bl-card-head">
      <div>
        <span class="bl-name">${escHtml(waveRow.order)} &middot; ${escHtml(waveRow.name)}</span>
        <span class="bl-badge ${waveRow.enabled ? 'on' : 'off'}">${waveRow.enabled ? 'enabled' : 'off'}</span>
        ${(waveRow.group_tags || []).map(tg => `<span class="chip">${escHtml(tg)}</span>`).join(' ')}
      </div>
      <div class="wave-card-actions">
        <button class="btn btn-sky btn-sm" data-wave-edit="${escAttr(waveRow.id)}">${escHtml(t('Edit'))}</button>
        <button class="btn btn-ghost btn-sm delete-btn" data-wave-del="${escAttr(waveRow.id)}">${escHtml(t('Delete'))}</button>
      </div>
    </div>
    <div class="bl-card-body card-body-lines">
      <div>${hosts} &middot; validation window ${escHtml(waveRow.validation_hours)}h</div>
      <div class="wave-card-tags">${tags}</div>
    </div>
  </div>`;
}

/** @returns {void} */
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

/** @returns {Promise<void>} */
async function loadWaves() {
  const box = document.getElementById('waves-list');
  if (!box) return;
  box.innerHTML = `<div class="empty-block"><p>${escHtml(t('Loading…'))}</p></div>`;
  try {
    const [waves, groups] = await Promise.all([
      apiJson('/api/v1/waves/'),
      apiJson('/api/v1/wave-groups/').catch(() => ({ groups: [] })),
    ]);
    _allWaves = waves;
    _waveGroups = (groups && groups.groups) || [];
    _renderWaves();
  } catch (e) {
    box.innerHTML = `<div class="empty-block"><h4>${escHtml(t("Couldn't load waves"))}</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

/* ── Editor ───────────────────────────────────────────────────────────── */

/** @param {any=} waveId @returns {void} */
function openWaveEditor(waveId) {
  _editingWaveId = waveId || null;
  const w = waveId ? _allWaves.find(x => x.id === waveId) : null;
  document.getElementById('wave-editor-title').textContent = w ? 'Edit Wave' : 'New Wave';
  /** @type {HTMLInputElement} */ (document.getElementById('wave-name')).value = w ? w.name : '';
  // Default a new wave to the end of the order rather than colliding with 1.
  /** @type {HTMLInputElement} */ (document.getElementById('wave-order')).value = w
    ? w.order
    : (_allWaves.reduce((m, x) => Math.max(m, x.order), 0) + 1);
  /** @type {HTMLInputElement} */ (document.getElementById('wave-validation')).value = w ? w.validation_hours : 24;
  /** @type {HTMLInputElement} */ (document.getElementById('wave-tags')).value = w ? (w.tags || []).join(', ') : '';
  /** @type {HTMLInputElement} */ (document.getElementById('wave-enabled')).checked = w ? w.enabled : true;
  const groupInput = /** @type {HTMLInputElement} */ (document.getElementById('wave-group-tags'));
  if (groupInput) groupInput.value = w ? (w.group_tags || []).join(', ') : '';
  const siteSel = /** @type {HTMLSelectElement} */ (document.getElementById('wave-site'));
  sitePickFill(siteSel, document.getElementById('wave-site-field'), w ? w.site_id : null,
    { editing: !!w }).then(() => { siteSel.disabled = !!w; });
  _updateWaveMatchPreview();
  document.getElementById('wave-editor-overlay').classList.add('open');
  document.getElementById('wave-editor-modal').classList.add('open');
}

/** @returns {void} */
function closeWaveEditor() {
  document.getElementById('wave-editor-overlay').classList.remove('open');
  document.getElementById('wave-editor-modal').classList.remove('open');
  _editingWaveId = null;
}

/** @returns {string[]} */
function _waveTagsFromInput() {
  return (/** @type {HTMLInputElement} */ (document.getElementById('wave-tags')).value || '')
    .split(',').map(t => t.trim()).filter(Boolean);
}

/** @returns {Promise<void>} */
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

/** @returns {Promise<void>} */
async function saveWave() {
  const btn = /** @type {HTMLButtonElement} */ (document.getElementById('wave-save-btn'));
  /** @type {{name: string, order: number, validation_hours: number, tags: string[], enabled: boolean, group_tags?: string[], site_id?: string|number|null}} */
  const payload = {
    name: (/** @type {HTMLInputElement} */ (document.getElementById('wave-name')).value || '').trim(),
    order: parseInt(/** @type {HTMLInputElement} */ (document.getElementById('wave-order')).value, 10) || 1,
    validation_hours: parseInt(/** @type {HTMLInputElement} */ (document.getElementById('wave-validation')).value, 10) || 0,
    tags: _waveTagsFromInput(),
    enabled: /** @type {HTMLInputElement} */ (document.getElementById('wave-enabled')).checked,
  };
  payload.group_tags = ((/** @type {HTMLInputElement} */ (document.getElementById('wave-group-tags')) || {}).value || '')
    .split(',').map(t => t.trim()).filter(Boolean);
  if (!_editingWaveId) {
    const siteId = sitePickValue(/** @type {HTMLSelectElement} */ (document.getElementById('wave-site')), document.getElementById('wave-site-field'));
    if (siteId !== undefined) payload.site_id = siteId;
  }
  if (!payload.name) { showToast(t('Give the wave a name'), 'error'); return; }
  if (!payload.tags.length) { showToast(t('A wave needs at least one tag'), 'error'); return; }

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

/** @param {any=} waveId @returns {Promise<void>} */
async function deleteWave(waveId) {
  const w = _allWaves.find(x => x.id === waveId);
  if (!w) return;
  if (!confirm(`Delete wave "${w.name}"? Rollouts already finished keep their history.`)) return;
  try {
    await apiJson(`/api/v1/waves/${waveId}/`, { method: 'DELETE' });
    showToast(t('Wave deleted'), 'success');
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


  document.querySelectorAll('.sub-tab[data-subtab]').forEach((/** @type {HTMLElement} */ tab) => {
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


/* Wave row buttons. The ids are server integers, so Number() keeps the
   argument the same type the inline attribute passed. */
delegateClick('[data-wave-edit]', (el) => openWaveEditor(Number(el.dataset.waveEdit)));
delegateClick('[data-wave-del]', (el) => deleteWave(Number(el.dataset.waveDel)));
