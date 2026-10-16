// vigil-reports.js
// Owns: the Reports page — the saved-report list,
//   the builder dialog, and the viewer that charts a report's widgets with
//   Chart.js and offers each one as CSV. Backed by /api/v1/reports/.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr, showToast, confirmModal,
//   tok, delegateClick) and Chart.js on window (base.html).

const RPT_RANGES = [7, 14, 30, 90, 180, 365];
const RPT_DEFAULT_KINDS = ['success_rate', 'runs_per_day', 'failures_by_step'];
const RPT_SERIES_TOKENS = ['--oc-1', '--oc-2', '--oc-3', '--oc-4'];

let _rptReports = [];
let _rptCatalog = [];
let _rptPlaybooks = [];
let _rptAutomations = [];
let _rptLoaded = false;
let _rptCharts = [];
// The viewer's Gridstack and the charts drawn inside it, by widget index: a
// resize hands the card new pixels and Chart.js only fills them when asked.
let _rptGrid = null;
let _rptChartAt = [];
let _rptEditing = false;
let _rptDirty = false;
// The row _rptReports holds for the report on screen — its saved widget specs,
// which is what a layout save writes its new places into.
let _rptOpen = null;
// The builder's working state: the widget rows, and the ids ticked in the two
// checkbox lists.
let _rptDraft = { widgets: [], 'rpt-pb': new Set(), 'rpt-auto': new Set() };
let _rptOpenId = null;
// The builder's live preview: its own charts (the viewer's and the preview's
// never destroy each other), the pending debounce, and the number of the latest
// request so a slow reply to an older one is dropped.
let _rptPreviewCharts = [];
let _rptPreviewTimer = null;
let _rptPreviewSeq = 0;

function _rptSourceNames(report) {
  const names = [];
  for (const id of report.playbooks || []) {
    const p = _rptPlaybooks.find(x => String(x.id) === String(id));
    if (p) names.push(p.name);
  }
  for (const id of report.automations || []) {
    const a = _rptAutomations.find(x => String(x.id) === String(id));
    if (a) names.push(a.name);
  }
  return names;
}

// Whether a widget has anything to draw. A rate or a duration is null on a day
// with nothing finished, so any number counts, 0 included: a playbook that
// failed everywhere has a success rate of 0%, which is the point of the chart.
// A count is 0 on an empty day, so only a count above 0 counts.
function _rptHasData(w) {
  const d = w.data || {};
  if (Array.isArray(d.rows)) return d.rows.length > 0;
  const measured = w.kind === 'success_rate' || w.kind === 'duration';
  const has = v => (measured ? v !== null && v !== undefined : Number(v) > 0);
  if (Array.isArray(d.series)) return d.series.some(s => (s.values || []).some(has));
  return ['ok', 'failed', 'pending', 'median', 'p95'].some(k => (d[k] || []).some(has));
}

/* ── List ────────────────────────────────────────────────────────────── */

async function loadReports(refreshView) {
  const list = document.getElementById('rpt-list');
  if (!list) return;
  if (!_rptLoaded) list.innerHTML = '<div class="empty-block"><p>Loading…</p></div>';
  try {
    const [reports, catalog, playbooks, automations] = await Promise.all([
      apiJson('/api/v1/reports/'),
      apiJson('/api/v1/reports/widgets/'),
      apiJson('/api/v1/playbooks/'),
      apiJson('/api/v1/automations/').catch(() => ({ automations: [] })),
    ]);
    _rptReports = Array.isArray(reports) ? reports : [];
    _rptCatalog = Array.isArray(catalog) ? catalog : [];
    _rptPlaybooks = Array.isArray(playbooks) ? playbooks : [];
    _rptAutomations = (automations && automations.automations) || [];
    _rptLoaded = true;
  } catch (e) {
    list.innerHTML = `<div class="empty-block"><h4>Couldn't load reports</h4>`
      + `<p>${escHtml(e.message)}</p></div>`;
    return;
  }
  if (_rptOpenId === null) { _renderReportList(); return; }
  // The report open on screen can have just been edited or deleted; an open
  // report with no row any more is no reason to hold the old view open.
  if (_rptReports.some(r => r.id === _rptOpenId)) {
    if (refreshView) await openReport(_rptOpenId);
  } else {
    _showReportList();
  }
}

function _rptCardHtml(r) {
  const names = _rptSourceNames(r);
  const shown = names.slice(0, 3).join(', ') + (names.length > 3 ? ` +${names.length - 3}` : '');
  const charts = (r.widgets || []).length;
  return `<div class="def-card">
    <div class="def-card-body">
      <div class="def-card-title">${escHtml(r.name)}</div>
      <div class="def-card-desc">${escHtml(shown || 'No sources')}</div>
      <div class="def-card-meta">
        <span>last ${escHtml(String(r.range_days))} days</span>
        <span class="dot-sep">·</span>
        <span>${escHtml(String(charts))} chart${charts === 1 ? '' : 's'}</span>
      </div>
    </div>
    <div class="def-card-footer">
      <button class="btn btn-sky btn-sm" type="button" data-rpt-open="${escAttr(r.id)}">Open</button>
      <button class="btn btn-lemon btn-sm" type="button" data-rpt-edit="${escAttr(r.id)}">Edit</button>
      <button class="btn btn-rose btn-sm" type="button" data-rpt-del="${escAttr(r.id)}">Delete</button>
    </div>
  </div>`;
}

function _renderReportList() {
  const list = document.getElementById('rpt-list');
  if (!list) return;
  const q = (document.getElementById('rpt-search')?.value || '').trim().toLowerCase();
  const reports = !q ? _rptReports : _rptReports.filter(r =>
    (r.name || '').toLowerCase().includes(q)
    || (r.description || '').toLowerCase().includes(q)
    || _rptSourceNames(r).some(n => n.toLowerCase().includes(q)));
  if (reports.length) { list.innerHTML = reports.map(_rptCardHtml).join(''); return; }
  if (_rptReports.length) {
    list.innerHTML = '<div class="empty-block"><h4>No reports match that search</h4>'
      + '<p>Nothing of yours is named that.</p></div>';
    return;
  }
  list.innerHTML = `<div class="empty-block">
    <h4>No reports yet</h4>
    <p>A report charts how your playbooks and automations are doing over time.</p>
    <button class="btn btn-mint btn-sm" type="button" data-rpt-new>New report</button>
  </div>`;
}

function _showReportList() {
  _rptOpenId = null;
  _rptOpen = null;
  _destroyRptCharts();
  const view = document.getElementById('rpt-view');
  if (view) { view.hidden = true; view.replaceChildren(); }
  const toolbar = document.getElementById('rpt-toolbar');
  if (toolbar) toolbar.hidden = false;
  const list = document.getElementById('rpt-list');
  if (list) list.hidden = false;
  _renderReportList();
}

/* ── Builder: draft rows (no DOM work) ───────────────────────────────── */

const RPT_CHART_NAMES = {
  line: 'Line', area: 'Area', 'stacked-bar': 'Stacked bars', column: 'Columns',
  pie: 'Pie', doughnut: 'Doughnut', table: 'Table',
};

//: A days metric's bars stand up; a rows metric's run across the page.
function rptChartLabel(chart, family) {
  if (chart === 'bar') return family === 'rows' ? 'Bars across' : 'Bars';
  return RPT_CHART_NAMES[chart] || chart;
}

function rptHasAxes(chart) {
  return !['pie', 'doughnut', 'table'].includes(chart);
}

//: A saved spec as the builder edits it. Blank label text means "the default";
//: both labels saved as "" means the titles were switched off. The card's place
//: on the viewer grid is carried through untouched.
function rptDraftFrom(spec, catalog) {
  const entry = (catalog || []).find(c => c.kind === spec.kind) || {};
  return {
    kind: spec.kind,
    title: spec.title || '',
    chart: spec.chart || entry.chart,
    xLabel: typeof spec.x_label === 'string' ? spec.x_label : '',
    yLabel: typeof spec.y_label === 'string' ? spec.y_label : '',
    axes: !(spec.x_label === '' && spec.y_label === ''),
    legend: spec.legend === true ? 'show' : spec.legend === false ? 'hide' : 'auto',
    sourceMode: (spec.sources || []).length ? 'some' : 'all',
    sources: new Set((spec.sources || []).map(String)),
    place: ('x' in spec) ? { x: spec.x, y: spec.y, w: spec.w, h: spec.h } : null,
  };
}

//: The spec to save: only what the operator set, so every default stays the
//: server's to choose.
function rptSpecFromDraft(d) {
  const spec = { kind: d.kind, chart: d.chart };
  const title = (d.title || '').trim();
  if (title) spec.title = title;
  if (rptHasAxes(d.chart)) {
    if (!d.axes) {
      spec.x_label = '';
      spec.y_label = '';
    } else {
      const x = (d.xLabel || '').trim();
      const y = (d.yLabel || '').trim();
      if (x) spec.x_label = x;
      if (y) spec.y_label = y;
    }
  }
  if (d.legend === 'show') spec.legend = true;
  if (d.legend === 'hide') spec.legend = false;
  if (d.sourceMode === 'some' && d.sources && d.sources.size) spec.sources = [...d.sources];
  if (d.place) Object.assign(spec, d.place);
  return spec;
}

//: Another metric picked: keep the chart if the new one can be drawn that way,
//: and drop typed axis titles, which named the old metric's axes.
function rptKindChanged(d, entry) {
  d.kind = entry.kind;
  if (!(entry.charts || []).includes(d.chart)) d.chart = entry.chart;
  d.xLabel = '';
  d.yLabel = '';
  return d;
}

/* ── Builder ─────────────────────────────────────────────────────────── */

function openReportEditor(report) {
  if (!_rptLoaded) return;
  document.getElementById('rpt-editor-title').textContent = report ? 'Edit report' : 'New report';
  document.getElementById('rpt-editor-modal').dataset.editing = report ? report.id : '';
  document.getElementById('rpt-name').value = report ? report.name : '';
  document.getElementById('rpt-description').value = report ? (report.description || '') : '';
  const range = document.getElementById('rpt-range');
  range.innerHTML = RPT_RANGES.map(n => `<option value="${n}">Last ${n} days</option>`).join('');
  range.value = String(report ? report.range_days : 30);
  _rptDraft = {
    widgets: (report ? (report.widgets || []) : RPT_DEFAULT_KINDS.map(kind => ({ kind })))
      .map(spec => rptDraftFrom(spec, _rptCatalog)),
    'rpt-pb': new Set(report ? (report.playbooks || []).map(String) : []),
    'rpt-auto': new Set(report ? (report.automations || []).map(String) : []),
  };
  const pbFilter = document.getElementById('rpt-pb-filter');
  if (pbFilter) pbFilter.value = '';
  const autoFilter = document.getElementById('rpt-auto-filter');
  if (autoFilter) autoFilter.value = '';
  _renderRptPicks('rpt-pb-picks', 'rpt-pb', _rptPlaybooks);
  _renderRptPicks('rpt-auto-picks', 'rpt-auto', _rptAutomations);
  _renderRptWidgetAdd();
  _renderRptWidgets();
  document.getElementById('rpt-editor-overlay').classList.add('open');
  document.getElementById('rpt-editor-modal').classList.add('open');
  _rptRunPreview();
}

function closeRptEditor() {
  document.getElementById('rpt-editor-modal')?.classList.remove('open');
  document.getElementById('rpt-editor-overlay')?.classList.remove('open');
  clearTimeout(_rptPreviewTimer);
  _rptPreviewTimer = null;
  _rptPreviewSeq += 1;
  _destroyRptPreview();
}

function _rptPicked(group) {
  return [..._rptDraft[group]];
}

function _renderRptPicks(boxId, group, sources) {
  const box = document.getElementById(boxId);
  if (!box) return;
  const q = (document.getElementById(`${group}-filter`)?.value || '').trim().toLowerCase();
  const rows = !q ? sources : sources.filter(s => (s.name || '').toLowerCase().includes(q));
  if (!rows.length) {
    box.innerHTML = `<div class="rpt-none muted">${sources.length ? 'Nothing matches that filter.' : 'None yet.'}</div>`;
    return;
  }
  box.innerHTML = rows.map(s => `<label class="rpt-pick">
      <input type="checkbox" data-rpt-src="${escAttr(group)}" value="${escAttr(String(s.id))}"
        ${_rptDraft[group].has(String(s.id)) ? 'checked' : ''}>
      <span>${escHtml(s.name)}</span>
    </label>`).join('');
}

function _renderRptWidgetAdd() {
  const sel = document.getElementById('rpt-widget-add');
  if (!sel) return;
  sel.innerHTML = '<option value="">Add a chart…</option>' + _rptCatalog.map(c =>
    `<option value="${escAttr(c.kind)}">${escHtml(c.title)} — ${escHtml(c.description)}</option>`).join('');
  sel.value = '';
}

function _renderRptWidgets() {
  const box = document.getElementById('rpt-widgets');
  if (!box) return;
  // The canvases are about to go; their charts go with them, and the
  // scheduled preview draws fresh ones.
  _destroyRptPreview();
  if (!_rptDraft.widgets.length) {
    box.innerHTML = '<div class="rpt-none muted">Pick at least one chart.</div>';
    return;
  }
  const picked = _rptPickedSources();
  box.innerHTML = _rptDraft.widgets.map((d, i) => _rptWidgetCardHtml(d, i, picked)).join('');
}

//: The report's ticked playbooks and automations, by id and name: the choices
//: a chart's "Only these" can narrow to.
function _rptPickedSources() {
  return [
    ..._rptPlaybooks.filter(p => _rptDraft['rpt-pb'].has(String(p.id))),
    ..._rptAutomations.filter(a => _rptDraft['rpt-auto'].has(String(a.id))),
  ].map(s => ({ id: String(s.id), name: s.name }));
}

function _rptWidgetCardHtml(d, i, picked) {
  const entry = _rptCatalog.find(c => c.kind === d.kind) || { title: d.kind, charts: [d.chart], labels: {} };
  const last = _rptDraft.widgets.length - 1;
  const labels = (entry.labels || {})[d.chart] || ['', ''];
  const axesOff = d.axes ? '' : 'disabled';
  const sourcePicks = !picked.length
    ? '<div class="rpt-none muted">Tick a playbook or automation above first.</div>'
    : picked.map(s => `<label class="rpt-pick">
        <input type="checkbox" data-rpt-wsrc="${i}" value="${escAttr(s.id)}"
          ${d.sources.has(s.id) ? 'checked' : ''} ${d.sourceMode === 'some' ? '' : 'disabled'}>
        <span>${escHtml(s.name)}</span>
      </label>`).join('');
  return `<div class="rpt-wcard">
    <div class="rpt-wset">
      <label class="rpt-wfield">Data
        <select class="form-control" data-rpt-wkind="${i}">
          ${_rptCatalog.map(c => `<option value="${escAttr(c.kind)}" ${c.kind === d.kind ? 'selected' : ''}>${escHtml(c.title)}</option>`).join('')}
        </select>
      </label>
      <div class="rpt-how">${escHtml(entry.how || '')}</div>
      <label class="rpt-wfield">Show as
        <select class="form-control" data-rpt-wchart="${i}">
          ${(entry.charts || []).map(ch => `<option value="${escAttr(ch)}" ${ch === d.chart ? 'selected' : ''}>${escHtml(rptChartLabel(ch, entry.family))}</option>`).join('')}
        </select>
      </label>
      <label class="rpt-wfield">Title
        <input type="text" class="form-control" data-rpt-wtitle="${i}" maxlength="80"
          placeholder="${escAttr(entry.title)}" value="${escAttr(d.title)}">
      </label>
      <div class="rpt-axes" ${rptHasAxes(d.chart) ? '' : 'hidden'}>
        <label class="rpt-check"><input type="checkbox" data-rpt-waxes="${i}" ${d.axes ? 'checked' : ''}> Show axis titles</label>
        <label class="rpt-wfield">Horizontal axis
          <input type="text" class="form-control" data-rpt-wx="${i}" maxlength="40"
            placeholder="${escAttr(labels[0] || '')}" value="${escAttr(d.xLabel)}" ${axesOff}>
        </label>
        <label class="rpt-wfield">Vertical axis
          <input type="text" class="form-control" data-rpt-wy="${i}" maxlength="40"
            placeholder="${escAttr(labels[1] || '')}" value="${escAttr(d.yLabel)}" ${axesOff}>
        </label>
      </div>
      <label class="rpt-wfield">Legend
        <select class="form-control" data-rpt-wlegend="${i}">
          ${[['auto', 'Auto'], ['show', 'Show'], ['hide', 'Hide']].map(([v, t]) =>
            `<option value="${v}" ${d.legend === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </label>
      <div class="rpt-wsrc">
        <label class="rpt-check"><input type="radio" name="rpt-wsrcmode-${i}" data-rpt-wsrcmode="${i}" value="all"
          ${d.sourceMode === 'all' ? 'checked' : ''}> All of this report's sources</label>
        <label class="rpt-check"><input type="radio" name="rpt-wsrcmode-${i}" data-rpt-wsrcmode="${i}" value="some"
          ${d.sourceMode === 'some' ? 'checked' : ''}> Only these</label>
        <div class="rpt-wsrc-picks">${sourcePicks}</div>
      </div>
      <div class="rpt-wbtns">
        ${_rptDraft.widgets.length < 12
          ? `<button class="btn btn-outline btn-xs" type="button" data-rpt-wdup="${i}">Duplicate</button>` : ''}
        <button class="btn btn-outline btn-xs" type="button" data-rpt-wup="${i}" ${i === 0 ? 'disabled' : ''}>Move up</button>
        <button class="btn btn-outline btn-xs" type="button" data-rpt-wdown="${i}" ${i === last ? 'disabled' : ''}>Move down</button>
        <button class="btn btn-rose btn-xs" type="button" data-rpt-wdel="${i}">Remove</button>
      </div>
    </div>
    <div class="rpt-wprev" data-rpt-wprev="${i}"><div class="rpt-empty muted">Drawing the preview…</div></div>
  </div>`;
}

/* ── Builder: live preview ───────────────────────────────────────────── */

function _destroyRptPreview() {
  _rptPreviewCharts.forEach(c => c.destroy());
  _rptPreviewCharts = [];
}

function _rptSchedulePreview() {
  clearTimeout(_rptPreviewTimer);
  _rptPreviewTimer = setTimeout(_rptRunPreview, 400);
}

function _rptPreviewBoxes() {
  return [...document.querySelectorAll('#rpt-widgets [data-rpt-wprev]')];
}

function _rptPreviewMessage(text) {
  _destroyRptPreview();
  for (const el of _rptPreviewBoxes()) el.innerHTML = `<div class="rpt-empty muted">${escHtml(text)}</div>`;
}

//: Draws every chart from the draft as it stands, from real run history. A
//: reply to anything but the latest request is dropped, so typing fast never
//: leaves an older preview on screen.
async function _rptRunPreview() {
  _rptPreviewTimer = null;
  if (!_rptDraft.widgets.length) return;
  const body = _rptBodyFromForm();
  if (!body.playbooks.length && !body.automations.length) {
    _rptPreviewSeq += 1;
    _rptPreviewMessage('Pick a playbook or automation to see a preview.');
    return;
  }
  const seq = ++_rptPreviewSeq;
  let data;
  try {
    data = await apiJson('/api/v1/reports/preview/', {
      method: 'POST',
      body: JSON.stringify({ range_days: body.range_days, playbooks: body.playbooks,
                             automations: body.automations, widgets: body.widgets }),
    });
  } catch (e) {
    if (seq === _rptPreviewSeq) _rptPreviewMessage(e.message);
    return;
  }
  if (seq !== _rptPreviewSeq) return;
  _destroyRptPreview();
  const widgets = data.widgets || [];
  for (const el of _rptPreviewBoxes()) {
    const i = Number(el.dataset.rptWprev);
    if (widgets[i]) _drawRptWidget(el, widgets[i], i, _rptPreviewCharts);
  }
}

function _rptBodyFromForm() {
  const range = Number(document.getElementById('rpt-range').value);
  return {
    name: document.getElementById('rpt-name').value.trim(),
    description: document.getElementById('rpt-description').value.trim(),
    range_days: Number.isFinite(range) ? range : 30,
    widgets: _rptDraft.widgets.map(rptSpecFromDraft),
    playbooks: _rptPicked('rpt-pb'),
    automations: _rptPicked('rpt-auto'),
  };
}

async function saveReport() {
  const btn = document.getElementById('rpt-save-btn');
  const body = _rptBodyFromForm();
  if (!body.name) { showToast('Give the report a name', 'error'); return; }
  if (!body.widgets.length) { showToast('Pick at least one chart', 'error'); return; }
  if (!body.playbooks.length && !body.automations.length) {
    showToast('Pick at least one playbook or automation', 'error');
    return;
  }
  const editing = document.getElementById('rpt-editor-modal').dataset.editing;
  btn.disabled = true;
  try {
    await apiJson(editing ? `/api/v1/reports/${editing}/` : '/api/v1/reports/', {
      method: editing ? 'PATCH' : 'POST',
      body: JSON.stringify(body),
    });
    showToast(editing ? 'Report updated' : 'Report created', 'success');
    closeRptEditor();
    await loadReports(_rptOpenId !== null);
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function deleteReport(id) {
  const r = _rptReports.find(x => x.id === id);
  if (!(await confirmModal(`Delete the report “${r ? r.name : ''}”?`,
    { danger: true, confirmText: 'Delete' }))) return;
  try {
    await apiJson(`/api/v1/reports/${id}/`, { method: 'DELETE' });
    showToast('Report deleted', 'success');
  } catch (e) {
    showToast(e.message, 'error');
    return;
  }
  if (_rptOpenId === id) _showReportList();
  await loadReports();
}

/* ── Viewer ──────────────────────────────────────────────────────────── */

function _destroyRptCharts() {
  _rptCharts.forEach(c => c.destroy());
  _rptCharts = [];
  _rptChartAt = [];
  // The grid holds the cards the charts were drawn into, so it goes with them:
  // destroy(false) leaves our markup in the DOM rather than tearing it out from
  // under a view we are about to replace wholesale.
  if (_rptGrid) _rptGrid.destroy(false);
  _rptGrid = null;
  _rptEditing = false;
  _rptDirty = false;
}

async function openReport(id) {
  const view = document.getElementById('rpt-view');
  if (!view) return;
  let data;
  try {
    data = await apiJson(`/api/v1/reports/${id}/data/`);
  } catch (e) {
    showToast(e.message, 'error');
    return;
  }
  const report = _rptReports.find(r => r.id === id) || {};
  _rptOpenId = id;
  _rptOpen = report;
  _destroyRptCharts();
  const list = document.getElementById('rpt-list');
  if (list) { list.hidden = true; list.replaceChildren(); }
  const toolbar = document.getElementById('rpt-toolbar');
  if (toolbar) toolbar.hidden = true;
  const range = data.range || {};
  const widgets = data.widgets || [];
  view.hidden = false;
  view.innerHTML = `
    <div class="rpt-head">
      <button class="btn btn-outline btn-sm" type="button" data-rpt-back>Back to reports</button>
      <span class="rpt-head-title">${escHtml(report.name || 'Report')}</span>
      <span class="rpt-head-range muted">${escHtml(String(range.from || ''))} – ${escHtml(String(range.to || ''))}
        · last ${escHtml(String((range.days || []).length || report.range_days || ''))} days</span>
      <button class="btn btn-lemon btn-sm" type="button" data-rpt-view-edit>Edit</button>
      <button class="btn btn-outline btn-sm" type="button" data-rpt-layout>Edit layout</button>
    </div>
    ${data.truncated
      ? '<div class="rpt-note muted">Too much history to read at once — this covers the most recent runs only.</div>'
      : ''}
    <div class="grid-stack rpt-gs" id="rpt-gs"></div>`;
  _renderRptGrid(id, widgets);
}

//: The viewer's grid: the widgets someone placed go where they left them, and
//: the ones nobody has placed yet take the first space there is. staticGrid to
//: begin with — the cards only move while the header says Done.
function _renderRptGrid(id, widgets) {
  const el = document.getElementById('rpt-gs');
  if (!el) return;
  _rptGrid = gridMake(el, {
    column: 12,
    staticGrid: true,
    columnOpts: { breakpoints: [{ w: 700, c: 1 }] },
  });
  _rptGrid.on('change', () => { _rptDirty = true; });
  // A resize hands the card new pixels; Chart.js only fills them when asked.
  // The chart's own container is what Gridstack passes back, so the index is
  // read off the body inside it.
  _rptGrid.on('resizestop', (event, item) => {
    const chart = _rptChartAt[_rptIndexOf(item)];
    if (chart) chart.resize();
  });

  const placed = widgets.filter(w => w.layout);
  const rest = widgets.filter(w => !w.layout);
  for (const w of placed) _rptAddWidget(w, widgets.indexOf(w), { ...w.layout });
  for (const w of rest) _rptAddWidget(w, widgets.indexOf(w), { autoPosition: true, w: 4, h: 4 });

  widgets.forEach((w, i) => {
    _drawRptWidget(el.querySelector(`[data-rpt-body="${i}"]`), w, i);
  });
}

//: The id Gridstack carries is the widget's index in the report's own order —
//: the same index the CSV link and the saved specs are counted in.
function _rptAddWidget(w, i, place) {
  _rptGrid.addWidget({
    ...place,
    id: String(i),
    minW: 2,
    minH: 2,
    content: `<div class="rpt-card">
        <div class="rpt-card-head">
          <span class="rpt-card-title">${escHtml(w.title)}</span>
          <a class="btn btn-outline btn-xs" href="/api/v1/reports/${escAttr(_rptOpenId)}/data/${i}.csv" download>CSV</a>
        </div>
        <div class="rpt-card-body" data-rpt-body="${i}"></div>
      </div>`,
  });
}

//: Which widget an event's grid item is: Gridstack's own node id, read off the
//: live node where it has one and off the `gs-id` attribute it stamps on the
//: element otherwise — resizestop hands back the inner content div, whose only
//: route to the item is up to its parent. The id is the widget's index in the
//: report's own order, the same index the CSV link and the saved specs use.
function _rptIndexOf(item) {
  const el = item && item.closest ? item.closest('.grid-stack-item') : null;
  const node = (el && el.gridstackNode) || (item && item.gridstackNode) || null;
  const id = node ? node.id : (el ? el.getAttribute('gs-id') : null);
  const n = Number(id);
  return Number.isInteger(n) ? n : -1;
}

//: Edit layout lets the cards move; Done puts them back and saves the places
//: that changed. A failed save leaves the operator in edit mode — the
//: arrangement on screen is still the one they wanted, and leaving would throw
//: it away as surely as discarding it.
async function _rptToggleLayout() {
  if (!_rptGrid) return;
  if (_rptEditing) {
    if (_rptDirty && !(await _rptSaveLayout())) return;
    _rptSetLayoutEditing(false);
  } else {
    // Under the breakpoint every card is one column wide; places saved from
    // there would squash the twelve-column layout for everyone.
    if (_rptGrid.getColumn() !== 12) {
      showToast('Widen the window to arrange the cards.', 'info');
      return;
    }
    // Only a move made while editing is worth saving, not the grid settling.
    _rptDirty = false;
    _rptSetLayoutEditing(true);
  }
}

function _rptSetLayoutEditing(on) {
  _rptEditing = on;
  _rptGrid.setStatic(!on);
  const button = document.querySelector('[data-rpt-layout]');
  if (button) button.textContent = on ? 'Done' : 'Edit layout';
  const grid = document.getElementById('rpt-gs');
  if (grid) grid.classList.toggle('rpt-gs-editing', on);
}

//: The saved specs plus where the cards are now, PATCHed to the report.
//: Resolves true when the layout is saved, false when the operator is staying
//: in edit mode to try again.
async function _rptSaveLayout() {
  const saved = (_rptOpen && _rptOpen.widgets) || [];
  try {
    const updated = await apiJson(`/api/v1/reports/${_rptOpenId}/`, {
      method: 'PATCH',
      body: JSON.stringify({ widgets: rptMergeLayout(saved, gridLayoutOf(_rptGrid)) }),
    });
    const at = _rptReports.findIndex(r => r.id === _rptOpenId);
    if (at >= 0) _rptReports[at] = updated;
    _rptOpen = updated;
    _rptDirty = false;
    showToast('Layout saved', 'success');
    return true;
  } catch (e) {
    showToast(e.message, 'error');
    return false;
  }
}

//: charts is the list the instance joins: the viewer's by default, the
//: builder's preview its own, so neither tears down the other's.
function _drawRptWidget(body, w, i, charts = _rptCharts) {
  if (!body) return;
  if (w.chart === 'table') {
    body.innerHTML = _rptTableHtml(w);
    return;
  }
  if (!_rptHasData(w)) {
    // An empty failures chart means nothing failed, not that nothing ran.
    const empty = w.kind === 'failures_by_step' ? 'No failures in this range.' : 'Nothing ran in this range.';
    body.innerHTML = `<div class="rpt-empty muted">${empty}</div>`;
    return;
  }
  const cfg = rptChartConfig(w);
  if (!cfg) {
    body.innerHTML = '<div class="rpt-empty muted">This chart kind is not known to this build.</div>';
    return;
  }
  const holder = document.createElement('div');
  holder.className = 'rpt-chart';
  holder.innerHTML = '<canvas></canvas>';
  body.replaceChildren(holder);
  const chart = new Chart(holder.querySelector('canvas').getContext('2d'), cfg);
  charts.push(chart);
  if (charts === _rptCharts) _rptChartAt[i] = chart;
}

const RPT_STATE_ACCENT = {
  ok: 'mint', not_applicable: 'lav', skipped: 'lemon',
  failed: 'rose', pending: 'peach',
};

//: A widget asked for a table gets one: one row per day with a column per
//: series, one row per host-run for outcomes, otherwise one row per record.
function _rptTableHtml(w) {
  return rptFamily(w) === 'days' ? _rptDaysTable(w) : _rptRowsTable(w);
}

function _rptDaysTable(w) {
  const series = rptDaySeries(w);
  const days = (w.data || {}).days || [];
  if (!days.length) return '<div class="rpt-empty muted">Nothing ran in this range.</div>';
  return `<table class="rpt-table">
    <thead><tr><th>Day</th>${series.map(x => `<th>${escHtml(x.name)}</th>`).join('')}</tr></thead>
    <tbody>${days.map((day, d) => `<tr><td>${escHtml(day)}</td>${
      series.map(x => `<td>${escHtml(_rptCell(w.kind, (x.values || [])[d]))}</td>`).join('')
    }</tr>`).join('')}</tbody>
  </table>`;
}

//: A day with no runs is an empty cell, not a 0 and not a "null". A rate is
//: already a percentage and a duration is seconds; both read wrong as plain
//: numbers.
function _rptCell(kind, value) {
  if (value === null || value === undefined) return '';
  if (kind === 'success_rate') return `${value}%`;
  if (kind === 'duration') return rptFormatSeconds(value);
  return String(value);
}

function _rptRowsTable(w) {
  const rows = (w.data || {}).rows || [];
  if (!rows.length) return '<div class="rpt-empty muted">Nothing ran in this range.</div>';
  if (w.kind === 'host_outcomes') {
    return `<table class="rpt-table">
      <thead><tr><th>Host</th><th>Source</th><th>Outcome</th><th>When</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
          <td>${escHtml(r.host)}</td>
          <td>${escHtml(r.source)}</td>
          <td><span class="run-state t-${escAttr(RPT_STATE_ACCENT[r.outcome] || 'lav')}">${escHtml(r.outcome)}</span></td>
          <td>${escHtml(r.at ? new Date(r.at).toLocaleString() : '')}</td>
        </tr>`).join('')}</tbody>
    </table>`;
  }
  // The value column's name and the field under it differ; the header says
  // which one this widget counts.
  const [second, head, field] = w.kind === 'branch_paths'
    ? ['branch', 'Host-runs', 'hosts'] : ['step', 'Failed', 'failed'];
  return `<table class="rpt-table">
    <thead><tr><th>Source</th><th>${second === 'branch' ? 'Branch' : 'Step'}</th><th>${head}</th></tr></thead>
    <tbody>${rows.map(r => `<tr>
        <td>${escHtml(r.source)}</td>
        <td>${escHtml(r[second])}</td>
        <td>${escHtml(String(r[field] ?? ''))}</td>
      </tr>`).join('')}</tbody>
  </table>`;
}

/* ── Chart configs (no DOM work) ─────────────────────────────────────── */

//: Series colours: the categorical order, and past four the rest folds into
//: the "Other" colour instead of cycling back round to a used one.
function rptSeriesColors(n) {
  const colors = [];
  for (let i = 0; i < n; i++) colors.push(RPT_SERIES_TOKENS[i] || '--st-skipped');
  return { colors, fold: n > RPT_SERIES_TOKENS.length };
}

//: 45 → "45s", 120 → "2m", 3600 → "1h", 5400 → "1.5h". Only hours keep a
//: decimal, and never a .0 on one.
function rptFormatSeconds(s) {
  const v = Number(s) || 0;
  if (v < 60) return `${Math.round(v)}s`;
  if (v < 3600) return `${Math.round(v / 60)}m`;
  const hours = v / 3600;
  return `${hours % 1 === 0 ? String(hours) : hours.toFixed(1)}h`;
}

//: Fold the tail into one "Other" series, so a report over forty automations
//: still draws four distinguishable colours. Counts add up; percentages do
//: not, so `mean` averages the tail's numbers per day (null where none has one).
function rptFoldedSeries(series, mean) {
  const rows = series || [];
  const { colors, fold } = rptSeriesColors(rows.length);
  const out = rows.slice(0, RPT_SERIES_TOKENS.length)
    .map((s, i) => ({ name: s.name, values: s.values, color: colors[i] }));
  if (fold) {
    const tail = rows.slice(RPT_SERIES_TOKENS.length);
    const len = Math.max(0, ...tail.map(s => (s.values || []).length));
    const values = Array.from({ length: len }, (_, i) => {
      const day = tail.map(s => (s.values || [])[i]).filter(v => v !== null && v !== undefined);
      if (!mean) return day.reduce((n, v) => n + v, 0);
      return day.length ? Math.round(day.reduce((n, v) => n + v, 0) * 10 / day.length) / 10 : null;
    });
    out.push({ name: mean ? 'Other (average)' : 'Other', values, color: '--st-skipped' });
  }
  return out;
}

function _rptLegend(display) {
  return { legend: { display, labels: { color: tok('--text-3'), boxWidth: 10 } } };
}

const RPT_AXIS_X = { ticks: { maxTicksLimit: 6, color: tok('--text-3') }, grid: { color: tok('--grid-line') } };

//: The charts each kind can draw, mirrored from the server's catalogue so a
//: widget built before a chart existed still draws the right thing. A days
//: widget asked for a pie draws nothing rather than a pie over dates.
const RPT_CHARTS = {
  success_rate: ['line', 'area', 'bar', 'table'],
  runs_per_day: ['stacked-bar', 'bar', 'line', 'area', 'table'],
  failures_by_step: ['bar', 'column', 'pie', 'doughnut', 'table'],
  branch_paths: ['bar', 'column', 'pie', 'doughnut', 'table'],
  duration: ['line', 'area', 'bar', 'table'],
  host_outcomes: ['table', 'pie', 'doughnut', 'column'],
  automation_fires: ['stacked-bar', 'bar', 'line', 'area', 'table'],
};

//: Which family each kind's data is, mirrored from the server's catalogue. A
//: days widget has one value per day; a rows widget has one row per record —
//: and host_outcomes is a rows widget whatever it draws, even though as a
//: table it lists records and as a chart it counts them.
const RPT_FAMILY = {
  success_rate: 'days',
  runs_per_day: 'days',
  failures_by_step: 'rows',
  branch_paths: 'rows',
  duration: 'days',
  host_outcomes: 'rows',
  automation_fires: 'days',
};

//: Outcomes in the order the tables and the pies list them, whatever order the
//: rows arrived in.
const RPT_OUTCOME_ORDER = ['ok', 'failed', 'not_applicable', 'skipped', 'pending'];

//: #rrggbb with an alpha on it, so an area's fill is the line's own colour
//: washed out. Anything else — already an rgba(), a var(), a named colour — is
//: returned untouched rather than guessed at.
function rptAlpha(color, alpha) {
  const hex = /^#([0-9a-f]{6})$/i.exec(String(color).trim());
  if (!hex) return color;
  const n = parseInt(hex[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

//: The report's saved widget specs with the grid's current places written into
//: them. A spec the grid does not carry — one added since the grid was drawn —
//: is returned as it came in, place and all.
function rptMergeLayout(specs, layout) {
  const byId = new Map((layout || []).map(l => [String(l.id), l]));
  return (specs || []).map((spec, i) => {
    const at = byId.get(String(i));
    if (!at) return { ...spec };
    return { ...spec, x: at.x, y: at.y, w: at.w, h: at.h };
  });
}

//: Which builder a widget's data has. The server's catalogue calls the days
//: family "days" and the rows family "rows"; the two differ by more than their
//: shape — host_outcomes as a table lists records, as a chart counts them.
function rptFamily(w) {
  const known = RPT_FAMILY[w.kind];
  if (known) return known;
  return Array.isArray((w.data || {}).rows) ? 'rows' : 'days';
}

//: The days family as named series, in the colours the legend and the tables
//: both use. Percentages are folded by average and counts by sum — see
//: rptFoldedSeries.
function rptDaySeries(w) {
  const d = w.data || {};
  const one = (name, values, color) => [{ name, values: values || [], color }];
  switch (w.kind) {
    case 'success_rate':
      return rptFoldedSeries(d.series, true);
    case 'automation_fires':
      return rptFoldedSeries(d.series);
    case 'runs_per_day':
      return [
        ...one('ok', d.ok, '--st-ok'),
        ...one('failed', d.failed, '--st-failed'),
        ...one('pending', d.pending, '--st-pending'),
      ];
    case 'duration':
      return [...one('median', d.median, '--oc-1'), ...one('p95', d.p95, '--oc-2')];
    default:
      return [];
  }
}

//: The rows family as (label, value) points: one per row, or — for outcomes —
//: one per outcome, counting its rows and leaving out the outcomes nobody has.
function rptRowPoints(w) {
  const rows = (w.data || {}).rows || [];
  if (w.kind === 'host_outcomes') {
    const counts = new Map();
    for (const r of rows) {
      const key = String(r.outcome);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    const names = [...RPT_OUTCOME_ORDER,
                   ...[...counts.keys()].filter(k => !RPT_OUTCOME_ORDER.includes(k))];
    return names.filter(k => counts.has(k)).map(k => ({ label: k, value: counts.get(k) }));
  }
  const pair = w.kind === 'branch_paths' ? ['branch', 'hosts'] : ['step', 'failed'];
  return rows.map(r => ({ label: `${r.source} · ${r[pair[0]]}`, value: r[pair[1]] }));
}

//: A pie is unreadable past a handful of wedges: the four biggest keep their
//: colours and everything else becomes one "Other".
function rptPieSlices(points) {
  const sorted = [...(points || [])].sort((a, b) => b.value - a.value);
  const head = sorted.slice(0, RPT_SERIES_TOKENS.length)
    .map((p, i) => ({ ...p, color: RPT_SERIES_TOKENS[i] }));
  const tail = sorted.slice(RPT_SERIES_TOKENS.length);
  if (tail.length) {
    head.push({ label: 'Other', value: tail.reduce((n, p) => n + (Number(p.value) || 0), 0),
                color: '--st-skipped' });
  }
  return head;
}

//: The value axis keeps what each kind measures: a rate is 0–100 with a %, a
//: duration reads as seconds, counts are whole numbers.
function _rptDayValueAxis(kind) {
  if (kind === 'success_rate') {
    return { min: 0, max: 100, ticks: { maxTicksLimit: 4, color: tok('--text-3'), callback: v => `${v}%` },
             grid: { color: tok('--grid-line') } };
  }
  if (kind === 'duration') {
    return { beginAtZero: true, ticks: { maxTicksLimit: 4, color: tok('--text-3'), callback: v => rptFormatSeconds(v) },
             grid: { color: tok('--grid-line') } };
  }
  return { beginAtZero: true, ticks: { maxTicksLimit: 4, color: tok('--text-3'), precision: 0 },
           grid: { color: tok('--grid-line') } };
}

//: Where the widget's two axis titles go. A days chart always has days along x
//: and its measure down y; a rows widget takes them from the table above, and
//: keeps them where they are when neither names its chart.
//: x_label titles the horizontal axis and y_label the vertical one, as drawn:
//: the server's default_labels has already turned them round for columns.
function _rptAxisTitles(w) {
  return { x: w.x_label, y: w.y_label };
}

//: Chart.js wants a real string where a title colour goes — a function there
//: is not a scriptable option and comes out black on Dark.
function _rptTitle(label) {
  return { display: !!label, text: label || '', color: cssVar('--text-3') };
}

//: A legend earns its rows: the widget decides, and left to us it appears with
//: more than one thing to name — or always on a pie, where the wedges carry no
//: axis to read their names off.
function _rptLegendDisplay(w, datasets, isPie) {
  if (w.legend === true || w.legend === false) return w.legend;
  return isPie || datasets.length > 1;
}

//: Everything a chart of these options draws from: its type, its datasets, its
//: axis titles, its legend. null is "this build cannot draw that combination".
function rptChartConfig(w) {
  const chart = w.chart;
  if (chart === 'table') return null;
  const allowed = RPT_CHARTS[w.kind] || [];
  if (!allowed.includes(chart)) return null;
  if (rptFamily(w) === 'days') return _rptDaysChart(w, chart);
  if (chart === 'pie' || chart === 'doughnut') return _rptPieChart(w, chart);
  return _rptRowsBars(w, chart);
}

function _rptDaysChart(w, chart) {
  const series = rptDaySeries(w);
  const stacked = chart === 'stacked-bar';
  const datasets = series.map((s) => {
    const color = tok(s.color);
    if (chart === 'line' || chart === 'area') {
      return {
        label: s.name, data: s.values || [], borderColor: color, borderWidth: 2,
        pointRadius: 0, tension: 0.25, spanGaps: true,
        fill: chart === 'area' ? 'origin' : false,
        // tok() hands back a function, which rptAlpha cannot see through;
        // the alpha is applied to the token's value on every draw instead.
        backgroundColor: chart === 'area' ? () => rptAlpha(cssVar(s.color), 0.25) : color,
      };
    }
    return { label: s.name, data: s.values || [], backgroundColor: color, borderWidth: 0,
             ...(stacked ? { stack: 'rpt' } : {}) };
  });
  const titles = _rptAxisTitles(w);
  return {
    type: chart === 'bar' || chart === 'stacked-bar' ? 'bar' : 'line',
    data: { labels: (w.data || {}).days || [], datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ...RPT_AXIS_X, ...(stacked ? { stacked: true } : {}),
             title: _rptTitle(titles.x) },
        y: { ..._rptDayValueAxis(w.kind), ...(stacked ? { stacked: true } : {}),
             title: _rptTitle(titles.y) },
      },
      plugins: _rptLegend(_rptLegendDisplay(w, datasets, false)),
    },
  };
}

function _rptRowsBars(w, chart) {
  const points = rptRowPoints(w);
  // indexAxis says which way the bars run; the titles follow the axes they
  // title, so a widget's "Step" stays under the categories however it is drawn.
  const horizontal = chart === 'bar';
  const titles = _rptAxisTitles(w);
  return {
    type: 'bar',
    data: {
      labels: points.map(p => p.label),
      datasets: [{
        label: w.title || '', data: points.map(p => p.value),
        backgroundColor: tok('--oc-1'), borderWidth: 0,
      }],
    },
    options: {
      indexAxis: horizontal ? 'y' : 'x',
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: horizontal
          ? { beginAtZero: true, ticks: { maxTicksLimit: 5, color: tok('--text-3'), precision: 0 },
              grid: { color: tok('--grid-line') }, title: _rptTitle(titles.x) }
          : { ticks: { autoSkip: false, color: tok('--text-3') }, grid: { display: false },
              title: _rptTitle(titles.x) },
        y: horizontal
          ? { ticks: { autoSkip: false, color: tok('--text-3') }, grid: { display: false },
              title: _rptTitle(titles.y) }
          : { beginAtZero: true, ticks: { maxTicksLimit: 5, color: tok('--text-3'), precision: 0 },
              grid: { color: tok('--grid-line') }, title: _rptTitle(titles.y) },
      },
      plugins: _rptLegend(_rptLegendDisplay(w, [{}], false)),
    },
  };
}

function _rptPieChart(w, chart) {
  const slices = rptPieSlices(rptRowPoints(w));
  // No scales at all: a pie has no axes to hang a title on, and Chart.js would
  // draw the empty ones anyway.
  return {
    type: chart,
    data: {
      labels: slices.map(s => s.label),
      datasets: [{
        label: w.title || '', data: slices.map(s => s.value),
        // One scriptable option per dataset: Chart.js indexes into an array
        // but never calls the functions inside one.
        backgroundColor: (ctx) => cssVar((slices[ctx.dataIndex] || slices[0]).color), borderWidth: 0,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: _rptLegend(_rptLegendDisplay(w, [], true)),
    },
  };
}

/* ── Wiring ──────────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('rpt-new-btn')?.addEventListener('click', () => openReportEditor(null));
  document.getElementById('rpt-cancel-btn')?.addEventListener('click', closeRptEditor);
  document.getElementById('rpt-cancel-btn-2')?.addEventListener('click', closeRptEditor);
  document.getElementById('rpt-editor-overlay')?.addEventListener('click', closeRptEditor);
  document.getElementById('rpt-save-btn')?.addEventListener('click', saveReport);
  document.getElementById('rpt-search')?.addEventListener('input', _renderReportList);
  document.getElementById('rpt-pb-filter')?.addEventListener('input',
    () => _renderRptPicks('rpt-pb-picks', 'rpt-pb', _rptPlaybooks));
  document.getElementById('rpt-auto-filter')?.addEventListener('input',
    () => _renderRptPicks('rpt-auto-picks', 'rpt-auto', _rptAutomations));
  // Typing only updates the draft: re-rendering the cards would throw the
  // cursor out of the box being typed in.
  document.getElementById('rpt-widgets')?.addEventListener('input', (e) => {
    const t = e.target;
    const field = ['rptWtitle', 'rptWx', 'rptWy'].find(k => k in t.dataset);
    if (!field) return;
    const d = _rptDraft.widgets[Number(t.dataset[field])];
    if (!d) return;
    d[{ rptWtitle: 'title', rptWx: 'xLabel', rptWy: 'yLabel' }[field]] = t.value;
    _rptSchedulePreview();
  });
  // Every other control changes what the card shows, so the cards redraw.
  document.getElementById('rpt-widgets')?.addEventListener('change', (e) => {
    if (_rptWidgetChanged(e.target)) {
      _renderRptWidgets();
      _rptSchedulePreview();
    }
  });
  document.getElementById('rpt-widget-add')?.addEventListener('change', (e) => {
    const kind = e.target.value;
    if (!kind) return;
    if (_rptDraft.widgets.length < 12) _rptDraft.widgets.push(rptDraftFrom({ kind }, _rptCatalog));
    _renderRptWidgetAdd();
    _renderRptWidgets();
    _rptSchedulePreview();
  });
  document.getElementById('rpt-range')?.addEventListener('change', _rptSchedulePreview);
  // Delegated from the panel, so a refiltered checkbox list keeps its ticks.
  document.getElementById('rpt-editor-modal')?.addEventListener('change', (e) => {
    const box = e.target.closest('[data-rpt-src]');
    if (!box || !_rptDraft[box.dataset.rptSrc]) return;
    if (box.checked) _rptDraft[box.dataset.rptSrc].add(box.value);
    else _rptDraft[box.dataset.rptSrc].delete(box.value);
    // A chart can only narrow to sources the report still has.
    const kept = new Set(_rptPickedSources().map(x => x.id));
    for (const d of _rptDraft.widgets) d.sources = new Set([...d.sources].filter(id => kept.has(id)));
    _renderRptWidgets();
    _rptSchedulePreview();
  });

  delegateClick('[data-rpt-new]', () => openReportEditor(null));
  delegateClick('[data-rpt-open]', (el) => openReport(el.dataset.rptOpen));
  delegateClick('[data-rpt-edit]', (el) => openReportEditor(_rptReports.find(r => r.id === el.dataset.rptEdit)));
  delegateClick('[data-rpt-del]', (el) => deleteReport(el.dataset.rptDel));
  delegateClick('[data-rpt-back]', _showReportList);
  delegateClick('[data-rpt-view-edit]', () => openReportEditor(_rptReports.find(r => r.id === _rptOpenId)));
  delegateClick('[data-rpt-layout]', () => { _rptToggleLayout(); });
  delegateClick('[data-rpt-wup]', (el) => _moveRptWidget(Number(el.dataset.rptWup), -1));
  delegateClick('[data-rpt-wdown]', (el) => _moveRptWidget(Number(el.dataset.rptWdown), 1));
  delegateClick('[data-rpt-wdel]', (el) => {
    _rptDraft.widgets.splice(Number(el.dataset.rptWdel), 1);
    _renderRptWidgets();
    _rptSchedulePreview();
  });
  delegateClick('[data-rpt-wdup]', (el) => {
    const i = Number(el.dataset.rptWdup);
    const d = _rptDraft.widgets[i];
    if (!d || _rptDraft.widgets.length >= 12) return;
    // The copy is a new card: no place of its own yet, and its own sources.
    _rptDraft.widgets.splice(i + 1, 0, { ...d, place: null, sources: new Set(d.sources) });
    _renderRptWidgets();
    _rptSchedulePreview();
  });

});

/* The page fetches its reports each time it is shown. */
(function () {
  const previous = window.navigateTo;
  if (typeof previous !== 'function') return;
  window.navigateTo = function (pageName) {
    previous.apply(this, arguments);
    if (pageName !== 'reports') return;
    if (_rptOpenId !== null) _showReportList();
    loadReports();
  };
})();

function _moveRptWidget(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= _rptDraft.widgets.length) return;
  const rows = _rptDraft.widgets;
  [rows[i], rows[j]] = [rows[j], rows[i]];
  _renderRptWidgets();
  _rptSchedulePreview();
}

//: A select, box or radio on a chart's card, written into its draft row.
//: false when the control is not one of the card's (typing is handled apart).
function _rptWidgetChanged(t) {
  const ds = t.dataset;
  const at = (k) => _rptDraft.widgets[Number(ds[k])];
  if ('rptWkind' in ds) {
    const entry = _rptCatalog.find(c => c.kind === t.value);
    if (entry && at('rptWkind')) rptKindChanged(at('rptWkind'), entry);
  } else if ('rptWchart' in ds) {
    if (at('rptWchart')) at('rptWchart').chart = t.value;
  } else if ('rptWaxes' in ds) {
    if (at('rptWaxes')) at('rptWaxes').axes = t.checked;
  } else if ('rptWlegend' in ds) {
    if (at('rptWlegend')) at('rptWlegend').legend = t.value;
  } else if ('rptWsrcmode' in ds) {
    if (at('rptWsrcmode')) at('rptWsrcmode').sourceMode = t.value;
  } else if ('rptWsrc' in ds) {
    const d = at('rptWsrc');
    if (!d) return false;
    if (t.checked) d.sources.add(t.value);
    else d.sources.delete(t.value);
  } else {
    return false;
  }
  return true;
}
