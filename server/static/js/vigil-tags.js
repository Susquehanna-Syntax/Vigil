/* Tags — the fleet-wide list, and the picker every tag field uses.
 *
 * A tag is a row now, not a string typed in four different places. That makes
 * two things possible that were not before: a picker can show you what already
 * exists (so a typo is visible instead of silently matching nothing), and a
 * rename lands everywhere at once.
 *
 * Reserved namespaces — os:, os_family:, pkg:, arch:, agent:* — are set by
 * Vigil from what each machine reports. They are shown but not editable; an
 * operator editing one would just be overwritten by the next check-in.
 */

let _allTags = [];

/* ── The Tags tab ─────────────────────────────────────────────────────── */

function _filterTags() {
  const q = (document.getElementById('tag-search')?.value || '').trim().toLowerCase();
  if (!q) return _allTags;
  return _allTags.filter(t =>
    (t.name || '').toLowerCase().includes(q) ||
    (t.description || '').toLowerCase().includes(q));
}

function _tagKindBadge(kind) {
  if (kind === 'auto') return '<span class="bl-badge off">from inventory</span>';
  if (kind === 'agent') return '<span class="bl-badge off">reported by agent</span>';
  return '';
}

function _tagCard(t) {
  const used = t.host_count === 0
    ? '<span style="color:var(--text-3);">on no machines</span>'
    : `on ${t.host_count} machine${t.host_count === 1 ? '' : 's'}`;
  const actions = t.editable
    ? `<button class="btn btn-outline btn-sm" onclick="renameTag(${t.id})">Rename</button>
       <button class="btn btn-ghost btn-sm" style="color:var(--rose);" onclick="deleteTag(${t.id})">Delete</button>`
    : '<span class="confirm-hint" style="margin:0;">maintained by Vigil</span>';
  return `<div class="bl-card">
    <div class="bl-card-head">
      <div>
        <span class="bl-name">${escHtml(t.name)}</span>
        ${_tagKindBadge(t.kind)}
      </div>
      <div style="display:flex;gap:6px;align-items:center;">${actions}</div>
    </div>
    <div class="bl-card-body" style="color:var(--text-2);font-size:12px;">
      ${used}${t.description ? ' · ' + escHtml(t.description) : ''}
    </div>
  </div>`;
}

function _renderTags() {
  const box = document.getElementById('tags-list');
  if (!box) return;
  const items = _filterTags();
  if (!items.length) {
    const searching = _allTags.length > 0;
    box.innerHTML = `<div class="empty-block">
      <h4>${searching ? 'No tags match that search' : 'No tags yet'}</h4>
      <p>${searching ? 'Clear the search to see them all.'
        : 'Tags are how waves, playbooks and automations pick machines. Create one here, then put it on a machine from its host card.'}</p>
    </div>`;
    return;
  }
  box.innerHTML = items.map(_tagCard).join('');
}

async function loadTags() {
  const box = document.getElementById('tags-list');
  if (!box) return;
  box.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    _allTags = await apiJson('/api/v1/tags/');
    _renderTags();
  } catch (e) {
    box.innerHTML = `<div class="empty-block"><h4>Couldn't load tags</h4><p>${escHtml(e.message)}</p></div>`;
  }
}

async function createTag() {
  const name = prompt('Name for the new tag:');
  if (!name || !name.trim()) return;
  try {
    await apiJson('/api/v1/tags/', {
      method: 'POST', body: JSON.stringify({ name: name.trim() }),
    });
    showToast('Tag created', 'success');
    await loadTags();
  } catch (e) {
    showToast(e.message || 'Could not create the tag', 'error');
  }
}

async function renameTag(tagId) {
  const tag = _allTags.find(t => t.id === tagId);
  if (!tag) return;
  const name = prompt(
    `Rename "${tag.name}". This renames it on every machine, wave, playbook and ` +
    `automation that uses it.`, tag.name);
  if (!name || name.trim() === tag.name) return;
  try {
    await apiJson(`/api/v1/tags/${tagId}/`, {
      method: 'PATCH', body: JSON.stringify({ name: name.trim() }),
    });
    showToast('Tag renamed everywhere', 'success');
    await loadTags();
  } catch (e) {
    showToast(e.message || 'Could not rename the tag', 'error');
  }
}

async function deleteTag(tagId) {
  const tag = _allTags.find(t => t.id === tagId);
  if (!tag) return;
  if (!confirm(`Delete the tag "${tag.name}"?`)) return;
  try {
    await apiJson(`/api/v1/tags/${tagId}/`, { method: 'DELETE' });
    showToast('Tag deleted', 'success');
    await loadTags();
  } catch (e) {
    // A 409 here is the API refusing to empty a wave behind your back; its
    // message names what is still using the tag.
    showToast(e.message || 'Could not delete the tag', 'error');
  }
}

/* ── The tag picker, used by every tag field ──────────────────────────── */

/**
 * Open a searchable list of existing tags.
 *
 * `onSelect(tag)` receives the chosen tag. Typing a name that does not exist
 * offers to create it — so a typo is a visible decision rather than a silent
 * non-match, which is the whole reason tags became rows.
 */
function openTagPicker(opts) {
  const chosen = new Set((opts.selected || []).map(n => String(n).toLowerCase()));
  openPicker({
    type: 'tag',
    title: opts.title || 'Pick a tag',
    allowAdd: false,
    items: _allTags
      .filter(t => opts.includeReserved ? true : t.editable)
      .map(t => ({
        key: t.name,
        name: t.name + (chosen.has(t.key) ? '  ✓' : ''),
        meta: (t.host_count === 1 ? '1 machine' : `${t.host_count} machines`)
              + (t.kind !== 'manual' ? ' · from inventory' : ''),
        editable: false,
        raw: t,
      })),
    onSelect: (item) => opts.onSelect(item.raw || { name: item.key }),
  });
}

/** Ensure `_allTags` is loaded before a picker opens. */
async function ensureTagsLoaded() {
  if (_allTags.length) return;
  try { _allTags = await apiJson('/api/v1/tags/'); } catch { _allTags = []; }
}

/**
 * Give a comma-separated tag input a "Pick…" button beside it.
 *
 * Every tag field in the app goes through here, so they all behave the same:
 * the picker shows what already exists with machine counts, and picking
 * appends rather than replaces. Typed entry still works — a tag that does not
 * exist yet has to be typeable somewhere — but the list is now one click away,
 * so a misspelling is a choice rather than an accident.
 */
function attachTagPicker(inputId, opts = {}) {
  const input = document.getElementById(inputId);
  if (!input || input.dataset.tagPicker) return;
  input.dataset.tagPicker = '1';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-outline btn-sm tag-pick-btn';
  btn.textContent = 'Pick…';
  input.insertAdjacentElement('afterend', btn);

  btn.addEventListener('click', async () => {
    await ensureTagsLoaded();
    const current = (input.value || '').split(',').map(t => t.trim()).filter(Boolean);
    openTagPicker({
      title: opts.title || 'Add a tag',
      selected: current,
      // Reprovision and host tagging can legitimately reference an
      // inventory-derived tag; a wave selector normally should not.
      includeReserved: opts.includeReserved === true,
      onSelect: (tag) => {
        if (!current.some(t => t.toLowerCase() === String(tag.name).toLowerCase())) {
          current.push(tag.name);
        }
        input.value = current.join(', ');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      },
    });
  });
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('tag-search')?.addEventListener('input', _renderTags);
  document.getElementById('tag-new-btn')?.addEventListener('click', createTag);
  document.querySelectorAll('.tab[data-tab]').forEach(tab => {
    tab.addEventListener('click', () => {
      if (tab.dataset.tab === 'tasks-tags') loadTags();
    });
  });

  // Every tag field in the app, in one place so none gets forgotten.
  attachTagPicker('wave-tags', { title: 'Machines in this wave are tagged' });
  attachTagPicker('bl-tags', { title: 'Machines this playbook targets' });
  attachTagPicker('auto-event-tags', { title: 'Only fire for hosts tagged' });
  attachTagPicker('auto-target-tags', { title: 'Run on hosts tagged' });
  attachTagPicker('repro-prof-tags', { title: 'Tag the rebuilt host with',
                                       includeReserved: true });
  attachTagPicker('detail-tag-input', { title: 'Add a tag to this machine' });
});
