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
// or playbook names outcomes, by outcome. Clicking a slice or a legend row
// floats that group's host cards (`[data-host]` inside `scope`) to the top of
// their container and dims the rest; clicking it again restores the order.
function runSummaryHtml(summary) {
  if (!summary || !summary.hosts) return '';
  const states = donutHtml(stateSegments(summary.states), {
    center: summary.hosts === 1 ? 'host' : 'hosts', filterKind: 'state', title: 'Hosts by result' });
  const outcomes = summary.outcomes
    ? donutHtml(outcomeSegments(summary.outcomes), { center: 'outcomes', filterKind: 'outcome', title: 'Where hosts ended up' })
    : '';
  return `<div class="run-summary">${states}${outcomes}</div>`;
}

function wireRunSummary(scope, summary, run) {
  if (!scope || !summary || !summary.per_host) return;
  let active = null;
  const controls = Array.from(scope.querySelectorAll('.donut-key'));

  const paint = () => {
    controls.forEach(b => b.setAttribute('aria-pressed',
      String(active === `${b.dataset.filterKind}:${b.dataset.filter}`)));
    scope.querySelectorAll('.donut-slice').forEach(slice =>
      slice.classList.toggle('is-active',
        !!active && active.startsWith(`${slice.dataset.sliceKind}:`)));
  };

  const hostMatches = (card, btn) => {
    const host = summary.per_host[card.dataset.host] || {};
    const value = btn.dataset.filterKind === 'state' ? host.state
      : (host.outcome || 'No outcome yet');
    return value === btn.dataset.filter
      || (btn.dataset.filter === '__other' && host.outcome);
  };

  // FLIP: measure, reorder in the DOM, measure again, then play the delta as a
  // slide so the cards visibly travel to the top instead of teleporting.
  const reorder = (containers, matched) => {
    const before = new Map();
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      containers.forEach(cards => cards.forEach(card =>
        before.set(card, card.getBoundingClientRect().top)));
    }
    containers.forEach(cards => {
      cards.forEach((card, i) => {
        if (!card.hasAttribute('data-order')) card.setAttribute('data-order', String(i));
      });
      const original = cards.slice()
        .sort((a, b) => Number(a.dataset.order) - Number(b.dataset.order));
      const top = matched ? original.filter(card => hostMatches(card, matched)) : [];
      const rest = matched ? original.filter(card => !top.includes(card)) : original;
      top.concat(rest).forEach(card => card.parentElement.appendChild(card));
      cards.forEach(card => card.classList.toggle('run-sort-dim',
        !!matched && !top.includes(card)));
    });
    before.forEach((was, card) => {
      const delta = was - card.getBoundingClientRect().top;
      if (!delta) return;
      card.style.transition = 'none';
      card.style.transform = `translateY(${delta}px)`;
      requestAnimationFrame(() => {
        card.style.transition = 'transform 260ms var(--ease-out-expo)';
        card.style.transform = '';
        card.addEventListener('transitionend', function settle() {
          card.style.transition = '';
          card.removeEventListener('transitionend', settle);
        });
      });
    });
  };

  const containers = () => {
    const groups = new Map();
    scope.querySelectorAll('[data-host]').forEach(card => {
      if (!groups.has(card.parentElement)) groups.set(card.parentElement, []);
      groups.get(card.parentElement).push(card);
    });
    return Array.from(groups.values());
  };

  const select = (btn) => {
    const key = `${btn.dataset.filterKind}:${btn.dataset.filter}`;
    const clear = active === key;
    active = clear ? null : key;
    paint();
    reorder(containers(), clear ? null : btn);
  };

  controls.forEach(btn => btn.addEventListener('click', () => select(btn)));
  // A slice is the legend row drawn as an arc: it borrows its donut's kind,
  // which only the legend rows carry.
  scope.querySelectorAll('.donut-slice').forEach(slice => {
    const kind = slice.closest('.donut-block')?.querySelector('.donut-key')?.dataset.filterKind;
    if (!kind) return;
    slice.dataset.sliceKind = kind;
    slice.addEventListener('click', () => {
      const btn = controls.find(b => b.dataset.filterKind === kind
        && b.dataset.filter === slice.dataset.slice);
      if (btn) select(btn);
    });
  });

  if (run) {
    scope.querySelectorAll('[data-host-run]').forEach(el =>
      el.addEventListener('click', () => openHostRunDetail(run, el.dataset.hostRun)));
  }
}

function _hostRunWhen(iso) {
  return iso ? new Date(iso).toLocaleString() : '—';
}

function _hostRunDuration(dispatched, completed) {
  if (!dispatched || !completed) return '—';
  const secs = Math.max(0, Math.round((new Date(completed) - new Date(dispatched)) / 1000));
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function _hostRunTaskHtml(t) {
  const color = _TASK_STATE_COLORS[t.state] || 'var(--text-3)';
  const results = _stepResultsHtml(t.result_data, t.result_output);
  const output = (t.result_output || '').trim();
  const meta = [
    ['Requested by', t.requested_by_username || '—'],
    ['Risk', t.risk_level || '—'],
    ['TTL', `${t.ttl_seconds}s`],
    ['Created', _hostRunWhen(t.created_at)],
    ['Dispatched', _hostRunWhen(t.dispatched_at)],
    ['Completed', _hostRunWhen(t.completed_at)],
    ['Duration', _hostRunDuration(t.dispatched_at, t.completed_at)],
  ];
  if (t.step_ref) meta.push(['Playbook step', t.step_ref]);
  if (t.branch) meta.push(['Branch', t.branch]);
  return `
    <div class="host-run-task">
      <div class="host-run-head">
        <span class="host-run-step">${escHtml(t.step_label || t.action || '')}</span>
        <span class="host-run-action">${escHtml(t.action || '')}</span>
        <span class="host-run-state" style="color:${escAttr(color)}">${escHtml(TASK_STATE_LABELS[t.state] || t.state || '')}</span>
      </div>
      <dl class="host-run-meta">${meta.map(([k, v]) =>
        `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`).join('')}</dl>
      <div class="host-run-k">Params</div>
      <pre class="host-run-json">${escHtml(JSON.stringify(t.params ?? {}, null, 2))}</pre>
      ${results ? `<div class="host-run-results">${results}</div>` : ''}
      <div class="host-run-k">Output</div>
      <pre class="host-run-out">${output ? escHtml(output) : 'No output captured.'}</pre>
    </div>`;
}

// A host's name in a run's results opens this: every task the run sent that
// host, in step order.
function openHostRunDetail(run, hostId) {
  const tasks = (run.tasks || [])
    .filter(t => String(t.host) === String(hostId))
    .sort((a, b) => (a.step_order || 0) - (b.step_order || 0)
      || String(a.created_at || '').localeCompare(String(b.created_at || '')));
  const hostname = (tasks.find(t => t.host_hostname) || {}).host_hostname || String(hostId);

  const m = mountModal('host-run-detail', { wide: true });
  m.setBody(`
    <div class="modal-title"><span id="host-run-title"></span>
      <button class="modal-close" id="hrd-x" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    ${tasks.map(_hostRunTaskHtml).join('')}
    <div class="confirm-actions"><button class="btn btn-outline btn-sm" id="hrd-close">Close</button></div>`);
  m.modal.querySelector('#host-run-title').textContent =
    `${hostname} · ${run.name_snapshot || 'Run'}`;
  m.modal.querySelector('#hrd-x').onclick = () => m.close();
  m.modal.querySelector('#hrd-close').onclick = () => m.close();
  requestAnimationFrame(m.open);
}
