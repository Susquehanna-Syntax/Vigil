// vigil-reports.js
// Owns: the Reports sub-panel on the Deployments page — the saved-report list,
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
// The builder's working state: the widget rows, and the ids ticked in the two
// checkbox lists.
let _rptDraft = { widgets: [], 'rpt-pb': new Set(), 'rpt-auto': new Set() };
let _rptOpenId = null;

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
  _destroyRptCharts();
  const view = document.getElementById('rpt-view');
  if (view) { view.hidden = true; view.replaceChildren(); }
  const toolbar = document.getElementById('rpt-toolbar');
  if (toolbar) toolbar.hidden = false;
  const list = document.getElementById('rpt-list');
  if (list) list.hidden = false;
  _renderReportList();
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
    widgets: report
      ? (report.widgets || []).map(w => ({ kind: w.kind, title: w.title || '' }))
      : RPT_DEFAULT_KINDS.map(kind => ({ kind, title: '' })),
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
}

function closeRptEditor() {
  document.getElementById('rpt-editor-modal')?.classList.remove('open');
  document.getElementById('rpt-editor-overlay')?.classList.remove('open');
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
  if (!_rptDraft.widgets.length) {
    box.innerHTML = '<div class="rpt-none muted">Pick at least one chart.</div>';
    return;
  }
  box.innerHTML = _rptDraft.widgets.map((w, i) => {
    const c = _rptCatalog.find(x => x.kind === w.kind) || { title: w.kind };
    return `<div class="rpt-widget-row">
      <span class="rpt-widget-kind">${escHtml(c.title)}</span>
      <input type="text" class="form-control" data-rpt-wtitle="${i}" maxlength="80"
        placeholder="${escAttr(c.title)}" value="${escAttr(w.title || '')}" aria-label="Chart title">
      <button class="btn btn-outline btn-xs" type="button" data-rpt-wup="${i}" aria-label="Move up"
        ${i === 0 ? 'disabled' : ''}>&#8593;</button>
      <button class="btn btn-outline btn-xs" type="button" data-rpt-wdown="${i}" aria-label="Move down"
        ${i === _rptDraft.widgets.length - 1 ? 'disabled' : ''}>&#8595;</button>
      <button class="btn btn-outline btn-xs" type="button" data-rpt-wdel="${i}" aria-label="Remove">&times;</button>
    </div>`;
  }).join('');
}

function _rptBodyFromForm() {
  const range = Number(document.getElementById('rpt-range').value);
  return {
    name: document.getElementById('rpt-name').value.trim(),
    description: document.getElementById('rpt-description').value.trim(),
    range_days: Number.isFinite(range) ? range : 30,
    widgets: _rptDraft.widgets.map(w => (w.title ? { kind: w.kind, title: w.title } : { kind: w.kind })),
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
    </div>
    ${data.truncated
      ? '<div class="rpt-note muted">Too much history to read at once — this covers the most recent runs only.</div>'
      : ''}
    <div class="rpt-grid">${widgets.map((w, i) => `
      <div class="rpt-card" data-rpt-card="${i}">
        <div class="rpt-card-head">
          <span class="rpt-card-title">${escHtml(w.title)}</span>
          <a class="btn btn-outline btn-xs" href="/api/v1/reports/${escAttr(id)}/data/${i}.csv" download>CSV</a>
        </div>
        <div class="rpt-card-body" data-rpt-body="${i}"></div>
      </div>`).join('')}</div>`;
  widgets.forEach((w, i) => {
    _drawRptWidget(view.querySelector(`[data-rpt-body="${i}"]`), w);
  });
}

function _drawRptWidget(body, w) {
  if (!body) return;
  if (w.chart === 'table') {
    body.innerHTML = _rptTableHtml(w.data);
    return;
  }
  if (!_rptHasData(w)) {
    body.innerHTML = '<div class="rpt-empty muted">Nothing ran in this range.</div>';
    return;
  }
  const d = w.data || {};
  let cfg = null;
  if (w.kind === 'success_rate' || w.kind === 'duration') {
    cfg = rptLineConfig(d, w.kind);
  } else if (w.kind === 'runs_per_day') {
    cfg = rptStackedConfig(d.days, [
      { name: 'ok', values: d.ok, color: '--st-ok' },
      { name: 'failed', values: d.failed, color: '--st-failed' },
      { name: 'pending', values: d.pending, color: '--st-pending' },
    ]);
  } else if (w.kind === 'automation_fires') {
    cfg = rptStackedConfig(d.days, rptFoldedSeries(d.series));
  } else if (w.kind === 'failures_by_step') {
    cfg = rptBarRowsConfig(d.rows, 'failed', row => `${row.source} · ${row.step}`);
  } else if (w.kind === 'branch_paths') {
    cfg = rptBarRowsConfig(d.rows, 'hosts', row => `${row.source} · ${row.branch}`);
  }
  if (!cfg) {
    body.innerHTML = '<div class="rpt-empty muted">This chart kind is not known to this build.</div>';
    return;
  }
  const holder = document.createElement('div');
  holder.className = 'rpt-chart';
  holder.innerHTML = '<canvas></canvas>';
  body.replaceChildren(holder);
  _rptCharts.push(new Chart(holder.querySelector('canvas').getContext('2d'), cfg));
}

const RPT_STATE_ACCENT = {
  ok: 'mint', not_applicable: 'lav', skipped: 'lemon',
  failed: 'rose', pending: 'peach',
};

function _rptTableHtml(data) {
  const rows = (data && data.rows) || [];
  if (!rows.length) return '<div class="rpt-empty muted">Nothing ran in this range.</div>';
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

//: Line charts on a category axis: the day names come from the server, so the
//: date adapter isn't involved and days with no runs still take up their slot.
function rptLineConfig(data, kind) {
  const d = data || {};
  const datasets = kind === 'duration'
    ? [
        { label: 'median', data: d.median || [], borderColor: tok('--oc-1'), borderWidth: 2, pointRadius: 0, tension: 0.25 },
        { label: 'p95', data: d.p95 || [], borderColor: tok('--oc-2'), borderWidth: 2, pointRadius: 0, tension: 0.25 },
      ]
    : rptFoldedSeries(d.series, true).map(s => ({
        label: s.name, data: s.values || [], borderColor: tok(s.color),
        borderWidth: 2, pointRadius: 0, tension: 0.25, spanGaps: true,
      }));
  const y = kind === 'success_rate'
    ? { min: 0, max: 100, ticks: { maxTicksLimit: 4, color: tok('--text-3'), callback: v => `${v}%` } }
    : { beginAtZero: true, ticks: { maxTicksLimit: 4, color: tok('--text-3'), callback: v => rptFormatSeconds(v) } };
  return {
    type: 'line',
    data: { labels: d.days || [], datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: RPT_AXIS_X, y },
      plugins: _rptLegend(datasets.length > 1),
    },
  };
}

function rptStackedConfig(days, stacks) {
  const list = stacks || [];
  return {
    type: 'bar',
    data: {
      labels: days || [],
      datasets: list.map(s => ({
        label: s.name, data: s.values || [], stack: 'rpt',
        backgroundColor: tok(s.color), borderWidth: 0,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ...RPT_AXIS_X, stacked: true },
        y: { beginAtZero: true, stacked: true,
             ticks: { maxTicksLimit: 4, color: tok('--text-3'), precision: 0 },
             grid: { color: tok('--grid-line') } },
      },
      plugins: _rptLegend(list.length > 1),
    },
  };
}

function rptBarRowsConfig(rows, valueKey, labelFn) {
  const list = rows || [];
  return {
    type: 'bar',
    data: {
      labels: list.map(labelFn),
      datasets: [{
        label: valueKey, data: list.map(r => r[valueKey]),
        backgroundColor: tok('--oc-1'), borderWidth: 0,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { beginAtZero: true, ticks: { maxTicksLimit: 5, color: tok('--text-3'), precision: 0 },
             grid: { color: tok('--grid-line') } },
        y: { ticks: { autoSkip: false, color: tok('--text-3') }, grid: { display: false } },
      },
      plugins: _rptLegend(false),
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
  document.getElementById('rpt-widgets')?.addEventListener('input', (e) => {
    const i = Number(e.target.dataset.rptWtitle);
    if (Number.isFinite(i) && _rptDraft.widgets[i]) _rptDraft.widgets[i].title = e.target.value;
  });
  document.getElementById('rpt-widget-add')?.addEventListener('change', (e) => {
    const kind = e.target.value;
    if (!kind) return;
    _rptDraft.widgets.push({ kind, title: '' });
    _renderRptWidgetAdd();
    _renderRptWidgets();
  });
  // Delegated from the panel, so a refiltered checkbox list keeps its ticks.
  document.getElementById('rpt-editor-modal')?.addEventListener('change', (e) => {
    const box = e.target.closest('[data-rpt-src]');
    if (!box || !_rptDraft[box.dataset.rptSrc]) return;
    if (box.checked) _rptDraft[box.dataset.rptSrc].add(box.value);
    else _rptDraft[box.dataset.rptSrc].delete(box.value);
  });

  delegateClick('[data-rpt-new]', () => openReportEditor(null));
  delegateClick('[data-rpt-open]', (el) => openReport(el.dataset.rptOpen));
  delegateClick('[data-rpt-edit]', (el) => openReportEditor(_rptReports.find(r => r.id === el.dataset.rptEdit)));
  delegateClick('[data-rpt-del]', (el) => deleteReport(el.dataset.rptDel));
  delegateClick('[data-rpt-back]', _showReportList);
  delegateClick('[data-rpt-view-edit]', () => openReportEditor(_rptReports.find(r => r.id === _rptOpenId)));
  delegateClick('[data-rpt-wup]', (el) => _moveRptWidget(Number(el.dataset.rptWup), -1));
  delegateClick('[data-rpt-wdown]', (el) => _moveRptWidget(Number(el.dataset.rptWdown), 1));
  delegateClick('[data-rpt-wdel]', (el) => {
    _rptDraft.widgets.splice(Number(el.dataset.rptWdel), 1);
    _renderRptWidgets();
  });

  document.querySelectorAll('#page-playbooks .sub-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      if (tab.dataset.subtab !== 'rpt-panel') return;
      if (_rptOpenId !== null) _showReportList();
      loadReports();
    });
  });
});

function _moveRptWidget(i, delta) {
  const j = i + delta;
  if (j < 0 || j >= _rptDraft.widgets.length) return;
  const rows = _rptDraft.widgets;
  [rows[i], rows[j]] = [rows[j], rows[i]];
  _renderRptWidgets();
}
