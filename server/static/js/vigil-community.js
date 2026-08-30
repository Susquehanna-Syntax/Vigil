/* The Community page — tasks, baselines and automations from the public repo.
 *
 * Each kind is fetched and cached independently (server-side, ten minutes) so
 * a slow or empty directory does not hold up the others, and each panel keeps
 * its own search so switching tabs does not carry a stale filter across.
 *
 * Forking is where the three kinds differ. A task is self-contained: its YAML
 * opens in the editor and saving adds it. A baseline names its tasks by slug
 * and an automation names the task or baseline it runs the same way, so those
 * two are imported server-side, which resolves the names against your own
 * library. When something is missing the import refuses and says which items
 * to fork first — a baseline whose steps quietly vanished would be worse than
 * a refusal.
 */

const COMMUNITY_KINDS = ['tasks', 'baselines', 'automations'];
const communityCache = { tasks: [], baselines: [], automations: [] };
const communityLoaded = { tasks: false, baselines: false, automations: false };
let communityKind = 'tasks';

const COMMUNITY_LABEL = { tasks: 'task', baselines: 'baseline', automations: 'automation' };

function _communityEmptyHtml(kind) {
  return `<div class="empty-state">
    <div class="empty-state-title">No community ${escHtml(kind)} yet</div>
    <div class="empty-state-desc">They come from the public
      <a href="https://github.com/Susquehanna-Syntax/Vigil-Approved-Scripts" target="_blank" rel="noopener" style="color:var(--sky);">Vigil-Approved-Scripts</a>
      repo. Be the first to contribute — open one in its editor and use Submit to Community.</div>
  </div>`;
}

function _communityCardHtml(item) {
  const meta = [];
  if (item.kind === 'tasks') {
    const actions = (item.parsed_spec?.actions || []).length;
    meta.push(`${actions} action${actions === 1 ? '' : 's'}`);
  } else if (item.summary) {
    meta.push(escHtml(item.summary));
  }
  if (item.relevance) meta.push(escHtml(item.relevance));
  if (item.author) meta.push(`by ${escHtml(item.author)}`);

  const github = item.html_url
    ? `<a class="btn btn-ghost btn-sm" style="color:var(--text-3);text-decoration:none;" href="${escHtml(item.html_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">View on GitHub</a>`
    : '';
  const arg = `'${item.kind}','${encodeURIComponent(item.filename)}'`;
  return `
    <div class="def-card" onclick="openCommunityItem(${arg})">
      <div class="def-card-body">
        <div class="def-card-title">${escHtml(item.name || 'Untitled')}</div>
        <div class="def-card-desc">${escHtml(item.description || 'No description provided.')}</div>
        <div class="def-card-meta">
          ${riskBadgeHtml(item.risk_level || 'standard')}
          ${meta.map(m => `<span class="dot-sep">·</span><span>${m}</span>`).join('')}
        </div>
      </div>
      <div class="def-card-footer">
        ${github}
        <button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); openCommunityItem(${arg})">Fork</button>
      </div>
    </div>`;
}

function _renderCommunity(kind) {
  const grid = document.getElementById(`community-${kind}-grid`);
  if (!grid) return;
  const all = communityCache[kind] || [];
  if (!all.length) {
    grid.innerHTML = communityLoaded[kind]
      ? _communityEmptyHtml(kind)
      : '<div class="empty-state"><div class="empty-state-desc">Loading…</div></div>';
    return;
  }
  const q = (document.getElementById(`community-${kind}-search`)?.value || '')
    .trim().toLowerCase();
  let visible = all;
  if (q) {
    visible = all.filter(item => [
      item.name, item.description, item.relevance, item.author, item.summary,
      ...(item.requires || []),
      ...((item.parsed_spec?.actions || []).map(a => `${a.type} ${a.label || ''}`)),
    ].filter(Boolean).join(' ').toLowerCase().includes(q));
  }
  if (!visible.length) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-state-title">No matches</div>
      <div class="empty-state-desc">Nothing in ${escHtml(kind)} matches “${escHtml(q)}”.</div></div>`;
    return;
  }
  grid.innerHTML = visible.map(_communityCardHtml).join('');
}

async function loadCommunityKind(kind, force) {
  if (communityLoaded[kind] && !force) return;
  const grid = document.getElementById(`community-${kind}-grid`);
  if (grid && !communityLoaded[kind]) {
    grid.innerHTML = '<div class="empty-state"><div class="empty-state-desc">Loading…</div></div>';
  }
  try {
    const url = `/api/v1/tasks/community/${kind}/${force ? '?refresh=1' : ''}`;
    communityCache[kind] = await apiJson(url);
    communityLoaded[kind] = true;
  } catch (e) {
    communityLoaded[kind] = true;
    communityCache[kind] = [];
    if (grid) {
      grid.innerHTML = `<div class="empty-state">
        <div class="empty-state-title">Couldn't reach the community repo</div>
        <div class="empty-state-desc">${escHtml(e.message)}</div></div>`;
      return;
    }
  }
  _renderCommunity(kind);
}

/** Refresh the visible panel. With `force`, bypasses the server-side cache. */
function refreshCommunity(force) {
  loadCommunityKind(communityKind, force === true);
}

function _communityItem(kind, encodedFilename) {
  const filename = decodeURIComponent(encodedFilename);
  return (communityCache[kind] || []).find(i => i.filename === filename);
}

/** Fork one item into the operator's own library. */
async function openCommunityItem(kind, encodedFilename) {
  const item = _communityItem(kind, encodedFilename);
  if (!item || !item.yaml_source) {
    showToast('Not loaded — hit Refresh and try again', 'error');
    return;
  }
  if (kind === 'tasks') {
    // Self-contained: straight into the editor, unsaved.
    openDefinitionEditor(null, item.yaml_source);
    showToast('Community task opened — save it to add it to your library', 'success');
    return;
  }
  // Baselines and automations reference other content by slug, so the server
  // resolves them. It answers 400 naming anything missing.
  const endpoint = kind === 'baselines' ? '/api/v1/baselines/yaml/'
                                        : '/api/v1/automations/yaml/';
  try {
    await apiJson(endpoint, {
      method: 'POST', body: JSON.stringify({ yaml: item.yaml_source }),
    });
    showToast(`Forked “${item.name}” into your library`, 'success');
    navigateTo('baselines');
    if (kind === 'baselines' && typeof loadBaselines === 'function') loadBaselines();
    if (kind === 'automations' && typeof loadAutomations === 'function') loadAutomations();
  } catch (e) {
    showToast(e.message || `Could not fork this ${COMMUNITY_LABEL[kind]}`, 'error');
  }
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#page-community .sub-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#page-community .sub-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('#page-community .sub-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.subtab).classList.add('active');
      communityKind = tab.dataset.kind;
      loadCommunityKind(communityKind);
    });
  });
  COMMUNITY_KINDS.forEach(kind => {
    document.getElementById(`community-${kind}-search`)
      ?.addEventListener('input', () => _renderCommunity(kind));
  });
});

/* Load the visible panel on navigation. The other two stay unfetched until
   their tab is opened — three GitHub round-trips for a tab nobody looked at
   is three round-trips wasted. */
if (typeof navigateTo === 'function') {
  const _origNavCommunity = navigateTo;
  navigateTo = function (page) {
    _origNavCommunity(page);
    if (page === 'community') loadCommunityKind(communityKind);
  };
}
