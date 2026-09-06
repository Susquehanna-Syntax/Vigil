/* Widget dashboards — the grid on the Dashboard page.
 *
 * The layout lives server-side (/api/v1/dashboards/), Gridstack owns the drag
 * and resize, and each widget kind has a renderer in WIDGET_RENDERERS below.
 *
 * Two modes. In *view* mode the grid is static and widgets refresh on a poll.
 * In *edit* mode Gridstack is unlocked, widgets grow a remove control, and the
 * layout is saved when you leave the mode — one PUT for the whole grid rather
 * than one request per nudge, because dragging a widget across a row emits
 * dozens of change events and every one of them is a layout nobody asked to
 * keep.
 */

const DASH = {
  boards: [],        // every dashboard visible to this user
  current: null,     // the one on screen
  catalog: null,     // widget registry from the server
  grid: null,        // the Gridstack instance
  editing: false,
  dirty: false,
  pollTimer: null,
};

/* ── Data ──────────────────────────────────────────────────────────────── */

async function _dashLoadAll() {
  const [boards, catalog] = await Promise.all([
    apiJson('/api/v1/dashboards/'),
    DASH.catalog ? Promise.resolve(DASH.catalog)
                 : apiJson('/api/v1/dashboards/catalog/'),
  ]);
  DASH.boards = boards;
  DASH.catalog = catalog;
  // Land on the default, or whatever is first — the server guarantees at
  // least one, so there is no empty state to design around.
  const wanted = DASH.current && boards.find(b => b.id === DASH.current.id);
  DASH.current = wanted || boards.find(b => b.is_default && b.is_mine) || boards[0];
}

/* ── Chrome ────────────────────────────────────────────────────────────── */

function _dashToolbar() {
  const opts = DASH.boards.map(b =>
    `<option value="${escAttr(b.id)}"${b.id === DASH.current.id ? ' selected' : ''}>`
    + `${escHtml(b.name)}${b.is_mine ? '' : ` (${escHtml(b.owner)})`}</option>`).join('');
  const mine = DASH.current.is_mine;
  return `
    <div class="dash-toolbar">
      <select class="form-control dash-switch" id="dash-switch">${opts}</select>
      ${mine ? `
        <button class="btn btn-${DASH.editing ? 'mint' : 'sky'} btn-sm" id="dash-edit">
          ${DASH.editing ? 'Done' : 'Edit layout'}</button>
        ${DASH.editing ? `<button class="btn btn-lav btn-sm" id="dash-add">+ Add widget</button>` : ''}
      ` : `<span class="chip">shared by ${escHtml(DASH.current.owner)} · read-only</span>`}
    </div>`;
}

// Returns what goes *inside* Gridstack's own .grid-stack-item-content. Emitting
// a second element with that class nested one inside the other, and only the
// outer one is stretched to the cell — so every widget collapsed to the height
// of its content and left dead space below it.
function _widgetShell(widget) {
  const spec = (DASH.catalog.widgets || {})[widget.kind] || {};
  const title = spec.label || widget.kind;
  return `
    <div class="dash-widget" data-kind="${escAttr(widget.kind)}">
      <div class="dash-widget-head">
        <span class="dash-widget-title">${escHtml(title)}</span>
        <span class="dash-widget-actions">
          <button class="btn btn-lav btn-xs dash-w-settings" data-id="${escAttr(widget.id)}"
                  title="Widget settings">Settings</button>
          <button class="btn btn-rose btn-xs dash-w-remove" data-id="${escAttr(widget.id)}"
                  title="Remove widget">&times;</button>
        </span>
      </div>
      <div class="dash-widget-body" data-widget-body="${escAttr(widget.id)}">
        <span class="muted-note">Loading…</span>
      </div>
    </div>`;
}

/* ── Render ────────────────────────────────────────────────────────────── */

function _dashRender() {
  const page = document.getElementById('page-dashboard');
  if (!page) return;
  const host = document.getElementById('dash-root');
  if (!host) return;

  host.innerHTML = _dashToolbar()
    + '<div class="grid-stack" id="dash-grid"></div>';

  // Gridstack's default renderCB assigns widget content with textContent, so a
  // markup string arrives on screen as literal angle brackets rather than as
  // elements. Its reason is to stop a caller injecting HTML by accident; ours
  // is built in _widgetShell with every interpolated value already through
  // escHtml/escAttr, so innerHTML is the correct assignment here.
  GridStack.renderCB = (el, widget) => { el.innerHTML = widget.content || ''; };

  // Gridstack rewrites the container, so it is rebuilt rather than reused.
  DASH.grid = GridStack.init({
    column: DASH.catalog.grid_columns || 12,
    cellHeight: 90,
    margin: 8,
    disableDrag: !DASH.editing || !DASH.current.is_mine,
    disableResize: !DASH.editing || !DASH.current.is_mine,
    animate: true,
    float: false,
  }, document.getElementById('dash-grid'));

  for (const w of DASH.current.widgets) {
    DASH.grid.addWidget({
      x: w.x, y: w.y, w: w.w, h: w.h,
      minW: (DASH.catalog.widgets[w.kind] || {}).min_w || 1,
      minH: (DASH.catalog.widgets[w.kind] || {}).min_h || 1,
      id: w.id,
      content: _widgetShell(w),
    });
  }

  DASH.grid.on('change', () => { DASH.dirty = true; });
  _dashBindChrome(host);
  _dashPaintAll();
}

function _dashBindChrome(host) {
  host.classList.toggle('dash-editing', DASH.editing);

  const sw = document.getElementById('dash-switch');
  if (sw) sw.onchange = async () => {
    if (DASH.editing) await _dashSaveLayout();
    DASH.editing = false;
    DASH.current = DASH.boards.find(b => b.id === sw.value) || DASH.current;
    _dashRender();
  };

  const edit = document.getElementById('dash-edit');
  if (edit) edit.onclick = async () => {
    if (DASH.editing) {
      await _dashSaveLayout();
      DASH.editing = false;
    } else {
      DASH.editing = true;
    }
    _dashRender();
  };

  const add = document.getElementById('dash-add');
  if (add) add.onclick = () => openWidgetCatalog();

  host.querySelectorAll('.dash-w-remove').forEach(btn => btn.onclick = () => {
    const el = btn.closest('.grid-stack-item');
    if (el) { DASH.grid.removeWidget(el); DASH.dirty = true; }
  });
  host.querySelectorAll('.dash-w-settings').forEach(btn => btn.onclick = () => {
    openWidgetSettings(btn.dataset.id);
  });
}

/* ── Persistence ───────────────────────────────────────────────────────── */

function _dashCollectLayout() {
  // Read geometry back off Gridstack rather than trusting our own copy: the
  // library reflows neighbours when one widget moves, and those neighbours
  // never fired an event of their own.
  const byId = new Map(DASH.current.widgets.map(w => [String(w.id), w]));
  return DASH.grid.save(false).map((node) => {
    const original = byId.get(String(node.id)) || {};
    return {
      kind: original.kind,
      x: node.x, y: node.y, w: node.w, h: node.h,
      settings: original.settings || {},
    };
  }).filter(w => w.kind);
}

async function _dashSaveLayout() {
  if (!DASH.dirty || !DASH.current.is_mine) return;
  const widgets = _dashCollectLayout();
  try {
    DASH.current = await apiJson(
      `/api/v1/dashboards/${DASH.current.id}/layout/`,
      { method: 'PUT', body: JSON.stringify({ widgets }) });
    const i = DASH.boards.findIndex(b => b.id === DASH.current.id);
    if (i >= 0) DASH.boards[i] = DASH.current;
    DASH.dirty = false;
    showToast('Layout saved', 'success');
  } catch (e) {
    showToast(e.message || 'Could not save the layout', 'error');
  }
}

/* ── Widget bodies ─────────────────────────────────────────────────────── */

function _dashPaintAll() {
  for (const w of DASH.current.widgets) {
    const body = document.querySelector(`[data-widget-body="${CSS.escape(String(w.id))}"]`);
    if (!body) continue;
    const render = WIDGET_RENDERERS[w.kind];
    if (!render) {
      body.innerHTML = '<span class="muted-note">No renderer for this widget yet.</span>';
      continue;
    }
    Promise.resolve()
      .then(() => render(body, w.settings || {}, w))
      .catch((e) => {
        body.innerHTML = `<span class="bad">${escHtml(e.message || 'Failed to load')}</span>`;
      });
  }
}

/* ── Entry ─────────────────────────────────────────────────────────────── */

async function loadDashboards() {
  const host = document.getElementById('dash-root');
  if (!host) return;
  try {
    await _dashLoadAll();
  } catch (e) {
    host.innerHTML = `<div class="empty-block"><h4>Couldn't load your dashboard</h4>`
      + `<p>${escHtml(e.message)}</p></div>`;
    return;
  }
  _dashRender();

  // Refresh widget contents on a poll, but never while the layout is being
  // edited — repainting under a drag is how a widget ends up somewhere nobody
  // dropped it.
  if (DASH.pollTimer) clearPollingInterval(DASH.pollTimer);
  DASH.pollTimer = pollingInterval(() => {
    if (!DASH.editing && document.getElementById('dash-root')) _dashPaintAll();
  }, 15000);
}


/* ── Add-widget rail ───────────────────────────────────────────────────── */

//: Group → SQSY accent. Sky for the everyday fleet widgets and peach for
//: security were chosen; the rest follow the design language's own meanings —
//: mint is healthy, lavender is information, lemon is a caution, rose is the
//: one that costs money.
const GROUP_ACCENT = {
  fleet: 'sky', health: 'mint', work: 'lav',
  security: 'peach', utility: 'lemon', business: 'rose',
};
const GROUP_LABEL = {
  fleet: 'Fleet', health: 'Health', work: 'Work in flight',
  security: 'Security', utility: 'Utility', business: 'Business',
};
const GROUP_ORDER = ['fleet', 'health', 'work', 'security', 'utility', 'business'];

function _railHtml() {
  const widgets = DASH.catalog.widgets || {};
  const byGroup = new Map(GROUP_ORDER.map(g => [g, []]));
  for (const [kind, spec] of Object.entries(widgets)) {
    const bucket = byGroup.get(spec.group) || byGroup.get('utility');
    bucket.push([kind, spec]);
  }
  const sections = GROUP_ORDER.map((group) => {
    const items = (byGroup.get(group) || []).sort((a, b) =>
      a[1].label.localeCompare(b[1].label));
    if (!items.length) return '';
    return `
      <div class="rail-group rail-${escAttr(group)}">
        <div class="rail-group-label">${escHtml(GROUP_LABEL[group] || group)}</div>
        ${items.map(([kind, spec]) => `
          <div class="rail-card" data-add-kind="${escAttr(kind)}"
               title="${escAttr(spec.description || '')}">
            <div class="rail-card-name">${escHtml(spec.label)}</div>
            <div class="rail-card-desc">${escHtml(spec.description || '')}</div>
            <button class="rail-add btn-${escAttr(GROUP_ACCENT[group] || 'sky')}"
                    data-add-kind="${escAttr(kind)}"
                    aria-label="Add ${escAttr(spec.label)}">+</button>
          </div>`).join('')}
      </div>`;
  }).join('');
  return `<aside class="dash-rail" id="dash-rail">
    <div class="rail-head">
      <span>Add a widget</span>
      <button class="modal-close" id="rail-close" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="rail-body">${sections}</div>
  </aside>`;
}

function _addWidgetOfKind(kind) {
  const spec = (DASH.catalog.widgets || {})[kind];
  if (!spec) return;
  // No x/y: Gridstack finds the first free slot, which is what "add" means
  // when the operator has not chosen a position.
  const id = `new-${kind}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const widget = {
    id, kind,
    w: spec.w, h: spec.h,
    settings: Object.fromEntries(
      Object.entries(spec.settings || {}).map(([k, f]) => [k, f.default])),
  };
  DASH.current.widgets.push(widget);
  DASH.grid.addWidget({
    w: spec.w, h: spec.h, minW: spec.min_w, minH: spec.min_h,
    id, content: _widgetShell(widget),
  });
  DASH.dirty = true;
  _dashBindChrome(document.getElementById('dash-root'));
  _dashPaintAll();
  showToast(`${spec.label} added`, 'success');
}

function openWidgetCatalog() {
  const rail = document.getElementById('dash-rail');
  if (rail) { rail.classList.toggle('open'); return; }
  const host = document.getElementById('dash-root');
  if (!host) return;
  host.insertAdjacentHTML('beforeend', _railHtml());
  const el = document.getElementById('dash-rail');
  requestAnimationFrame(() => el.classList.add('open'));
  el.querySelector('#rail-close').onclick = () => el.classList.remove('open');
  el.querySelectorAll('[data-add-kind]').forEach((node) => {
    node.addEventListener('click', (ev) => {
      ev.stopPropagation();
      _addWidgetOfKind(node.dataset.addKind);
    });
  });
}

/* The dashboard is the landing page, so it loads on boot as well as on every
   later navigation back to it — the wrap-navigateTo idiom the other feature
   files use (see vigil-nav.js). */
(function () {
  const previous = window.navigateTo;
  window.navigateTo = function (pageName) {
    previous.apply(this, arguments);
    if (pageName === 'dashboard') loadDashboards();
  };
  document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('dash-root')) loadDashboards();
  });
})();
