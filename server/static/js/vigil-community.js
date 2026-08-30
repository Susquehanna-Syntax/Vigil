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
/* Whether the Tasks panel is also showing the tasks that exist to serve a
   baseline. Off by default — those are offered by the thing that needs them. */
let communityShowServing = false;

const COMMUNITY_LABEL = { tasks: 'task', baselines: 'baseline', automations: 'automation' };

function _communityEmptyHtml(kind) {
  return `<div class="empty-state">
    <div class="empty-state-title">No community ${escHtml(kind)} yet</div>
    <div class="empty-state-desc">They come from the public
      <a href="https://github.com/Susquehanna-Syntax/Vigil-Approved-Scripts" target="_blank" rel="noopener" style="color:var(--sky);">Vigil-Approved-Scripts</a>
      repo. Be the first to contribute — open one in its editor and use Submit to Community.</div>
  </div>`;
}

/* Which catalog tasks exist only to serve a baseline or an automation.
 *
 * Those are forked along with whatever needs them, so listing them again as
 * standalone entries shows the same thing twice. They stay one click away
 * rather than being hidden outright — a task that serves a baseline is still
 * a perfectly good task to read, or to fork on its own.
 */
function _referencedTaskKeys() {
  const keys = new Set();
  ['baselines', 'automations'].forEach(kind => {
    (communityCache[kind] || []).forEach(item => {
      (item.requires || []).forEach(ref => {
        if (ref.kind !== 'tasks') return;
        if (ref.uid) keys.add('uid:' + ref.uid);
        if (ref.slug) keys.add('slug:' + ref.slug);
      });
    });
  });
  return keys;
}

function _isReferenced(item, keys) {
  const stem = (item.filename || '').replace(/\.(ya?ml)$/, '');
  return (item.uid && keys.has('uid:' + item.uid)) || keys.has('slug:' + stem);
}

/** What forking this item would pull in, rendered as a short list. */
function _requiresHtml(item) {
  const needs = item.requires || [];
  const held = item.have
    ? `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--mint);">Already in your library.</div>`
    : '';
  if (!needs.length) return held;
  if (item.have) return held;
  const rows = needs.map(ref => {
    const held = ref.have === true;
    const mark = held
      ? '<span style="color:var(--mint);">already yours</span>'
      : '<span style="color:var(--sky);">will fork</span>';
    return `<div style="display:flex;justify-content:space-between;gap:10px;">
      <span style="color:var(--text-2);">${escHtml(ref.name || ref.slug)}</span>${mark}</div>`;
  }).join('');
  return `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;">
    <div style="color:var(--text-3);margin-bottom:5px;">Pulls in ${needs.length} item${needs.length === 1 ? '' : 's'}:</div>
    ${rows}
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
        ${_requiresHtml(item)}
      </div>
      <div class="def-card-footer">
        ${github}
        <button class="btn btn-outline btn-sm" onclick="event.stopPropagation(); openCommunityItem(${arg})">${escHtml(_forkLabel(item))}</button>
      </div>
    </div>`;
}

/** "Fork" for a lone task, "Fork all 3" when it drags things along. */
function _forkLabel(item) {
  if (item.have) return 'Already yours';
  const outstanding = (item.requires || []).filter(r => r.have !== true).length;
  return outstanding > 0 ? `Fork all ${outstanding + 1}` : 'Fork';
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

  // Tasks that only exist to serve a baseline are already offered by that
  // baseline's card, so listing them here as well shows the same thing twice.
  // They fold away rather than disappearing — one is still worth reading, and
  // still forkable on its own.
  if (kind === 'tasks') {
    const referenced = _referencedTaskKeys();
    const standalone = visible.filter(i => !_isReferenced(i, referenced));
    const serving = visible.filter(i => _isReferenced(i, referenced));
    if (serving.length && !communityShowServing) {
      grid.innerHTML = standalone.map(_communityCardHtml).join('')
        + `<div class="empty-state" style="grid-column:1/-1;padding:18px;">
             <div class="empty-state-desc">
               ${serving.length} more task${serving.length === 1 ? ' is' : 's are'} used by a baseline or automation,
               and come${serving.length === 1 ? 's' : ''} along when you fork it.
               <button class="btn btn-ghost btn-sm" style="margin-left:8px;"
                       onclick="toggleCommunityServing()">Show ${serving.length === 1 ? 'it' : 'them'}</button>
             </div></div>`;
      return;
    }
    if (serving.length) {
      grid.innerHTML = standalone.concat(serving).map(_communityCardHtml).join('')
        + `<div class="empty-state" style="grid-column:1/-1;padding:14px;">
             <div class="empty-state-desc">
               Showing tasks used by baselines.
               <button class="btn btn-ghost btn-sm" style="margin-left:8px;"
                       onclick="toggleCommunityServing()">Hide them</button>
             </div></div>`;
      return;
    }
  }
  grid.innerHTML = visible.map(_communityCardHtml).join('');
}

function toggleCommunityServing() {
  communityShowServing = !communityShowServing;
  _renderCommunity('tasks');
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

  // The Tasks panel cannot tell which tasks serve a baseline without the other
  // two catalogs, and they are cached server-side, so pulling them is cheap.
  if (kind === 'tasks') {
    Promise.all(['baselines', 'automations'].map(k => loadCommunityKind(k)))
      .then(() => _renderCommunity('tasks'));
  }
  _annotateFork(kind);
}

/* Ask the server what each item would pull in, and mark what you already have.
 *
 * The same walk backs this and the fork itself, so the card's promise and the
 * button's behaviour cannot drift apart. One request per card, only for the
 * kinds that reference anything. */
async function _annotateFork(kind) {
  if (kind === 'tasks') return;
  const items = communityCache[kind] || [];
  await Promise.all(items.map(async item => {
    if (item._planned) return;
    try {
      const plan = await apiJson(
        `/api/v1/tasks/community/${kind}/${encodeURIComponent(item.filename)}/plan/`);
      item.requires = plan.needs || [];
      item.have = plan.have === true;
      item._planned = true;
    } catch {
      // Leave the card as it is; Fork still works and reports its own errors.
      item._planned = true;
    }
  }));
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
    // Self-contained and nothing to resolve: straight into the editor,
    // unsaved, so it can be read before it is kept.
    openDefinitionEditor(null, item.yaml_source);
    showToast('Community task opened — save it to add it to your library', 'success');
    return;
  }
  // A baseline is a sequence of tasks and an automation runs a task or a
  // baseline, so forking one alone would land you with something that cannot
  // run. The server walks the graph and forks only what is genuinely absent.
  try {
    const result = await apiJson(
      `/api/v1/tasks/community/${kind}/${encodeURIComponent(item.filename)}/fork/`,
      { method: 'POST', body: JSON.stringify({}) });
    if (result.already) {
      showToast(`You already have “${item.name}”`, 'success');
      item.have = true;
      _renderCommunity(kind);
      return;
    }
    const made = result.created || [];
    const extra = made.length - 1;
    showToast(
      extra > 0
        ? `Forked “${item.name}” and the ${extra} item${extra === 1 ? '' : 's'} it needs`
        : `Forked “${item.name}” into your library`,
      'success');
    navigateTo('baselines');
    if (typeof loadBaselines === 'function') loadBaselines();
    if (typeof loadAutomations === 'function') loadAutomations();
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
