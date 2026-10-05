// vigil-charts.js
// Owns: the donut used to summarise a run — hosts by end state (status
// colours, reserved) and by the outcomes an author named (a categorical order,
// validated for colour-vision deficiency in light and dark mode).
// The legend is the table view: every slice is named, counted and clickable.
// Depends on: vigil-utils.js (escHtml, escAttr).

// Status slots are fixed by meaning; outcome slots are fixed by order and fold
// into "Other" past four, never cycled.
const RUN_STATES = [
  { key: 'ok', label: 'ok', color: 'var(--st-ok)' },
  { key: 'failed', label: 'failed', color: 'var(--st-failed)' },
  { key: 'pending', label: 'waiting', color: 'var(--st-pending)' },
  { key: 'not_applicable', label: 'not applicable', color: 'var(--st-na)' },
  { key: 'skipped', label: 'skipped', color: 'var(--st-skipped)' },
];
const OUTCOME_COLORS = ['var(--oc-1)', 'var(--oc-2)', 'var(--oc-3)', 'var(--oc-4)'];

function stateSegments(states) {
  return RUN_STATES.filter(s => (states || {})[s.key])
    .map(s => ({ key: s.key, label: s.label, value: states[s.key], color: s.color }));
}

function outcomeSegments(outcomes) {
  const entries = Object.entries(outcomes || {});
  const none = entries.filter(([k]) => k === 'No outcome yet');
  const named = entries.filter(([k]) => k !== 'No outcome yet').sort((a, b) => b[1] - a[1]);
  const shown = named.slice(0, 4).map(([k, v], i) => ({ key: k, label: k, value: v, color: OUTCOME_COLORS[i] }));
  const rest = named.slice(4).reduce((n, [, v]) => n + v, 0);
  if (rest) shown.push({ key: '__other', label: 'Other', value: rest, color: 'var(--st-skipped)' });
  none.forEach(([k, v]) => shown.push({ key: k, label: 'no outcome yet', value: v, color: 'var(--st-na)' }));
  return shown;
}

// A donut with a 2px surface gap between slices and the total in the middle.
// `size` in px; `legend` false for the compact card version.
function donutHtml(segments, { size = 104, center = '', filterKind = '', legend = true, title = '' } = {}) {
  const total = segments.reduce((n, s) => n + s.value, 0);
  if (!total) return '';
  const r = 42;
  const circ = 2 * Math.PI * r;
  let offset = 0;
  const arcs = segments.map((s) => {
    const len = (s.value / total) * circ;
    const gap = segments.length > 1 ? Math.min(2, len / 3) : 0;
    const arc = `<circle class="donut-slice" cx="50" cy="50" r="${r}" fill="none" stroke="${s.color}" stroke-width="14"
      stroke-dasharray="${Math.max(len - gap, 0.01)} ${circ}" stroke-dashoffset="${-offset}"
      data-slice="${escAttr(s.key)}"><title>${escHtml(`${s.label}: ${s.value} of ${total}`)}</title></circle>`;
    offset += len;
    return arc;
  }).join('');
  const svg = `<svg class="donut" viewBox="0 0 100 100" width="${size}" height="${size}" role="img"
      aria-label="${escAttr(title || 'Summary')}: ${escAttr(segments.map(s => `${s.value} ${s.label}`).join(', '))}">
      <g transform="rotate(-90 50 50)">${arcs}</g>
      ${center ? `<text x="50" y="47" text-anchor="middle" class="donut-total">${escHtml(String(total))}</text>
      <text x="50" y="62" text-anchor="middle" class="donut-center">${escHtml(center)}</text>` : ''}
    </svg>`;
  if (!legend) return `<span class="donut-mini">${svg}</span>`;
  const rows = segments.map(s => `<button type="button" class="donut-key" data-filter-kind="${escAttr(filterKind)}" data-filter="${escAttr(s.key)}" aria-pressed="false">
      <span class="donut-swatch" style="background:${s.color}"></span>
      <span class="donut-label">${escHtml(s.label)}</span>
      <span class="donut-value">${s.value}</span>
      <span class="donut-pct">${Math.round((s.value / total) * 100)}%</span>
    </button>`).join('');
  return `<div class="donut-block">
    ${title ? `<div class="donut-title">${escHtml(title)}</div>` : ''}
    <div class="donut-row">${svg}<div class="donut-legend">${rows}</div></div>
  </div>`;
}

// The summary at the top of a run's detail: hosts by state and, when the task
// or playbook names outcomes, by outcome. Clicking a legend row shows only
// those hosts' cards (`[data-host]` inside `scope`); clicking it again shows all.
function runSummaryHtml(summary) {
  if (!summary || !summary.hosts) return '';
  const states = donutHtml(stateSegments(summary.states), {
    center: summary.hosts === 1 ? 'host' : 'hosts', filterKind: 'state', title: 'Hosts by result' });
  const outcomes = summary.outcomes
    ? donutHtml(outcomeSegments(summary.outcomes), { center: 'outcomes', filterKind: 'outcome', title: 'Where hosts ended up' })
    : '';
  return `<div class="run-summary">${states}${outcomes}</div>`;
}

function wireRunSummary(scope, summary) {
  if (!scope || !summary || !summary.per_host) return;
  let active = null;
  scope.querySelectorAll('.donut-key').forEach(btn => btn.addEventListener('click', () => {
    const key = `${btn.dataset.filterKind}:${btn.dataset.filter}`;
    active = active === key ? null : key;
    scope.querySelectorAll('.donut-key').forEach(b => b.setAttribute('aria-pressed',
      String(active === `${b.dataset.filterKind}:${b.dataset.filter}`)));
    scope.querySelectorAll('[data-host]').forEach(card => {
      const host = summary.per_host[card.dataset.host] || {};
      const value = btn.dataset.filterKind === 'state' ? host.state
        : (host.outcome || 'No outcome yet');
      card.hidden = !!active && value !== btn.dataset.filter
        && !(btn.dataset.filter === '__other' && host.outcome);
    });
  }));
}
