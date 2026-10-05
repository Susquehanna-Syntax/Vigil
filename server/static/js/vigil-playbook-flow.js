// vigil-playbook-flow.js
// Owns: the playbook flow — a tree of task steps and if/then/else branches.
//   * the canvas: the flow laid out left to right as nodes and edges you can
//     pan and zoom — colour-coded steps, if nodes whose true / otherwise ports
//     fork into two bands, "+" on every edge to add a step there;
//   * the editor's inspector (a selected node's settings and a guided
//     condition builder) used by the playbook modal in vigil-playbooks.js;
//   * the read-only canvas on playbook cards and in run history, where it also
//     shows how many hosts reached each step and which way they went;
//   * flow analysis: every path through the playbook, and the mistakes worth
//     catching before it runs anywhere.
// Tree nodes: { kind: 'step', def, ov, ona, onf, sid, color }
//           | { kind: 'if', cond, then: [...], else: [...] }
// Depends on: vigil-utils.js (escHtml, escAttr).

const FLOW_COLORS = ['rose', 'peach', 'lemon', 'mint', 'sky', 'lavender'];
const FLOW_STATUS = ['ok', 'failed', 'not_applicable', 'skipped'];

/* ── Tree helpers ─────────────────────────────────────────────────────── */

// A lane is addressed by a path: '' is the top level, '2.then' the then-lane
// of the branch at index 2, '2.then.0.else' deeper still.
function flowLane(tree, lane) {
  let list = tree;
  if (!lane) return list;
  const parts = lane.split('.');
  for (let i = 0; i < parts.length; i += 2) list = list[+parts[i]][parts[i + 1]];
  return list;
}

function flowSteps(tree, out = []) {
  for (const n of tree) {
    if (n.kind === 'if') { flowSteps(n.then, out); flowSteps(n.else, out); } else out.push(n);
  }
  return out;
}

function flowNextSid(tree) {
  const used = new Set(flowSteps(tree).map(s => s.sid));
  let i = 1;
  while (used.has('s' + i)) i++;
  return 's' + i;
}

// The row a playbook API returns → the editor's tree.
function flowTreeFromRow(row) {
  const bySid = {};
  (row.steps || []).forEach(s => { bySid[s.step_id] = s; });
  const mk = s => ({ kind: 'step', def: String(s.definition_id), ov: s.params_override || {},
    ona: s.on_not_applicable || 'stop', onf: s.on_failure || 'stop', sid: s.step_id || '',
    color: s.color || '', outcome: s.outcome || '', name: s.definition_name, risk: s.risk });
  if (!row.flow) return (row.steps || []).map(mk);
  const walk = nodes => nodes.map(n => (n.step
    ? (bySid[n.step] ? mk(bySid[n.step]) : null)
    : { kind: 'if', bid: n.id, cond: n.if, then: walk(n.then || []), else: walk(n.else || []) })).filter(Boolean);
  return walk(row.flow);
}

// The editor's tree → the API's flow_steps.
function flowSerialize(tree) {
  return tree.map(n => (n.kind === 'if'
    ? { if: (n.cond || '').trim(), then: flowSerialize(n.then), else: flowSerialize(n.else) }
    : { definition_id: n.def, id: n.sid, params_override: n.ov || {},
        on_not_applicable: n.ona || 'stop', on_failure: n.onf || 'stop', color: n.color || '',
        outcome: (n.outcome || '').trim() }));
}

/* ── Analysis ─────────────────────────────────────────────────────────── */

// Every path a host can take: one list of steps per combination of branch
// outcomes. Capped, because nine nested branches are 512 paths and nobody
// reads that many.
function flowPaths(tree, cap = 64) {
  let paths = [[]];
  const walk = (nodes, acc) => {
    let current = acc;
    for (const n of nodes) {
      if (n.kind !== 'if') { current = current.map(p => [...p, n]); continue; }
      const thenPaths = walk(n.then, current.map(p => [...p]));
      const elsePaths = walk(n.else, current.map(p => [...p]));
      current = [...thenPaths, ...elsePaths].slice(0, cap);
    }
    return current;
  };
  paths = walk(tree, paths);
  return paths;
}

function _flowCondRefs(cond) {
  const refs = [];
  const re = /steps\.([A-Za-z][A-Za-z0-9_-]*)\.(status|result\.([A-Za-z0-9_]+))/g;
  let m;
  while ((m = re.exec(cond || ''))) refs.push({ sid: m[1], field: m[3] || null, status: m[2] === 'status' });
  return refs;
}

// The mistakes worth catching before a playbook runs anywhere. Each finding:
// { level: 'warn' | 'info', text }.
function flowFindings(tree) {
  const findings = [];
  const steps = flowSteps(tree);
  const bySid = Object.fromEntries(steps.map(s => [s.sid, s]));
  const testedForFailure = new Set();
  const branches = [];
  const walk = (nodes, arm) => nodes.forEach(n => {
    if (n.kind !== 'if') return;
    branches.push({ node: n, arm });
    walk(n.then, n); walk(n.else, n);
  });
  walk(tree, null);

  for (const { node } of branches) {
    const cond = (node.cond || '').trim();
    if (!cond) { findings.push({ level: 'warn', text: 'A branch has no condition yet.' }); continue; }
    if (!node.then.length) findings.push({ level: 'warn', text: `“if ${cond}” has nothing to run when it is true.` });
    for (const ref of _flowCondRefs(cond)) {
      const target = bySid[ref.sid];
      if (!target) { findings.push({ level: 'warn', text: `“if ${cond}” names ${ref.sid}, which is not a step in this playbook.` }); continue; }
      if (ref.status && /["']failed["']/.test(cond)) {
        testedForFailure.add(ref.sid);
        if (target.onf !== 'continue') {
          findings.push({ level: 'warn', text: `“if ${cond}” can never be true: when ${ref.sid} fails the playbook stops there. Set “If it fails” on ${ref.sid} to “carry on”.` });
        }
      }
      if (ref.status && /["']not_applicable["']/.test(cond) && target.ona !== 'skip') {
        findings.push({ level: 'warn', text: `“if ${cond}” can never be true: when ${ref.sid} is not applicable the playbook stops there. Set “Not applicable” on ${ref.sid} to “go on”.` });
      }
    }
  }
  for (const s of steps) {
    if (s.onf === 'continue' && !testedForFailure.has(s.sid)) {
      findings.push({ level: 'info', text: `${s.sid} carries on after a failure, but no branch checks “steps.${s.sid}.status == "failed"” — the failure is ignored.` });
    }
  }
  return findings;
}

/* ── Canvas ───────────────────────────────────────────────────────────── */
// The flow is drawn left to right on a canvas you can pan and zoom: steps are
// nodes, an if/else is a decision node whose "true" and "otherwise" ports fork
// into two bands that merge again at a small join. Layout is computed from the
// tree, so nobody drags nodes around and two people see the same picture.

const FL = { NW: 200, NH: 80, IW: 208, IH: 66, GX: 60, GY: 30, PW: 64, PH: 30, J: 12, PAD: 24 };

const FLOW_MIN_FIT = 0.6;

function _flowColorClass(color) { return 'fc-' + (FLOW_COLORS.includes(color) ? color : 'none'); }

const _FLOW_STATUS_WORDS = {
  '==': { ok: 'is ok', failed: 'failed', not_applicable: 'is not applicable', skipped: 'was skipped' },
  '!=': { ok: 'is not ok', failed: 'did not fail', not_applicable: 'is applicable', skipped: 'was not skipped' },
};

// A condition in words, for the decision node: steps.check.status == "failed"
// reads "check failed". The expression itself stays one hover away.
function flowCondWords(cond) {
  return (cond || '').trim()
    .replace(/steps\.([A-Za-z][\w-]*)\.status\s*(==|!=)\s*["'](\w+)["']/g,
      (m, sid, op, v) => `${sid} ${(_FLOW_STATUS_WORDS[op] || {})[v] || `${op === '==' ? 'is' : 'is not'} ${v}`}`)
    .replace(/steps\.([A-Za-z][\w-]*)\.result\.(\w+)/g, '$1 → $2')
    .replace(/steps\.([A-Za-z][\w-]*)\.status/g, '$1 status')
    .replace(/\s*==\s*/g, ' is ').replace(/\s*!=\s*/g, ' is not ')
    .replace(/\s*>=\s*/g, ' ≥ ').replace(/\s*<=\s*/g, ' ≤ ');
}

function _flMeasure(nodes) {
  const parts = nodes.map((n) => {
    if (n.kind !== 'if') return { w: FL.NW, up: FL.NH / 2, down: FL.NH / 2 };
    const t = _flMeasure(n.then);
    const e = _flMeasure(n.else);
    // An empty "otherwise" runs straight through; the true band sits above it.
    const thenY = e.empty ? -(FL.GY + t.down) : -(FL.GY / 2 + t.down);
    const elseY = e.empty ? 0 : FL.GY / 2 + e.up;
    const bodyW = Math.max(t.w, e.w, FL.GX);
    return { t, e, thenY, elseY, bodyW,
      w: FL.IW + FL.GX + bodyW + FL.GX + FL.J,
      up: Math.max(FL.IH / 2, -thenY + t.up), down: Math.max(FL.IH / 2, elseY + e.down) };
  });
  if (!parts.length) return { parts, w: 0, up: 12, down: 12, empty: true };
  return { parts,
    w: parts.reduce((k, p) => k + p.w, 0) + FL.GX * (parts.length - 1),
    up: Math.max(...parts.map(p => p.up)), down: Math.max(...parts.map(p => p.down)) };
}

// Place a lane's nodes with their left edge at x and their centre line at y.
// Every gap between two nodes is an edge that carries the "<lane>|<index>"
// a new node dropped there would take.
function _flPlace(nodes, m, x, y, lane, tone, out) {
  const ports = [];
  let cx = x;
  nodes.forEach((n, i) => {
    const pm = m.parts[i];
    const at = `${lane}|${i}`;
    if (n.kind !== 'if') {
      out.nodes.push({ type: 'step', node: n, at, x: cx, y: y - FL.NH / 2, w: FL.NW, h: FL.NH });
      ports.push({ in: { x: cx, y }, out: { x: cx + FL.NW, y }, sid: n.sid });
    } else {
      const here = lane ? `${lane}.${i}` : String(i);
      out.nodes.push({ type: 'if', node: n, at, x: cx, y: y - FL.IH / 2, w: FL.IW, h: FL.IH });
      const bx = cx + FL.IW + FL.GX;
      const jx = bx + pm.bodyW + FL.GX;
      out.nodes.push({ type: 'join', x: jx, y: y - FL.J / 2, w: FL.J, h: FL.J });
      [['then', pm.t, y + pm.thenY, { x: cx + FL.IW, y: y - 14 }],
        ['else', pm.e, y + pm.elseY, { x: cx + FL.IW, y: y + 14 }]].forEach(([side, lm, ly, port]) => {
        const list = n[side];
        const lanePath = `${here}.${side}`;
        const branch = { bid: n.bid, side };
        if (!list.length) {
          out.edges.push({ a: port, b: { x: jx, y }, tone: side, insert: `${lanePath}|0`, branch, empty: true });
          return;
        }
        // A shorter band is centred under the longer one, so both meet the join evenly.
        const p = _flPlace(list, lm, bx + (pm.bodyW - lm.w) / 2, ly, lanePath, side, out);
        out.edges.push({ a: port, b: p.first, tone: side, insert: `${lanePath}|0`, branch, toSid: p.firstSid });
        out.edges.push({ a: p.last, b: { x: jx, y }, tone: side, insert: `${lanePath}|${list.length}` });
      });
      ports.push({ in: { x: cx, y }, out: { x: jx + FL.J, y }, sid: null });
    }
    if (i > 0) out.edges.push({ a: ports[i - 1].out, b: ports[i].in, tone, insert: at, toSid: ports[i].sid });
    cx += pm.w + FL.GX;
  });
  return { first: ports[0].in, last: ports[ports.length - 1].out, firstSid: ports[0].sid };
}

// Tree → { nodes, edges, w, h } in canvas pixels.
function flowLayout(tree) {
  const out = { nodes: [], edges: [] };
  const m = _flMeasure(tree);
  const y = 0;
  const start = { type: 'start', x: FL.PAD, y: y - FL.PH / 2, w: FL.PW, h: FL.PH };
  out.nodes.push(start);
  const x0 = FL.PAD + FL.PW + FL.GX;
  let endX = x0;
  if (tree.length) {
    const p = _flPlace(tree, m, x0, y, '', 'main', out);
    out.edges.push({ a: { x: FL.PAD + FL.PW, y }, b: p.first, tone: 'main', insert: '|0', toSid: p.firstSid });
    endX = x0 + m.w + FL.GX;
    out.edges.push({ a: p.last, b: { x: endX, y }, tone: 'main', insert: `|${tree.length}` });
  } else {
    endX = x0 + FL.GX;
    out.edges.push({ a: { x: FL.PAD + FL.PW, y }, b: { x: endX, y }, tone: 'main', insert: '|0' });
  }
  out.nodes.push({ type: 'end', x: endX, y: y - FL.PH / 2, w: FL.PW, h: FL.PH });
  const top = Math.min(...out.nodes.map(n => n.y)) - FL.PAD;
  const bottom = Math.max(...out.nodes.map(n => n.y + n.h)) + FL.PAD;
  out.nodes.forEach(n => { n.y -= top; });
  out.edges.forEach(e => { e.a = { x: e.a.x, y: e.a.y - top }; e.b = { x: e.b.x, y: e.b.y - top }; });
  out.w = endX + FL.PW + FL.PAD;
  out.h = bottom - top;
  out.anchor = -top;   // the y of the Start → End line
  return out;
}

function _flEdgePath(a, b) {
  const dx = Math.max(28, (b.x - a.x) / 2);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

const _FLOW_COUNT_WORDS = { ok: 'ok', failed: 'failed', not_applicable: 'n/a', skipped: 'skipped', pending: 'waiting' };

function _flStepNodeHtml(ln, num, opts) {
  const s = ln.node;
  const { ctx, stats, selected } = opts;
  const name = ctx ? ctx.defName(s.def) : (s.name || s.def);
  const risk = ctx ? ctx.defRisk(s.def) : s.risk;
  const why = ctx ? ctx.ineligible(s.def) : null;
  const st = stats && stats[s.sid];
  const flags = [s.ona === 'skip' ? 'n/a: go on' : '', s.onf === 'continue' ? 'fails: carry on' : ''].filter(Boolean);
  const counts = st ? ['ok', 'failed', 'not_applicable', 'skipped', 'pending'].filter(k => st[k])
    .map(k => `<span class="flow-count fs-${k}">${st[k]} ${escHtml(_FLOW_COUNT_WORDS[k])}${k === 'failed' && s.onf === 'continue' ? ', handled' : ''}</span>`).join('') : '';
  const third = counts || (s.outcome ? `<span class="flow-outcome">ends as “${escHtml(s.outcome)}”</span>` : '')
    || (risk && !stats ? `<span class="risk-badge risk-${escHtml(risk)}">${escHtml(risk)}</span>` : '');
  const tip = [name, `id ${s.sid}`, ...flags, s.outcome ? `outcome: ${s.outcome}` : '', why || ''].filter(Boolean).join('\n');
  const cls = ['fcn', 'fcn-step', _flowColorClass(s.color), why ? 'fcn-bad' : '',
    st && !st.total ? 'fcn-unreached' : '', selected === ln.at ? 'is-sel' : ''].filter(Boolean).join(' ');
  const tag = ctx ? 'button type="button"' : 'div';
  return `<${tag} class="${cls}" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px"
      data-sid="${escAttr(s.sid)}"${ctx ? ` data-flow-sel="${escAttr(ln.at)}"` : ''} title="${escAttr(tip)}">
    <span class="fcn-row"><span class="flow-num">${num}</span><span class="fcn-name">${escHtml(name)}</span></span>
    <span class="fcn-row fcn-sub"><code>${escHtml(s.sid)}</code>${flags.map(f => `<span class="flow-flag">${escHtml(f)}</span>`).join('')}</span>
    <span class="fcn-row fcn-third">${third}</span>
  </${tag.split(' ')[0]}>`;
}

function _flIfNodeHtml(ln, opts) {
  const node = ln.node;
  const { ctx, stats, selected } = opts;
  const took = stats && stats.__lanes && node.bid ? stats.__lanes[node.bid] : null;
  const cond = (node.cond || '').trim();
  const cls = ['fcn', 'fcn-if', cond ? '' : 'fcn-bad', selected === ln.at ? 'is-sel' : ''].filter(Boolean).join(' ');
  const tag = ctx ? 'button type="button"' : 'div';
  const port = (side, label) => `<span class="fcn-port p-${side}">${label}${took ? ` <b>${took[side]}</b>` : ''}</span>`;
  return `<${tag} class="${cls}" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px"
      ${ctx ? `data-flow-sel="${escAttr(ln.at)}" ` : ''}title="${escAttr(cond ? `if ${cond}` : 'No condition yet')}">
    <span class="fcn-if-mark">if</span>
    <span class="fcn-cond">${cond ? escHtml(flowCondWords(cond)) : '<em>add a condition</em>'}</span>
    ${port('then', 'true')}${port('else', 'otherwise')}
  </${tag.split(' ')[0]}>`;
}

// The canvas markup. opts: { ctx } makes it the editor (nodes select, edges
// take "+"), { stats } draws a run onto it, `selected` rings a node, `height`
// sets the viewport's height in px.
function flowCanvasHtml(tree, opts = {}) {
  const lay = flowLayout(tree);
  const stats = opts.stats;
  const edit = !!opts.ctx;
  let num = 0;
  const nodes = lay.nodes.map((ln) => {
    if (ln.type === 'step') { num += 1; return _flStepNodeHtml(ln, num, opts); }
    if (ln.type === 'if') return _flIfNodeHtml(ln, opts);
    if (ln.type === 'join') return `<span class="fcn-join" style="left:${ln.x}px;top:${ln.y}px"></span>`;
    return `<span class="fcn-pill" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px">${ln.type === 'start' ? 'Start' : 'End'}</span>`;
  }).join('');
  const faded = (e) => {
    if (!stats) return false;
    if (e.branch && stats.__lanes && e.branch.bid && stats.__lanes[e.branch.bid]) return !stats.__lanes[e.branch.bid][e.branch.side];
    return !!(e.toSid && stats[e.toSid] && !stats[e.toSid].total);
  };
  const paths = lay.edges.map(e => `<path class="fce t-${e.tone}${faded(e) ? ' fce-off' : ''}" d="${_flEdgePath(e.a, e.b)}"/>`).join('');
  const adds = edit ? lay.edges.map(e => `<button type="button" class="fce-add t-${e.tone}" data-flow-insert="${escAttr(e.insert)}"
      style="left:${(e.a.x + e.b.x) / 2}px;top:${(e.a.y + e.b.y) / 2}px" aria-label="Add a step here" title="Add a task or an if / else here">+</button>`).join('') : '';
  // Tall enough to show the flow at the smallest scale a fit will use.
  const [lo, hi] = edit ? [320, 620] : [140, 420];
  const height = Math.round(Math.min(Math.max(lay.h * FLOW_MIN_FIT + 16, lo), opts.maxHeight || hi));
  return `<div class="fcv${edit ? ' fcv-edit' : ''}" style="height:${height}px" data-w="${lay.w}" data-h="${lay.h}" data-anchor="${lay.anchor}">
    <div class="fcv-world" style="width:${lay.w}px;height:${lay.h}px">
      <svg class="fcv-edges" width="${lay.w}" height="${lay.h}" aria-hidden="true">${paths}</svg>
      ${nodes}${adds}
    </div>
    <div class="fcv-tools">
      <button type="button" class="fcv-btn" data-fcv="out" aria-label="Zoom out" title="Zoom out">−</button>
      <button type="button" class="fcv-btn" data-fcv="in" aria-label="Zoom in" title="Zoom in">+</button>
      <button type="button" class="fcv-btn fcv-fit" data-fcv="fit" title="Fit the whole flow">Fit</button>
    </div>
    <div class="fcv-hint">Drag to pan · Ctrl + scroll to zoom</div>
  </div>`;
}

// Pan and zoom. `view` restores a previous { s, tx, ty } (the editor keeps its
// place across re-renders); without it the flow is fitted once it has a size.
// Returns the live view object.
function flowCanvasMount(vp, view) {
  if (!vp || vp.dataset.mounted) return null;
  vp.dataset.mounted = '1';
  const world = vp.querySelector('.fcv-world');
  const W = +vp.dataset.w;
  const H = +vp.dataset.h;
  const v = view && view.s ? view : { s: 1, tx: 0, ty: 0, moved: false };
  const apply = () => { world.style.transform = `translate(${v.tx}px,${v.ty}px) scale(${v.s})`; };
  const fit = () => {
    const r = vp.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    // Never smaller than FLOW_MIN_FIT, or the step names stop being readable:
    // a flow that is bigger than that starts at the Start node, and you pan.
    v.s = Math.max(FLOW_MIN_FIT, Math.min(1, (r.width - 24) / W, (r.height - 24) / H));
    v.tx = W * v.s > r.width ? 8 : (r.width - W * v.s) / 2;
    if (H * v.s > r.height) {
      // Too tall: centre the Start → End line, without leaving empty canvas at an edge.
      const centred = r.height / 2 - (+vp.dataset.anchor) * v.s;
      v.ty = Math.min(8, Math.max(r.height - H * v.s - 8, centred));
    } else {
      v.ty = (r.height - H * v.s) / 2;
    }
    apply();
    return true;
  };
  const zoomAt = (factor, cx, cy) => {
    const s = Math.max(0.3, Math.min(1.8, v.s * factor));
    v.tx = cx - (cx - v.tx) * (s / v.s);
    v.ty = cy - (cy - v.ty) * (s / v.s);
    v.s = s; v.moved = true;
    apply();
  };
  if (view && view.s) apply();
  else if (!fit()) {
    const ro = new ResizeObserver(() => { if (!v.moved && fit()) ro.disconnect(); });
    ro.observe(vp);
  }
  vp.querySelectorAll('[data-fcv]').forEach(b => b.addEventListener('click', () => {
    const r = vp.getBoundingClientRect();
    if (b.dataset.fcv === 'fit') { v.moved = false; fit(); return; }
    zoomAt(b.dataset.fcv === 'in' ? 1.2 : 1 / 1.2, r.width / 2, r.height / 2);
  }));
  vp.addEventListener('wheel', (e) => {
    const r = vp.getBoundingClientRect();
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      zoomAt(Math.exp(-e.deltaY * 0.0022), e.clientX - r.left, e.clientY - r.top);
    } else if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      // A sideways swipe pans; an up/down scroll still scrolls the page.
      e.preventDefault();
      v.tx -= e.deltaX; v.moved = true; apply();
    }
  }, { passive: false });
  let drag = null;
  vp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.closest('button, input, select, textarea, a')) return;
    e.preventDefault();
    const sel = window.getSelection && window.getSelection();
    if (sel) sel.removeAllRanges();
    document.body.classList.add('fcv-dragging');
    drag = { x: e.clientX, y: e.clientY, tx: v.tx, ty: v.ty };
    vp.setPointerCapture(e.pointerId);
    vp.classList.add('is-panning');
  });
  vp.addEventListener('pointermove', (e) => {
    if (!drag) return;
    v.tx = drag.tx + e.clientX - drag.x;
    v.ty = drag.ty + e.clientY - drag.y;
    v.moved = true;
    apply();
  });
  const stop = () => {
    drag = null;
    vp.classList.remove('is-panning');
    document.body.classList.remove('fcv-dragging');
  };
  vp.addEventListener('pointerup', stop);
  vp.addEventListener('pointercancel', stop);
  return v;
}

// Canvases drawn by pages that only show a flow (playbook cards, run detail)
// mount themselves as they appear.
if (typeof MutationObserver !== 'undefined') {
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      document.querySelectorAll('.fcv:not([data-mounted])').forEach(vp => flowCanvasMount(vp));
    });
  }).observe(document.documentElement, { childList: true, subtree: true });
}

// Read-only flow: playbook cards and run history. `stats` (optional) maps a
// step id to { total, ok, failed, skipped, not_applicable, pending } across the
// run's hosts, and `__lanes` maps a branch id to { then, else } host counts.
function flowReadOnlyHtml(tree, stats, maxHeight) {
  return flowCanvasHtml(tree, { stats, maxHeight });
}

// The editor canvas. `ctx` supplies what the editor needs from the page:
//   defName(id), defRisk(id), defOutputs(id) → [names], ineligible(id) → text|null
function flowEditorHtml(tree, ctx, selected) {
  return flowCanvasHtml(tree, { ctx, selected });
}

// Steps that come before the node at `at` in document order — the ones a
// branch there may test.
function flowStepsBefore(tree, at) {
  const before = [];
  let found = false;
  const walk = (nodes, lane) => nodes.forEach((n, i) => {
    if (found) return;
    if (`${lane}|${i}` === at) { found = true; return; }
    const here = lane ? `${lane}.${i}` : String(i);
    if (n.kind === 'if') { walk(n.then, `${here}.then`); walk(n.else, `${here}.else`); } else before.push(n);
  });
  walk(tree, '');
  return before;
}

// The panel beside the editor canvas: the selected node's settings.
function flowInspectorHtml(tree, at, ctx) {
  const node = at ? (() => {
    const [lane, i] = at.split('|');
    try { return flowLane(tree, lane)[+i]; } catch (e) { return null; }
  })() : null;
  if (!node) {
    return `<div class="fi-empty">
      <p><b>Select a step or an if</b> to change it.</p>
      <p>Press <span class="fi-plus">+</span> on any line to add a task or an if / else at that point.</p>
      <p>Each if forks the flow: the <span class="fi-then">true</span> band runs when its condition holds, the <span class="fi-else">otherwise</span> band when it doesn't, and both meet again at the dot.</p>
    </div>`;
  }
  const [lane, idx] = at.split('|');
  const siblings = flowLane(tree, lane);
  const i = +idx;
  const moves = `<div class="fi-actions">
      <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>Move earlier</button>
      <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="1" ${i === siblings.length - 1 ? 'disabled' : ''}>Move later</button>
      <button type="button" class="btn btn-rose btn-xs" data-flow-rm="${escAttr(at)}">Remove</button>
    </div>`;
  if (node.kind === 'if') {
    const before = flowStepsBefore(tree, at);
    const options = before.map(s => `<option value="${escAttr(s.sid)}">${escHtml(s.sid)}: ${escHtml(ctx.defName(s.def))}</option>`).join('');
    const inside = flowSteps(node.then).length + flowSteps(node.else).length;
    return `<div class="fi">
      <div class="fi-head"><span class="fcn-if-mark">if</span> Decision</div>
      <label class="fi-field">Condition
        <textarea class="flow-cond" rows="2" data-flow-cond="${escAttr(at)}" placeholder="steps.check.result.count > 0" spellcheck="false">${escHtml(node.cond || '')}</textarea></label>
      <div class="fi-words" data-flow-words>${node.cond ? escHtml(flowCondWords(node.cond)) : ''}</div>
      <div class="flow-builder" data-flow-builder="${escAttr(at)}">
        <div class="fi-sub">Build it from an earlier step</div>
        ${before.length ? `<select data-fb-step>${options}</select>
        <select data-fb-what></select>
        <div class="fb-line"><select data-fb-op></select><span data-fb-value-wrap></span></div>
        <button type="button" class="btn btn-sky btn-xs" data-fb-use>Use this condition</button>`
    : '<div class="muted-note">Add a step before this if first: a condition tests an earlier step.</div>'}
      </div>
      ${inside ? `<div class="muted-note">Removing this if also removes the ${inside} step${inside === 1 ? '' : 's'} inside it.</div>` : ''}
      ${moves}
    </div>`;
  }
  const s = node;
  const why = ctx.ineligible(s.def);
  const nOv = Object.values(s.ov || {}).reduce((k, p) => k + Object.keys(p).length, 0);
  const swatches = ['', ...FLOW_COLORS].map(c => `<button type="button" class="flow-swatch ${_flowColorClass(c)}${(s.color || '') === c ? ' on' : ''}"
      data-flow-color="${escAttr(at)}" data-c="${escAttr(c)}" aria-label="${escAttr(c ? 'Colour ' + c : 'No colour')}" title="${escAttr(c || 'no colour')}"></button>`).join('');
  return `<div class="fi">
    <div class="fi-head">${escHtml(ctx.defName(s.def))}
      <span class="risk-badge risk-${escHtml(ctx.defRisk(s.def))}">${escHtml(ctx.defRisk(s.def))}</span></div>
    ${why ? `<div class="bl-step-warn">${escHtml(why)}</div>` : ''}
    <label class="fi-field">Step id <input class="flow-sid" data-flow-sid="${escAttr(at)}" value="${escAttr(s.sid)}" maxlength="60" spellcheck="false"></label>
    <label class="fi-field">When not applicable
      <select data-flow-ona="${escAttr(at)}">
        <option value="stop"${s.ona !== 'skip' ? ' selected' : ''}>stop here</option>
        <option value="skip"${s.ona === 'skip' ? ' selected' : ''}>go on to the next step</option>
      </select></label>
    <label class="fi-field">If it fails
      <select data-flow-onf="${escAttr(at)}">
        <option value="stop"${s.onf !== 'continue' ? ' selected' : ''}>stop here</option>
        <option value="continue"${s.onf === 'continue' ? ' selected' : ''}>carry on, so an if can recover</option>
      </select></label>
    <label class="fi-field">Outcome <input class="flow-outcome-in" data-flow-outcome="${escAttr(at)}" value="${escAttr(s.outcome || '')}" maxlength="40" placeholder="e.g. Recovered"></label>
    <div class="fi-field">Colour <span class="flow-swatches" role="group" aria-label="Step colour">${swatches}</span></div>
    <div class="fi-actions">
      <button type="button" class="btn btn-lav btn-xs" data-flow-inputs="${escAttr(at)}">Inputs${nOv ? ' · ' + nOv : ''}</button>
      <button type="button" class="btn btn-sky btn-xs" data-flow-view="${escAttr(s.def)}">View task</button>
    </div>
    ${moves}
  </div>`;
}

// Hovering a path in the analysis lights it up on the canvas.
function flowWirePathHighlight(analysisEl, canvasEl) {
  if (!analysisEl || !canvasEl) return;
  analysisEl.querySelectorAll('[data-path-sids]').forEach((li) => {
    const sids = new Set(li.dataset.pathSids.split(' '));
    const on = () => canvasEl.querySelectorAll('.fcn-step').forEach(n => n.classList.toggle('fcn-dim', !sids.has(n.dataset.sid)));
    const off = () => canvasEl.querySelectorAll('.fcn-dim').forEach(n => n.classList.remove('fcn-dim'));
    li.addEventListener('mouseenter', on);
    li.addEventListener('mouseleave', off);
    li.addEventListener('focus', on);
    li.addEventListener('blur', off);
  });
}

// The analysis panel under the editor: paths and findings.
function flowAnalysisHtml(tree, ctx) {
  const steps = flowSteps(tree);
  if (!steps.length) return '';
  const paths = flowPaths(tree);
  const lengths = paths.map(p => p.length);
  const findings = flowFindings(tree);
  const pathList = paths.slice(0, 8).map((p, i) => `<li tabindex="0" data-path-sids="${escAttr(p.map(s => s.sid).join(' '))}"><span class="flow-path-n">${i + 1}</span>${p.map(s =>
    `<span class="flow-path-step ${_flowColorClass(s.color)}">${escHtml(s.sid)}</span>`).join('<span class="flow-path-arrow" aria-hidden="true">›</span>') || '<em>nothing runs</em>'}</li>`).join('');
  const risks = steps.map(s => ctx.defRisk(s.def));
  const top = risks.includes('high') ? 'high' : risks.includes('standard') ? 'standard' : 'low';
  return `<div class="flow-analysis">
    <div class="flow-analysis-head">
      <span><b>${steps.length}</b> step${steps.length === 1 ? '' : 's'}</span>
      <span><b>${paths.length}${paths.length >= 64 ? '+' : ''}</b> possible path${paths.length === 1 ? '' : 's'}</span>
      <span>a host runs <b>${Math.min(...lengths)}${Math.max(...lengths) !== Math.min(...lengths) ? '–' + Math.max(...lengths) : ''}</b> of them</span>
      <span>highest risk <span class="risk-badge risk-${top}">${top}</span></span>
    </div>
    ${findings.length ? `<ul class="flow-findings">${findings.map(f => `<li class="${f.level}">${escHtml(f.text)}</li>`).join('')}</ul>` : '<div class="flow-findings-ok">No problems found in this flow.</div>'}
    ${paths.length > 1 ? `<details class="flow-paths"><summary>Every path a host can take (hover one to light it up)</summary><ol>${pathList}</ol>${paths.length > 8 ? `<div class="muted-note">…and ${paths.length - 8} more.</div>` : ''}</details>` : ''}
  </div>`;
}

/* ── Run analysis ─────────────────────────────────────────────────────── */

const _FLOW_TASK_BUCKET = {
  completed: 'ok', failed: 'failed', rejected: 'failed', expired: 'failed',
  not_applicable: 'not_applicable', skipped: 'skipped',
  pending: 'pending', dispatched: 'pending', executing: 'pending',
};

// A playbook run's tasks → per-step host counts and, per branch, how many
// hosts went each way. A step "reached" a host when it ran or is running
// there; a skipped or never-released step did not.
function flowRunStats(tasks) {
  const stats = { __lanes: {} };
  const lanes = {};
  for (const t of tasks || []) {
    if (!t.step_ref) continue;
    const st = stats[t.step_ref] || (stats[t.step_ref] = { total: 0 });
    const bucket = _FLOW_TASK_BUCKET[t.state];
    if (bucket) st[bucket] = (st[bucket] || 0) + 1;
    const reached = bucket && bucket !== 'skipped' && t.state !== 'rejected';
    if (reached) st.total += 1;
    if (reached && t.branch) {
      const parts = t.branch.split('.');
      for (let i = 0; i + 1 < parts.length; i += 2) {
        const sets = lanes[parts[i]] || (lanes[parts[i]] = { then: new Set(), else: new Set() });
        sets[parts[i + 1]].add(t.host);
      }
    }
  }
  for (const [bid, sets] of Object.entries(lanes)) {
    stats.__lanes[bid] = { then: sets.then.size, else: sets.else.size };
  }
  return stats;
}

// The flow block at the top of a playbook run's detail.
function flowRunHtml(run) {
  const snap = run && run.flow_snapshot;
  if (!snap) return '';
  const tree = flowTreeFromRow({ steps: snap.steps, flow: snap.flow });
  const hosts = new Set((run.tasks || []).map(t => t.host)).size;
  return `<div class="flow-run">
    <div class="flow-run-head">How this run went on ${hosts} host${hosts === 1 ? '' : 's'}</div>
    ${flowReadOnlyHtml(tree, flowRunStats(run.tasks), 520)}
  </div>`;
}
