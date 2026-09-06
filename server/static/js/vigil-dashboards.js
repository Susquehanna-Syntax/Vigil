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

function _widgetShell(widget) {
  const spec = (DASH.catalog.widgets || {})[widget.kind] || {};
  const title = spec.label || widget.kind;
  return `
    <div class="grid-stack-item-content dash-widget" data-kind="${escAttr(widget.kind)}">
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
