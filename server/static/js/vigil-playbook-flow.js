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
// Tree nodes: { kind: 'step', def, ov, inp, ona, onf, sid, color }
//           | { kind: 'if', cond, then: [...], elifs: [{ cond, then: [...] }], else: [...] }
//           | { kind: 'goto', gid, target, max }
// Depends on: vigil-utils.js (escHtml, escAttr).

const FLOW_COLORS = ['rose', 'peach', 'lemon', 'mint', 'sky', 'lavender'];
const FLOW_STATUS = ['ok', 'failed', 'not_applicable', 'skipped'];

/* ── Tree helpers ─────────────────────────────────────────────────────── */

// A lane is addressed by a path: '' is the top level, '2.then' the then-lane
// of the branch at index 2, '2.then.0.else' deeper still. An or-if lane is
// '2.elif1' — the same name the server's branch paths use.
function flowLane(list, lane) {
  let list_ = list;
  if (!lane) return list_;
  const parts = lane.split('.');
  for (let i = 0; i < parts.length; i += 2) list_ = flowArmList(list_[+parts[i]], parts[i + 1]);
  return list_;
}

// One lane of a node by arm key: 'then', 'else', or 'elif<k>' (k from 1).
function flowArmList(node, key) {
  if (key.startsWith('elif')) return (node.elifs || [])[+key.slice(4) - 1].then;
  return node[key];
}

// A decision's exits in the order they are judged: then, each or-if, else.
// Without or-ifs these are exactly then and else, so every walker that
// iterates them behaves as it always did.
function flowArms(node) {
  const elifs = node.elifs || [];
  const arms = [{ key: 'then', list: node.then, cond: node.cond }];
  elifs.forEach((arm, i) => arms.push({ key: `elif${i + 1}`, list: arm.then, cond: arm.cond }));
  arms.push({ key: 'else', list: node.else, cond: null });
  return arms;
}

// A decision's own condition is addressed by `at`; its k-th or-if's by
// `${at}#elif<k>`.
function flowCondGet(tree, addr) {
  const { node, elif } = _flowCondTarget(tree, addr);
  return ((elif ? elif.cond : node.cond) || '');
}

function flowCondSet(tree, addr, value) {
  const { node, elif } = _flowCondTarget(tree, addr);
  if (elif) elif.cond = value; else node.cond = value;
}

function _flowCondTarget(tree, addr) {
  const [at, part] = String(addr).split('#');
  const [lane, i] = at.split('|');
  const node = flowLane(tree, lane)[+i];
  if (!part) return { node, elif: null };
  return { node, elif: (node.elifs || [])[+part.slice(4) - 1] };
}

function flowSteps(tree, out = []) {
  for (const n of tree) {
    if (n.kind === 'if') { for (const arm of flowArms(n)) flowSteps(arm.list, out); } else if (n.kind === 'step') out.push(n);
  }
  return out;
}

// The lane path ("", "2.then", "2.then.0.else") part of a "<lane>|<index>" address.
function flowLaneOf(tree, at) {
  return String(at || '').split('|')[0];
}

// Every step with the lane path its lane is, using the layout's lane paths:
// '' for the top lane, "<index>.then" / "<index>.else" per branch.
function _flowStepsWithLanes(tree, lane = '', out = []) {
  tree.forEach((n, i) => {
    const here = lane ? `${lane}.${i}` : String(i);
    if (n.kind === 'if') {
      for (const arm of flowArms(n)) _flowStepsWithLanes(arm.list, `${here}.${arm.key}`, out);
    } else if (n.kind === 'step') out.push({ step: n, lane });
  });
  return out;
}

// The steps a go to sitting at "<lane>|<index>" may name: one in its own lane,
// or in the top lane or an outer branch's lane — never into a sibling branch.
// The same rule apps/playbooks/flow.py enforces, in document order.
function flowGotoTargets(tree, at) {
  const goLane = flowLaneOf(tree, at);
  return _flowStepsWithLanes(tree)
    .filter(e => e.step.sid && (e.lane === '' || e.lane === goLane || goLane.startsWith(`${e.lane}.`)))
    .map(e => e.step.sid);
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
    inp: s.inputs || {},
    ona: s.on_not_applicable || 'stop', onf: s.on_failure || 'stop', sid: s.step_id || '',
    color: s.color || '', outcome: s.outcome || '', name: s.definition_name, risk: s.risk });
  if (!row.flow) return (row.steps || []).map(mk);
  const walk = nodes => nodes.map(n => (n.step
    ? (bySid[n.step] ? mk(bySid[n.step]) : null)
    : n.goto
      ? { kind: 'goto', gid: n.id || '', target: n.goto || '', max: n.max || 3 }
      : { kind: 'if', bid: n.id, cond: n.if, then: walk(n.then || []),
        elifs: (n.elif || []).map(e => ({ cond: e.if, then: walk(e.then || []) })),
        else: walk(n.else || []) })).filter(Boolean);
  return walk(row.flow);
}

// The editor's tree → the API's flow_steps.
function flowSerialize(tree) {
  return tree.map(n => (n.kind === 'if'
    ? { if: (n.cond || '').trim(), then: flowSerialize(n.then),
      ...(n.elifs && n.elifs.length
        ? { elif: n.elifs.map(e => ({ if: (e.cond || '').trim(), then: flowSerialize(e.then) })) }
        : {}),
      else: flowSerialize(n.else) }
    : n.kind === 'goto'
      ? { goto: n.target, max: +n.max || 3 }
      : { definition_id: n.def, id: n.sid, params_override: n.ov || {},
        inputs: n.inp || {},
        on_not_applicable: n.ona || 'stop', on_failure: n.onf || 'stop', color: n.color || '',
        outcome: (n.outcome || '').trim() }));
}

/* ── Analysis ─────────────────────────────────────────────────────────── */

// Every path a host can take: one list of steps per combination of branch
// outcomes. Capped, because nine nested branches are 512 paths and nobody
// reads that many. A go to adds nothing to a path and the list does not
// follow jumps — the paths are the branch structure, not the run order.
function flowPaths(tree, cap = 64) {
  let paths = [[]];
  const walk = (nodes, acc) => {
    let current = acc;
    for (const n of nodes) {
      if (n.kind === 'if') {
        const splits = flowArms(n).map(arm => walk(arm.list, current.map(p => [...p])));
        current = splits.flat().slice(0, cap);
      } else if (n.kind === 'step') {
        current = current.map(p => [...p, n]);
      }
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
    for (const a of flowArms(n)) walk(a.list, n);
  });
  walk(tree, null);

  // Go tos, with the lane address each one sits at.
  const gotos = [];
  const gwalk = (nodes, lane) => nodes.forEach((n, i) => {
    const here = lane ? `${lane}.${i}` : String(i);
    if (n.kind === 'if') { for (const a of flowArms(n)) gwalk(a.list, `${here}.${a.key}`); }
    else if (n.kind === 'goto') gotos.push({ node: n, at: `${here}|${i}`, lane, i });
  });
  gwalk(tree, '');
  const stepSids = new Set(flowSteps(tree).map(s => s.sid));
  const gotoSids = new Set(gotos.map(g => g.node.gid));
  for (const g of gotos) {
    const target = (g.node.target || '').trim();
    if (!target) { findings.push({ level: 'warn', text: 'A go to has no target yet.' }); continue; }
    if (gotoSids.has(target) || !stepSids.has(target)) {
      findings.push({ level: 'warn', text: `A go to names ${target}, which is not a step in this playbook.` });
      continue;
    }
    if (!flowGotoTargets(tree, g.at).includes(target)) {
      findings.push({ level: 'warn', text: `A go to can only jump to ${target} from its own lane or a lane around it, not into another branch.` });
    }
    if (g.i !== flowLane(tree, g.lane).length - 1) {
      findings.push({ level: 'warn', text: 'Nothing after a go to can run: move it to the end of its lane.' });
    }
  }
  if (gotos.length > 8) findings.push({ level: 'warn', text: 'A playbook has at most 8 go to nodes.' });

  for (const { node } of branches) {
    const arms = flowArms(node);
    if (arms.length > 10) findings.push({ level: 'warn', text: 'A decision has at most 8 or-if exits.' });
    const seen = [];
    for (const arm of arms) {
      const label = arm.key === 'then' ? 'if' : arm.key === 'else' ? null : 'or if';
      if (!label) continue;  // otherwise runs when nothing above it held — nothing to check
      const cond = (arm.cond || '').trim();
      if (!cond) {
        findings.push({ level: 'warn', text: label === 'if' ? 'A branch has no condition yet.' : 'An or-if has no condition yet.' });
        seen.push('');
        continue;
      }
      if (seen.includes(cond)) {
        findings.push({ level: 'warn', text: `“or if ${cond}” can never be true: an earlier exit of the same decision already catches it.` });
      }
      seen.push(cond);
      if (!arm.list.length) {
        findings.push({ level: 'warn', text: `“${label} ${cond}” has nothing to run when it is true.` });
      }
      for (const ref of _flowCondRefs(cond)) {
        const target = bySid[ref.sid];
        if (!target) { findings.push({ level: 'warn', text: `“${label} ${cond}” names ${ref.sid}, which is not a step in this playbook.` }); continue; }
        if (ref.status && /["']failed["']/.test(cond)) {
          testedForFailure.add(ref.sid);
          if (target.onf !== 'continue') {
            findings.push({ level: 'warn', text: `“${label} ${cond}” can never be true: when ${ref.sid} fails the playbook stops there. Set “If it fails” on ${ref.sid} to “carry on”.` });
          }
        }
        if (ref.status && /["']not_applicable["']/.test(cond) && target.ona !== 'skip') {
          findings.push({ level: 'warn', text: `“${label} ${cond}” can never be true: when ${ref.sid} is not applicable the playbook stops there. Set “Not applicable” on ${ref.sid} to “go on”.` });
        }
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

const FL = { NW: 200, NH: 80, IW: 208, IH: 66, GX: 60, GY: 30, PW: 64, PH: 30, J: 12, PAD: 24,
  GW: 132, GH: 46 };

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

/* ── Condition rows ────────────────────────────────────────────────────── */
// An if's condition is a list of rows joined with and / or. Rows serialize to
// an ordinary expression, so the server and the agent never see the difference.

function flowCondRow(row) {
  const rhs = row.value == null ? '' : String(row.value);
  const lhs = row.what === 'status' ? `steps.${row.sid}.status` : `steps.${row.sid}.result.${row.what}`;
  if (row.what === 'status' || !/^(-?\d+(\.\d+)?|true|false|True|False)$/.test(rhs)) {
    return `${lhs} ${row.op} ${JSON.stringify(rhs)}`;
  }
  const literal = /^(true|True)$/.test(rhs) ? 'True' : /^(false|False)$/.test(rhs) ? 'False' : rhs;
  return `${lhs} ${row.op} ${literal}`;
}

function flowCondJoin(rows, mode) {
  const list = rows || [];
  if (!list.length) return '';
  const parts = list.map(flowCondRow);
  if (parts.length === 1) return parts[0];
  return parts.map(p => `(${p})`).join(mode === 'any' ? ' or ' : ' and ');
}

// Split at the top level — outside quotes and outside parentheses — on the
// words `and` / `or`. Returns [{ text, op }] in order; op is the separator
// that precedes the part ('' for the first).
function _flowCondParts(text) {
  const parts = [];
  let buf = '';
  let quote = '';
  let depth = 0;
  let op = '';
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (quote) {
      buf += c;
      if (c === '\\') { buf += text[i + 1] || ''; i += 2; continue; }
      if (c === quote) quote = '';
      i += 1;
      continue;
    }
    if (c === '"' || c === "'") { quote = c; buf += c; i += 1; continue; }
    if (c === '(') depth += 1;
    else if (c === ')') depth -= 1;
    if (depth === 0 && c === ' ') {
      const m = /^ (and|or) /.exec(text.slice(i));
      if (m) {
        parts.push({ text: buf, op });
        buf = '';
        op = m[1];
        i += m[0].length;
        continue;
      }
    }
    buf += c;
    i += 1;
  }
  parts.push({ text: buf, op });
  return parts;
}

// True when the whole string is one balanced parenthesised group.
function _flowCondWrapped(text) {
  if (!text.startsWith('(') || !text.endsWith(')')) return false;
  let depth = 0;
  let quote = '';
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quote) {
      if (c === '\\') { i += 1; continue; }
      if (c === quote) quote = '';
      continue;
    }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '(') depth += 1;
    else if (c === ')') {
      depth -= 1;
      if (depth === 0) return i === text.length - 1;
    }
  }
  return false;
}

// A quoted string, True/False, or a number — anything else is not a row value.
function _flowCondValue(raw) {
  const first = raw[0];
  if (first === '"' || first === "'") {
    if (raw.length < 2 || raw[raw.length - 1] !== first) return null;
    if (first === '"') {
      try { return JSON.parse(raw); } catch (e) { return null; }
    }
    let out = '';
    for (let i = 1; i < raw.length - 1; i += 1) {
      out += raw[i] === '\\' ? raw[++i] : raw[i];
    }
    return out;
  }
  if (raw === 'True') return 'true';
  if (raw === 'False') return 'false';
  if (/^-?\d+(\.\d+)?$/.test(raw)) return raw;
  return null;
}

const _FLOW_COND_ROW = /^steps\.([A-Za-z][\w-]*)\.(status|result\.(\w+))\s*(==|!=|>=|<=|>|<)\s*(.+)$/;

// Rows back out of an expression, or null when it is something rows cannot
// hold (not, mixed and/or, inputs.x, a bare name, ordering on status).
function flowCondParse(cond) {
  const text = (cond || '').trim();
  if (!text) return { mode: 'all', rows: [] };
  const parts = _flowCondParts(text);
  const ops = new Set(parts.slice(1).map(p => p.op));
  if (ops.size > 1) return null;
  const mode = ops.has('or') ? 'any' : 'all';
  const rows = [];
  for (const part of parts) {
    let p = part.text.trim();
    if (!p) return null;
    if (_flowCondWrapped(p)) p = p.slice(1, -1).trim();
    const m = _FLOW_COND_ROW.exec(p);
    if (!m) return null;
    const what = m[2] === 'status' ? 'status' : m[3];
    const op = m[4];
    if (what === 'status' && op !== '==' && op !== '!=') return null;
    const value = _flowCondValue(m[5].trim());
    if (value === null) return null;
    rows.push({ sid: m[1], what, op, value });
  }
  return { mode, rows };
}

// A condition in words, row by row: flowCondWords alone runs a multi-row
// condition into one sentence with its parentheses still in it, so a condition
// the rows can hold is read one row at a time and the joins put back.
function flowCondSummary(cond) {
  const parsed = flowCondParse(cond);
  if (parsed && parsed.rows.length >= 2) {
    return parsed.rows.map(r => flowCondWords(flowCondRow(r))).join(parsed.mode === 'any' ? ' or ' : ' and ');
  }
  return flowCondWords(cond);
}

// A decision's layout. With or-ifs (more than two exits) every arm gets a
// band of its own; two-way decisions keep the numbers they have always had.
// `belowY` / `aboveY` are the band's half-heights, measured from the band's
// centre line, so a band can be centred without clashing with its neighbours.
function _flIfMeasure(n) {
  const arms = flowArms(n);
  if (arms.length <= 2) {
    const t = _flMeasure(n.then);
    const e = _flMeasure(n.else);
    // An empty "otherwise" runs straight through; the true band sits above it.
    const thenY = e.empty ? -(FL.GY + t.down) : -(FL.GY / 2 + t.down);
    const elseY = e.empty ? 0 : FL.GY / 2 + e.up;
    const bodyW = Math.max(t.w, e.w, FL.GX);
    return { twoWay: true, t, e, thenY, elseY, bodyW,
      w: FL.IW + FL.GX + bodyW + FL.GX + FL.J,
      up: Math.max(FL.IH / 2, -thenY + t.up), down: Math.max(FL.IH / 2, elseY + e.down) };
  }
  const bands = arms.map(arm => _flMeasure(arm.list));
  // Half-heights of a band measured from its own centre line; an empty arm
  // is a straight line to the join, 24px tall, so its "+" is clickable.
  const above = (lm, o) => (lm.empty ? 12 : lm.up + o);
  const below = (lm, o) => (lm.empty ? 12 : lm.down - o);
  const ih = Math.max(FL.IH, 18 * arms.length + 12);
  // Bands stack in arm order with FL.GY between neighbours, each centred on
  // its own contents the way two-way bands are; the stack is then centred on
  // the decision's centre line.
  const off = [];
  let cursor = 0;   // top edge of the band being placed
  bands.forEach((lm, k) => {
    if (k) cursor += FL.GY;
    off.push(cursor + (lm.empty ? 12 : lm.up));
    cursor += lm.empty ? 24 : lm.up + lm.down;
  });
  const mid = cursor / 2;
  off.forEach((v, k) => { off[k] = v - mid; });
  const bodyW = Math.max(...bands.map(lm => lm.w), FL.GX);
  // The tallest node in a band may reach above / below the band's centre
  // line, and the canvas has to hold it.
  return { bands, off, ih, bodyW, w: FL.IW + FL.GX + bodyW + FL.GX + FL.J,
    up: Math.max(ih / 2, ...bands.map((lm, k) => above(lm, off[k]))),
    down: Math.max(ih / 2, ...bands.map((lm, k) => below(lm, off[k]))) };
}

function _flMeasure(nodes) {
  const parts = nodes.map((n) => {
    // A go to sits at the end of its lane and arcs to its target; the arc's
    // apex is accounted for by flowLayout when it works out the canvas top.
    if (n.kind === 'goto') return { w: FL.GW, up: FL.GH / 2, down: FL.GH / 2 };
    if (n.kind !== 'if') return { w: FL.NW, up: FL.NH / 2, down: FL.NH / 2 };
    const pm = _flIfMeasure(n);
    if (pm.twoWay) {
      return { if_: pm, t: pm.t, e: pm.e, thenY: pm.thenY, elseY: pm.elseY,
        bodyW: pm.bodyW, w: pm.w,
        up: Math.max(FL.IH / 2, -pm.thenY + pm.t.up), down: Math.max(FL.IH / 2, pm.elseY + pm.e.down) };
    }
    return { if_: pm, bands: pm.bands, off: pm.off, ih: pm.ih, w: pm.w, up: pm.up, down: pm.down };
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
    if (n.kind === 'goto') {
      out.nodes.push({ type: 'goto', node: n, at, x: cx, y: y - FL.GH / 2, w: FL.GW, h: FL.GH });
      ports.push({ in: { x: cx, y }, out: { x: cx + FL.GW, y }, sid: null });
    } else if (n.kind !== 'if') {
      out.nodes.push({ type: 'step', node: n, at, x: cx, y: y - FL.NH / 2, w: FL.NW, h: FL.NH });
      ports.push({ in: { x: cx, y }, out: { x: cx + FL.NW, y }, sid: n.sid });
    } else {
      const here = lane ? `${lane}.${i}` : String(i);
      if (!pm.if_.twoWay) {
        _flPlaceIfMulti(n, pm.if_, here, at, cx, y, lane, out);
        ports.push({ in: { x: cx, y }, out: { x: cx + pm.w, y }, sid: null });
      } else {
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
          // Nothing can be inserted after a go to, so its trailing edge has no "+".
          out.edges.push({ a: p.last, b: { x: jx, y }, tone: side, after: true,
            afterGoto: p.endsInGoto, insert: p.endsInGoto ? null : `${lanePath}|${list.length}` });
        });
        ports.push({ in: { x: cx, y }, out: { x: jx + FL.J, y }, sid: null });
      }
    }
    if (i > 0) out.edges.push({ a: ports[i - 1].out, b: ports[i].in, tone, insert: at, toSid: ports[i].sid });
    cx += pm.w + FL.GX;
  });
  return { first: ports[0].in, last: ports[ports.length - 1].out, firstSid: ports[0].sid,
    endsInGoto: nodes.length > 0 && nodes[nodes.length - 1].kind === 'goto' };
}

// A decision with or-ifs: one band per exit, stacked in arm order, each
// centred in the body width and meeting one join on the centre line.
function _flPlaceIfMulti(n, pm, here, at, cx, y, lane, out) {
  const arms = flowArms(n);
  out.nodes.push({ type: 'if', node: n, at, x: cx, y: y - pm.ih / 2, w: FL.IW, h: pm.ih });
  const bx = cx + FL.IW + FL.GX;
  const jx = bx + pm.bodyW + FL.GX;
  out.nodes.push({ type: 'join', x: jx, y: y - FL.J / 2, w: FL.J, h: FL.J });
  arms.forEach((arm, k) => {
    const tone = arm.key === 'then' ? 'then' : arm.key === 'else' ? 'else' : 'elif';
    const ly = y + pm.off[k];
    const port = { x: cx + FL.IW, y: y - (arms.length - 1) * 9 + 18 * k };
    const lanePath = `${here}.${arm.key}`;
    const branch = { bid: n.bid, side: arm.key };
    const lm = pm.bands[k];
    if (!arm.list.length) {
      // An empty exit still runs along its own band, so its line and "+"
      // never cross a neighbour's steps on the way to the join.
      const mid = { x: bx + pm.bodyW / 2, y: ly };
      out.edges.push({ a: port, b: mid, tone, insert: `${lanePath}|0`, branch, empty: true });
      out.edges.push({ a: mid, b: { x: jx, y }, tone, branch, empty: true, after: true, tail: true });
      return;
    }
    const p = _flPlace(arm.list, lm, bx + (pm.bodyW - lm.w) / 2, ly, lanePath, tone, out);
    out.edges.push({ a: port, b: p.first, tone, insert: `${lanePath}|0`, branch, toSid: p.firstSid });
    out.edges.push({ a: p.last, b: { x: jx, y }, tone, after: true,
      afterGoto: p.endsInGoto, insert: p.endsInGoto ? null : `${lanePath}|${arm.list.length}` });
  });
}

// Tree → { nodes, edges, jumps, w, h } in canvas pixels.
function flowLayout(tree) {
  const out = { nodes: [], edges: [], jumps: [] };
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
    // Nothing can be inserted after a go to, so its trailing edge has no "+".
    out.edges.push({ a: p.last, b: { x: endX, y }, tone: 'main', after: true,
      afterGoto: p.endsInGoto, insert: p.endsInGoto ? null : `|${tree.length}` });
  } else {
    endX = x0 + FL.GX;
    out.edges.push({ a: { x: FL.PAD + FL.PW, y }, b: { x: endX, y }, tone: 'main', insert: '|0' });
  }
  out.nodes.push({ type: 'end', x: endX, y: y - FL.PH / 2, w: FL.PW, h: FL.PH });
  // Each go to arcs above both its own node and its target.
  for (const ln of out.nodes) {
    if (ln.type !== 'goto') continue;
    const t = out.nodes.find(k => k.type === 'step' && k.node.sid === ln.node.target);
    if (!t) continue;
    ln.back = t.x < ln.x;
    out.jumps.push({ a: { x: ln.x + ln.w / 2, y: ln.y }, b: { x: t.x + t.w / 2, y: t.y },
      back: t.x < ln.x, at: ln.at, apexY: Math.min(ln.y, t.y) - FL.GY - 20 });
  }
  const top = Math.min(...out.nodes.map(n => n.y), ...out.jumps.map(j => j.apexY)) - FL.PAD;
  const bottom = Math.max(...out.nodes.map(n => n.y + n.h)) + FL.PAD;
  out.nodes.forEach(n => { n.y -= top; });
  out.edges.forEach(e => { e.a = { x: e.a.x, y: e.a.y - top }; e.b = { x: e.b.x, y: e.b.y - top }; });
  out.jumps.forEach(j => {
    j.a = { x: j.a.x, y: j.a.y - top }; j.b = { x: j.b.x, y: j.b.y - top }; j.apexY -= top;
  });
  out.w = endX + FL.PW + FL.PAD;
  out.h = bottom - top;
  out.anchor = -top;   // the y of the Start → End line
  return out;
}

function _flEdgePath(a, b) {
  const dx = Math.max(28, (b.x - a.x) / 2);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

// The dashed arc from a go to to its target, bowing above both nodes.
function _flJumpPath(a, b, apexY) {
  return `M${a.x},${a.y} C${a.x},${apexY} ${b.x},${apexY} ${b.x},${b.y}`;
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
  const arms = flowArms(node);
  // With or-ifs every exit gets its own port at its own height; the two-way
  // node keeps the markup — and the CSS's centring — it has always had.
  const ports = arms.length > 2
    ? arms.map((arm, k) => {
        const side = arm.key === 'then' ? 'then' : arm.key === 'else' ? 'else' : 'elif';
        const label = arm.key === 'then' ? 'true' : arm.key === 'else' ? 'otherwise' : `or if ${arm.key.slice(4)}`;
        return `<span class="fcn-port p-${side}" style="top:${ln.h / 2 - (arms.length - 1) * 9 + 18 * k - 8}px">${label}${took ? ` <b>${took[arm.key] || 0}</b>` : ''}</span>`;
      }).join('')
    : `${port('then', 'true')}${port('else', 'otherwise')}`;
  return `<${tag} class="${cls}" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px"
      ${ctx ? `data-flow-sel="${escAttr(ln.at)}" ` : ''}title="${escAttr(cond ? `if ${cond}` : 'No condition yet')}">
    <span class="fcn-if-mark">if</span>
    <span class="fcn-cond">${cond ? escHtml(flowCondSummary(cond)) : '<em>add a condition</em>'}</span>
    ${ports}
  </${tag.split(' ')[0]}>`;
}

// A go to: the end of a lane, and the step its arc lands on.
function _flGotoNodeHtml(ln, opts) {
  const node = ln.node;
  const { ctx, stats, selected } = opts;
  const target = (node.target || '').trim();
  const jumped = stats && stats[`@${node.gid}`];
  const cls = ['fcn', 'fcn-goto', target ? '' : 'fcn-bad', selected === ln.at ? 'is-sel' : ''].filter(Boolean).join(' ');
  const tag = ctx ? 'button type="button"' : 'div';
  const max = node.max || 3;
  const sub = jumped && jumped.jumped != null
    ? `${jumped.jumped} jumped`
    : ln.back ? `back, up to ${escHtml(max)} times` : 'jump ahead';
  return `<${tag} class="${cls}" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px"
      ${ctx ? `data-flow-sel="${escAttr(ln.at)}" ` : ''}title="${escAttr(target ? `go to ${target}` : 'Choose a step for this go to')}">
    <span class="fcn-row"><span class="fcn-goto-mark">go to</span><code>${escHtml(target || '?')}</code></span>
    <span class="fcn-row fcn-sub">${sub}</span>
  </${tag.split(' ')[0]}>`;
}

// A step by its id, or {} when the id names nothing (yet).
function _flowStepBySid(tree, sid) {
  return flowSteps(tree).find(s => s.sid === sid) || {};
}

// The go-to node sitting at "<lane>|<index>", or {} when nothing is there.
function _flowGotoNodeAt(tree, at) {
  try {
    const node = flowLane(tree, flowLaneOf(tree, at))[+String(at).split('|')[1]];
    return node && node.kind === 'goto' ? node : {};
  } catch (e) {
    return {};
  }
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
    if (ln.type === 'goto') return _flGotoNodeHtml(ln, opts);
    if (ln.type === 'join') return `<span class="fcn-join" style="left:${ln.x}px;top:${ln.y}px"></span>`;
    return `<span class="fcn-pill" style="left:${ln.x}px;top:${ln.y}px;width:${ln.w}px;height:${ln.h}px">${ln.type === 'start' ? 'Start' : 'End'}</span>`;
  }).join('');
  const faded = (e) => {
    if (!stats) return false;
    if (e.branch && stats.__lanes && e.branch.bid && stats.__lanes[e.branch.bid]) return !stats.__lanes[e.branch.bid][e.branch.side];
    return !!(e.toSid && stats[e.toSid] && !stats[e.toSid].total);
  };
  const paths = (lay.edges.map(e => `<path class="fce t-${e.tone}${faded(e) || e.afterGoto ? ' fce-off' : ''}" d="${_flEdgePath(e.a, e.b)}"/>`).join('')
    + lay.jumps.map(j => `<path class="fce fce-jump${j.back ? ' fce-back' : ''}" d="${_flJumpPath(j.a, j.b, j.apexY)}"/>`).join(''));
  const adds = edit ? lay.edges.filter(e => e.insert).map(e => `<button type="button" class="fce-add t-${e.tone}" data-flow-insert="${escAttr(e.insert)}"
      style="left:${(e.a.x + e.b.x) / 2}px;top:${(e.a.y + e.b.y) / 2}px" aria-label="Add a step here" title="Add a task or an if / else here">+</button>`).join('') : '';
  const jumpLabels = lay.jumps.map((j) => {
    const max = _flowGotoNodeAt(tree, j.at).max;
    return `<span class="fcj-label" style="left:${(j.a.x + j.b.x) / 2}px;top:${j.apexY}px">`
      + `${j.back ? `back ×${escHtml(max)}` : 'go to'}</span>`;
  }).join('');
  // Tall enough to show the flow at the smallest scale a fit will use.
  const [lo, hi] = edit ? [320, 620] : [140, 420];
  const height = Math.round(Math.min(Math.max(lay.h * FLOW_MIN_FIT + 16, lo), opts.maxHeight || hi));
  return `<div class="fcv${edit ? ' fcv-edit' : ''}" style="height:${height}px" data-w="${lay.w}" data-h="${lay.h}" data-anchor="${lay.anchor}">
    <div class="fcv-world" style="width:${lay.w}px;height:${lay.h}px">
      <svg class="fcv-edges" width="${lay.w}" height="${lay.h}" aria-hidden="true">${paths}</svg>
      ${nodes}${jumpLabels}${adds}
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
// run's hosts, and `__lanes` maps a branch id to its per-exit host counts —
// `{ then, else }` without or-ifs, plus `elif1` … once there are any.
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
    if (n.kind === 'if') { for (const arm of flowArms(n)) walk(arm.list, `${here}.${arm.key}`); } else if (n.kind === 'step') before.push(n);
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
      <p>Each if forks the flow: the <span class="fi-then">true</span> band runs when its condition holds, the <span class="fi-else">otherwise</span> band when it doesn't, and both meet again at the dot. Add an <span class="fi-elif">or if</span> exit for anything in between.</p>
    </div>`;
  }
  const [lane, idx] = at.split('|');
  const siblings = flowLane(tree, lane);
  const i = +idx;
  const moves = `<div class="fi-actions">
      <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>Move earlier</button>
      <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="1" ${i === siblings.length - 1 || (siblings[i + 1] && siblings[i + 1].kind === 'goto') ? 'disabled' : ''}>Move later</button>
      <button type="button" class="btn btn-rose btn-xs" data-flow-rm="${escAttr(at)}">Remove</button>
    </div>`;
  if (node.kind === 'goto') {
    const targets = flowGotoTargets(tree, at);
    const options = ['<option value="">Choose a step…</option>'].concat(targets.map(sid =>
      `<option value="${escAttr(sid)}"${node.target === sid ? ' selected' : ''}>${escHtml(sid)} — ${escHtml(ctx.defName(_flowStepBySid(tree, sid).def))}</option>`)).join('');
    return `<div class="fi">
      <div class="fi-head"><span class="fcn-goto-mark">go to</span> Jump</div>
      <label class="fi-field">Jump to
        <select data-flow-goto-target="${escAttr(at)}">${options}</select></label>
      <label class="fi-field">At most, going back
        <input type="number" min="1" max="10" data-flow-goto-max="${escAttr(at)}" value="${escAttr(node.max || 3)}"></label>
      <div class="muted-note">A step later in the playbook: hosts skip ahead to it, so branches meet.
        An earlier step: hosts go back and run from there again, at most this many times, then carry on.</div>
      <div class="fi-actions">
        <button type="button" class="btn btn-rose btn-xs" data-flow-rm="${escAttr(at)}">Remove</button>
      </div>
    </div>`;
  }
  if (node.kind === 'if') {
    const before = flowStepsBefore(tree, at);
    const arms = flowArms(node);
    const inside = arms.reduce((k, arm) => k + flowSteps(arm.list).length, 0);
    const builder = (addr, cond, label) => `<div class="flow-builder" data-flow-builder="${escAttr(addr)}">
        ${before.length ? `<div class="fi-sub">${label}</div>
        <div class="fb-mode" data-fb-mode-wrap>Run this exit when
          <select data-fb-mode><option value="all">all of these hold</option><option value="any">any of these holds</option></select>
        </div>
        <div class="fb-rows" data-fb-rows></div>
        <button type="button" class="btn btn-outline btn-xs" data-fb-add>Add a condition</button>
        <div class="muted-note" data-fb-raw hidden>This condition is written by hand, so it can only be edited as text above.
          <button type="button" class="btn btn-outline btn-xs" data-fb-reset>Start again with rows</button></div>`
    : '<div class="muted-note">Add a step before this if first: a condition tests an earlier step.</div>'}
      </div>`;
    const elifs = arms.slice(1, -1).map((arm, k) => `<div class="fi">
      <div class="fi-head"><span class="fi-elif">or if ${k + 1}</span> Another exit</div>
      <label class="fi-field">Expression
        <textarea class="flow-cond" rows="2" data-flow-cond="${escAttr(`${at}#elif${k + 1}`)}" placeholder="steps.check.result.count > 5" spellcheck="false">${escHtml(arm.cond || '')}</textarea></label>
      <div class="fi-words" data-flow-words="${escAttr(`${at}#elif${k + 1}`)}">${arm.cond ? escHtml(flowCondSummary(arm.cond)) : ''}</div>
      ${builder(`${at}#elif${k + 1}`, arm.cond, 'Conditions')}
      <div class="fi-actions">
        <button type="button" class="btn btn-rose btn-xs" data-flow-elif-rm="${escAttr(`${at}#elif${k + 1}`)}">Remove this exit</button>
      </div>
    </div>`).join('');
    return `<div class="fi">
      <div class="fi-head"><span class="fcn-if-mark">if</span> Decision</div>
      <label class="fi-field">Expression
        <textarea class="flow-cond" rows="2" data-flow-cond="${escAttr(at)}" placeholder="steps.check.result.count > 0" spellcheck="false">${escHtml(node.cond || '')}</textarea></label>
      <div class="fi-words" data-flow-words="${escAttr(at)}">${node.cond ? escHtml(flowCondSummary(node.cond)) : ''}</div>
      ${builder(at, node.cond, 'Conditions')}
      ${inside ? `<div class="muted-note">Removing this if also removes the ${inside} step${inside === 1 ? '' : 's'} inside it.</div>` : ''}
      ${moves}
    </div>
    ${elifs}
    <div class="fi-actions">
      <button type="button" class="btn btn-sky btn-xs" data-flow-elif-add="${escAttr(at)}" ${arms.length >= 10 ? 'disabled' : ''}>Add an or-if exit</button>
    </div>
    <div class="muted-note">Exits are checked in order: the first one that holds wins, and otherwise runs when none do.</div>`;
  }
  const s = node;
  const why = ctx.ineligible(s.def);
  const nOv = Object.values(s.ov || {}).reduce((k, p) => k + Object.keys(p).length, 0);
  const swatches = ['', ...FLOW_COLORS].map(c => `<button type="button" class="flow-swatch ${_flowColorClass(c)}${(s.color || '') === c ? ' on' : ''}"
      data-flow-color="${escAttr(at)}" data-c="${escAttr(c)}" aria-label="${escAttr(c ? 'Colour ' + c : 'No colour')}" title="${escAttr(c || 'no colour')}"></button>`).join('');
  /* Task inputs — how this step gets the values its task asks for. Rows only
     for a definition that declares inputs; the text may name a playbook input
     as ${{ inputs.<id> }} (see apps/playbooks/inputs.py). */
  const declared = ((ctx && ctx.defInputs) ? ctx.defInputs(s.def) : []) || [];
  let inputsHtml = '';
  if (declared.length) {
    const nodeInputs = s.inp || {};
    const pbIds = ((ctx && ctx.playbookInputs) ? ctx.playbookInputs() : []) || [];
    const rows = declared.map((ti) => {
      const id = ti.id || '';
      const val = nodeInputs[id] != null ? nodeInputs[id] : '';
      const ph = pbIds.indexOf(id) >= 0
        ? `from playbook input "${id}"`
        : (ti.default !== '' && ti.default != null ? `default: ${ti.default}` : 'required');
      const ins = pbIds.length
        ? `<select class="fci-insert" data-flow-inp-ins="${escAttr(at)}" data-inp-id="${escAttr(id)}" title="Insert a playbook input">
             <option value="">Insert…</option>${pbIds.map(p => `<option value="${escAttr(p)}">${escHtml(p)}</option>`).join('')}
           </select>`
        : '';
      return `<label class="fi-field">Task input ${escHtml(ti.label || id)}${ti.required ? ' (required)' : ''}
          <span class="fci-ctl">
            <input class="flow-sid" data-flow-inp="${escAttr(at)}" data-inp-id="${escAttr(id)}"
                   value="${escAttr(val)}" maxlength="500" spellcheck="false"
                   placeholder="${escAttr(ph)}">${ins}
          </span>
        </label>`;
    }).join('');
    inputsHtml = `<div class="fi-sub">Task inputs</div>${rows}`
      + (pbIds.length
        ? '<span class="muted-note">Insert… puts ${{ inputs.&lt;id&gt; }} in the field.</span>'
        : '');
  }

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
    ${inputsHtml}
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
    // A go to's own row is not a step; what it did is a count on the node.
    if (t.action === '_goto' && t.params && t.params.goto_id) {
      const g = stats[`@${t.params.goto_id}`] || (stats[`@${t.params.goto_id}`] = { jumped: 0 });
      if ((t.result_output || '').startsWith('[GOTO] jumped')) g.jumped += 1;
      continue;
    }
    if (!t.step_ref) continue;
    const st = stats[t.step_ref] || (stats[t.step_ref] = { total: 0 });
    const bucket = _FLOW_TASK_BUCKET[t.state];
    if (bucket) st[bucket] = (st[bucket] || 0) + 1;
    const reached = bucket && bucket !== 'skipped' && t.state !== 'rejected';
    if (reached) st.total += 1;
    if (reached && t.branch) {
      const parts = t.branch.split('.');
      for (let i = 0; i + 1 < parts.length; i += 2) {
        const sets = lanes[parts[i]] || (lanes[parts[i]] = {});
        const side = parts[i + 1];
        (sets[side] || (sets[side] = new Set())).add(t.host);
      }
    }
  }
  for (const [bid, sets] of Object.entries(lanes)) {
    // A two-way run always reports both keys, even the arm nobody took —
    // faded() reads a missing key as 0, and the port labels read these.
    const out = {};
    for (const [key, hosts] of Object.entries(sets)) out[key] = hosts.size;
    const twoWay = !Object.keys(out).some(k => k.startsWith('elif'));
    if (twoWay) { out.then = out.then || 0; out.else = out.else || 0; }
    stats.__lanes[bid] = out;
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
