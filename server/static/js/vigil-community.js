// @ts-check
/* The Community page — tasks, playbooks and automations from the public repo.
 *
 * Each kind is fetched and cached independently (server-side, ten minutes) so
 * a slow or empty directory does not hold up the others, and each panel keeps
 * its own search so switching tabs does not carry a stale filter across.
 *
 * Forking is where the three kinds differ. A task is self-contained: its YAML
 * opens in the editor and saving adds it. A playbook names its tasks by slug
 * and an automation names the task or playbook it runs the same way, so those
 * two are imported server-side, which resolves the names against your own
 * library. When something is missing the import refuses and says which items
 * to fork first — a playbook whose steps quietly vanished would be worse than
 * a refusal.
 */

const COMMUNITY_KINDS = ['tasks', 'playbooks', 'automations'];
/** @type {Record<string, CommunityItem[]>} */
const communityCache = { tasks: [], playbooks: [], automations: [] };
const communityLoaded = { tasks: false, playbooks: false, automations: false };
let communityKind = 'tasks';
/* Whether the Tasks panel is also showing the tasks that exist to serve a
   playbook. Off by default — those are offered by the thing that needs them. */
let communityShowServing = false;

/** The catalog noun for a kind — one word, so a translation may change its
 * case or replace the whole phrase around it. */
/** @param {string} kind @returns {string} */
function _communityKindWord(kind) {
  if (kind === 'tasks') return t('task');
  if (kind === 'playbooks') return t('playbook');
  if (kind === 'automations') return t('automation');
  return kind;
}

/** The plural catalog noun for a kind, for "No community tasks yet". */
/** @param {string} kind @returns {string} */
function _communityKindPlural(kind) {
  if (kind === 'tasks') return t('tasks');
  if (kind === 'playbooks') return t('playbooks');
  if (kind === 'automations') return t('automations');
  return kind;
}

/** One catalog entry from /api/v1/tasks/community/<kind>/. */
/** @typedef {{kind?: string, filename?: string, name?: string, description?: string, summary?: string,
 *  relevance?: string, author?: string, html_url?: string, risk_level?: string, uid?: string,
 *  yaml_source?: string, have?: boolean, _planned?: boolean, requires?: Array<{kind?: string, uid?: string, slug?: string, name?: string, have?: boolean}>,
 *  parsed_spec?: {actions?: Array<{type?: string, label?: string}>}, [k: string]: any}} CommunityItem */

/** @param {string} kind @returns {string} */
function _communityEmptyHtml(kind) {
  const repo = `<a class="community-catalog-link" href="https://github.com/Susquehanna-Syntax/Vigil-Approved-Scripts" target="_blank" rel="noopener">Vigil-Approved-Scripts</a>`; // i18n-ignore
  return `<div class="empty-state">
    <div class="empty-state-title">${escHtml(t('No community {kind} yet', { kind: _communityKindPlural(kind) }))}</div>
    <div class="empty-state-desc">${t('They come from the public {repo} repo. Be the first to contribute — open one in its editor and use Submit to Community.', { repo })}</div>
  </div>`;
}

/* Which catalog tasks exist only to serve a playbook or an automation.
 *
 * Those are forked along with whatever needs them, so listing them again as
 * standalone entries shows the same thing twice. They stay one click away
 * rather than being hidden outright — a task that serves a playbook is still
 * a perfectly good task to read, or to fork on its own.
 */
/** @returns {Set<string>} */
function _referencedTaskKeys() {
  const keys = new Set();
  ['playbooks', 'automations'].forEach(kind => {
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

/** @param {CommunityItem} item @param {Set<string>} keys @returns {boolean} */
function _isReferenced(item, keys) {
  const stem = (item.filename || '').replace(/\.(ya?ml)$/, '');
  return (item.uid && keys.has('uid:' + item.uid)) || keys.has('slug:' + stem);
}

/** What forking this item would pull in, rendered as a short list. */
/** @param {CommunityItem} item @returns {string} */
function _requiresHtml(item) {
  const needs = item.requires || [];
  const held = item.have
    ? `<div class="community-requires community-requires-held">${escHtml(t('Already in your library.'))}</div>`
    : '';
  if (!needs.length) return held;
  if (item.have) return held;
  const rows = needs.map(ref => {
    const held = ref.have === true;
    const mark = held
      ? `<span class="community-require-held">${escHtml(t('already yours'))}</span>`
      : `<span class="community-require-will">${escHtml(t('will fork'))}</span>`;
    return `<div class="community-require-row">
      <span class="community-require-name">${escHtml(ref.name || ref.slug)}</span>${mark}</div>`;
  }).join('');
  return `<div class="community-requires">
    <div class="community-require-count">${escHtml(tn('Pulls in {n} item:', 'Pulls in {n} items:', needs.length))}</div>
    ${rows}
  </div>`;
}

/** @param {CommunityItem} item @returns {string} */
function _communityCardHtml(item) {
  const meta = [];
  if (item.kind === 'tasks') {
    const actions = (item.parsed_spec?.actions || []).length;
    meta.push(escHtml(tn('{n} action', '{n} actions', actions)));
  } else if (item.summary) {
    // This list is spliced into markup as-is, so every entry escapes itself.
    meta.push(escHtml(item.summary));
  }
  if (item.relevance) meta.push(escHtml(item.relevance));
  if (item.author) meta.push(escHtml(t('by {author}', { author: item.author })));

  const github = item.html_url
    ? `<a class="btn btn-ghost btn-sm community-github-link" href="${escAttr(item.html_url)}" target="_blank" rel="noopener" data-stop>${escHtml(t('View on GitHub'))}</a>`
    : '';
  // Was built as a JS argument string: `'${item.kind}','${encodeURIComponent(
  // item.filename)}'` spliced into an onclick. encodeURIComponent is the wrong
  // escaping for a JS string literal — it leaves ' untouched — so a community
  // filename containing an apostrophe broke out of the quotes. Data attributes
  // have no such context to get wrong.
  return `
    <div class="def-card" data-comm-kind="${escAttr(item.kind)}" data-comm-file="${escAttr(item.filename)}">
      <div class="def-card-body">
        <div class="def-card-title">${escHtml(item.name || t('Untitled'))}</div>
        <div class="def-card-desc">${escHtml(item.description || t('No description provided.'))}</div>
        <div class="def-card-meta">
          ${riskBadgeHtml(item.risk_level || 'standard')}
          ${meta.map(m => `<span class="dot-sep">·</span><span>${m}</span>`).join('')}
        </div>
        ${_requiresHtml(item)}
      </div>
      <div class="def-card-footer">
        ${github}
        <button class="btn btn-mint btn-sm" data-comm-open data-comm-kind="${escAttr(item.kind)}" data-comm-file="${escAttr(item.filename)}" data-stop>${escHtml(_forkLabel(item))}</button>
      </div>
    </div>`;
}

/** "Fork" for a lone task, "Fork all 3" when it drags things along. */
/** @param {CommunityItem} item @returns {string} */
function _forkLabel(item) {
  if (item.have) return t('Already yours');
  const outstanding = (item.requires || []).filter(r => r.have !== true).length;
  return outstanding > 0 ? t('Fork all {n}', { n: outstanding + 1 }) : t('Fork');
}

/** @param {string} kind @returns {void} */
function _renderCommunity(kind) {
  const grid = document.getElementById(`community-${kind}-grid`);
  if (!grid) return;
  const all = communityCache[kind] || [];
  if (!all.length) {
    grid.innerHTML = communityLoaded[kind]
      ? _communityEmptyHtml(kind)
      : loadingHtml();
    return;
  }
  const q = (/** @type {HTMLInputElement} */ (document.getElementById(`community-${kind}-search`))?.value || '')
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
    grid.innerHTML = `<div class="empty-state"><div class="empty-state-title">${escHtml(t('No matches'))}</div>
      <div class="empty-state-desc">${escHtml(t('Nothing in {kind} matches “{query}”.', { kind: _communityKindPlural(kind), query: q }))}</div></div>`;
    return;
  }

  // Tasks that only exist to serve a playbook are already offered by that
  // playbook's card, so listing them here as well shows the same thing twice.
  // They fold away rather than disappearing — one is still worth reading, and
  // still forkable on its own.
  if (kind === 'tasks') {
    const referenced = _referencedTaskKeys();
    const standalone = visible.filter(i => !_isReferenced(i, referenced));
    const serving = visible.filter(i => _isReferenced(i, referenced));
    if (serving.length && !communityShowServing) {
      grid.innerHTML = standalone.map(_communityCardHtml).join('')
        + `<div class="empty-state community-grid-banner">
             <div class="empty-state-desc">
               ${escHtml(tn('{n} more task is used by a playbook or automation, and comes along when you fork it.', '{n} more tasks are used by a playbook or automation, and come along when you fork it.', serving.length))}
               <button class="btn btn-ghost btn-sm community-serving-toggle"
                       data-comm-serving>${escHtml(serving.length === 1 ? t('Show it') : t('Show them'))}</button>
             </div></div>`;
      return;
    }
    if (serving.length) {
      grid.innerHTML = standalone.concat(serving).map(_communityCardHtml).join('')
        + `<div class="empty-state community-grid-banner-tight">
             <div class="empty-state-desc">
               ${escHtml(t('Showing tasks used by playbooks.'))}
               <button class="btn btn-ghost btn-sm community-serving-toggle"
                       data-comm-serving>${escHtml(t('Hide them'))}</button>
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

/** @param {string} kind @param {boolean=} force @returns {Promise<void>} */
async function loadCommunityKind(kind, force) {
  if (communityLoaded[kind] && !force) return;
  const grid = document.getElementById(`community-${kind}-grid`);
  if (grid && !communityLoaded[kind]) {
    grid.innerHTML = loadingHtml();
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
        <div class="empty-state-title">${escHtml(t("Couldn't reach the community repo"))}</div>
        <div class="empty-state-desc">${escHtml(e.message)}</div></div>`;
      return;
    }
  }
  _renderCommunity(kind);

  // The Tasks panel cannot tell which tasks serve a playbook without the other
  // two catalogs, and they are cached server-side, so pulling them is cheap.
  if (kind === 'tasks') {
    Promise.all(['playbooks', 'automations'].map(k => loadCommunityKind(k)))
      .then(() => _renderCommunity('tasks'));
  }
  _annotateFork(kind);
}

/* Ask the server what each item would pull in, and mark what you already have.
 *
 * The same walk backs this and the fork itself, so the card's promise and the
 * button's behaviour cannot drift apart. One request per card, only for the
 * kinds that reference anything. */
/** @param {string} kind @returns {Promise<void>} */
async function _annotateFork(kind) {
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
      // state-ok: the fork plan is a convenience; the card still renders and Fork reports its own errors.
      item._planned = true;
    }
  }));
  _renderCommunity(kind);
}

/** Refresh the visible panel. With `force`, bypasses the server-side cache. */
function refreshCommunity(force) {
  loadCommunityKind(communityKind, force === true);
}

/* Forget every cached fork plan.
 *
 * The plans say "already yours" or "will fork" per item, and a single fork can
 * change that answer on cards the operator never touched. Without this the
 * card they just forked keeps offering itself. */
function _invalidateForkPlans() {
  COMMUNITY_KINDS.forEach(k => (communityCache[k] || []).forEach(i => {
    i._planned = false;
  }));
  COMMUNITY_KINDS.forEach(k => {
    if (communityLoaded[k]) _annotateFork(k);
  });
}

/** @param {string} kind @param {string} encodedFilename @returns {CommunityItem|undefined} */
function _communityItem(kind, encodedFilename) {
  const filename = decodeURIComponent(encodedFilename);
  return (communityCache[kind] || []).find(i => i.filename === filename);
}

/** Fork one item into the operator's own library. */
/** @param {string} kind @param {string} encodedFilename @returns {Promise<void>} */
async function openCommunityItem(kind, encodedFilename, btn) {
  const item = _communityItem(kind, encodedFilename);
  if (!item || !item.yaml_source) {
    showToast(t('Not loaded — hit Refresh and try again'), 'error');
    return;
  }
  // One fork per item at a time: a second click while the first is on its
  // way used to fork the item twice.
  if (_forking.has(item.filename)) return;
  _forking.add(item.filename);
  if (btn) { btn.disabled = true; btn.classList.add('is-busy'); btn.setAttribute('aria-busy', 'true'); }
  try {
    await _openCommunityItem(kind, item);
  } finally {
    _forking.delete(item.filename);
    if (btn && btn.isConnected) { btn.disabled = false; btn.classList.remove('is-busy'); btn.removeAttribute('aria-busy'); }
  }
}

/** Items with a fork on its way, by filename. */
const _forking = new Set();

/** @param {string} kind @param {CommunityItem} item @param {string} [name] */
async function _openCommunityItem(kind, item, name) {
  if (kind === 'tasks') {
    // Saving the catalog copy again would add a second task of the same name.
    if (item.have && !(await confirmModal(
      t('You already have “{name}” in your library. Open the catalog copy anyway?', { name: item.name }),
      { title: t('Already yours'), confirmText: t('Open anyway') }))) return;
    // Self-contained and nothing to resolve: straight into the editor,
    // unsaved, so it can be read before it is kept.
    openDefinitionEditor(null, item.yaml_source);
    showToast(t('Community task opened — save it to add it to your library'), 'success');
    return;
  }
  // A playbook is a sequence of tasks and an automation runs a task or a
  // playbook, so forking one alone would land you with something that cannot
  // run. The server walks the graph and forks only what is genuinely absent.
  try {
    const result = await apiJson(
      `/api/v1/tasks/community/${kind}/${encodeURIComponent(item.filename)}/fork/`,
      { method: 'POST', body: JSON.stringify(name ? { name } : {}) });
    if (result.already) {
      showToast(t('You already have “{name}”', { name: item.name }), 'success');
      item.have = true;
      _renderCommunity(kind);
      return;
    }
    // A fork changes what you hold, which changes what every other card would
    // pull in — the playbook you just took may share tasks with the next one.
    // Drop the cached plans so the cards re-read them.
    _invalidateForkPlans();
    const made = result.created || [];
    const extra = made.length - 1;
    showToast(
      extra > 0
        ? tn('Forked “{name}” and the {n} item it needs', 'Forked “{name}” and the {n} items it needs', extra, { name: item.name })
        : t('Forked “{name}” into your library', { name: item.name }),
      'success');
    navigateTo('playbooks');
    if (typeof loadPlaybooks === 'function') loadPlaybooks();
    if (typeof loadAutomations === 'function') loadAutomations();
  } catch (e) {
    const clash = e.status === 409 && e.body && e.body.conflict;
    if (clash) {
      // Something of yours already has this name: fork it under another.
      const renamed = await promptModal(
        t('You already have a {kind} called “{name}”. Fork this one under another name:',
          { kind: _communityKindWord(kind), name: clash.name }),
        { value: t('{name} (community)', { name: item.name }), confirmText: t('Fork') });
      if (renamed) await _openCommunityItem(kind, item, renamed);
      return;
    }
    showToast(e.message || t('Could not fork this {kind}', { kind: _communityKindWord(kind) }), 'error');
  }
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#page-community .sub-tab').forEach((/** @type {HTMLElement} */ tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#page-community .sub-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('#page-community .sub-panel').forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      /** @type {HTMLElement} */ (document.getElementById(/** @type {DOMStringMap} */ (tab.dataset).subtab)).classList.add('active');
      communityKind = /** @type {DOMStringMap} */ (tab.dataset).kind;
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
  window.navigateTo = function (page) {
    _origNavCommunity(page);
    if (page === 'community') loadCommunityKind(communityKind);
  };
}


/* Community cards. openCommunityItem still takes (kind, filename); the values
   now travel in data attributes instead of being spliced into a JS string
   inside an HTML attribute, where encodeURIComponent was doing escaping for
   the wrong context. */
delegateClick('[data-comm-kind]', (el, ev) => {
  if (el.hasAttribute('data-stop')) ev.stopPropagation();
  else if (ev.target.closest('button, a')) return;
  const btn = el.matches('button') ? el : el.querySelector('button[data-comm-open]');
  openCommunityItem(el.dataset.commKind, el.dataset.commFile, btn);
});
delegateClick('[data-comm-serving]', () => toggleCommunityServing());
delegateClick('a[data-stop]', (el, ev) => ev.stopPropagation());
