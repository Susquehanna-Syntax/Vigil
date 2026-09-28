// vigil-playbook-flow.js
// Owns: the playbook flow — a tree of task steps and if/then/else branches.
//   * the editor (colour-coded step cards, branch lanes, a guided condition
//     builder) used by the playbook modal in vigil-playbooks.js;
//   * the read-only flow drawn on playbook cards and in run history, where it
//     also shows how many hosts reached each step and which way they went;
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

/* ── Rendering ────────────────────────────────────────────────────────── */

function _flowColorClass(color) { return 'fc-' + (FLOW_COLORS.includes(color) ? color : 'none'); }

// Read-only flow: playbook cards and run history. `stats` (optional) maps a
// step id to { total, ok, failed, skipped, not_applicable, pending } across the
// run's hosts, and `lanes` maps "<branch index path>" to { then, else } host
// counts — both drawn onto the tree when present.
function flowReadOnlyHtml(tree, stats) {
  let n = 0;
  const step = s => {
    n += 1;
    const st = stats && stats[s.sid];
    const counts = st ? ['ok', 'failed', 'not_applicable', 'skipped', 'pending']
      .filter(k => st[k]).map(k => `<span class="flow-count fs-${k}" title="${escAttr(k.replace('_', ' '))}">${st[k]} ${escHtml({ ok: 'ok', failed: 'failed', not_applicable: 'n/a', skipped: 'skipped', pending: 'waiting' }[k])}${k === 'failed' && s.onf === 'continue' ? ' · handled' : ''}</span>`).join('') : '';
    const flags = [s.ona === 'skip' ? 'n/a → go on' : '', s.onf === 'continue' ? 'failure → carry on' : ''].filter(Boolean);
    const outcome = s.outcome ? ` <span class="flow-outcome">ends as “${escHtml(s.outcome)}”</span>` : '';
    return `<div class="flow-step ro ${_flowColorClass(s.color)}${st && !st.total ? ' flow-unreached' : ''}">
      <span class="flow-num">${n}</span>
      <div class="flow-step-body">
        <div class="flow-step-title">${escHtml(s.name || s.def)}${s.risk && !stats ? ` <span class="risk-badge risk-${escHtml(s.risk)}">${escHtml(s.risk)}</span>` : ''}</div>
        <div class="flow-step-sub"><code>${escHtml(s.sid)}</code>${flags.map(f => ` <span class="flow-flag">${escHtml(f)}</span>`).join('')}${outcome}</div>
        ${counts ? `<div class="flow-counts">${counts}</div>` : ''}
      </div>
    </div>`;
  };
  const lane = (nodes, lanes, path) => nodes.map((node, i) => {
    if (node.kind !== 'if') return step(node);
    const here = path ? `${path}.${i}` : String(i);
    const took = lanes && node.bid && lanes[node.bid];
    const label = (text, cls, count) => `<div class="flow-lane-label ${cls}">${text}${took ? ` <span class="flow-lane-hosts">${count} host${count === 1 ? '' : 's'}</span>` : ''}</div>`;
    return `<div class="flow-branch ro">
      <div class="flow-if"><span class="flow-if-mark">if</span><code class="flow-cond-ro">${escHtml(node.cond)}</code></div>
      <div class="flow-lanes">
        <div class="flow-lane lane-then">${label('When true', 'then', took ? took.then : 0)}${lane(node.then, lanes, here + '.then')}</div>
        <div class="flow-lane lane-else">${label('Otherwise', 'else', took ? took.else : 0)}${node.else.length ? lane(node.else, lanes, here + '.else') : '<div class="flow-empty">nothing — carry on below</div>'}</div>
      </div>
    </div>`;
  }).join('');
  return `<div class="flow ro">${lane(tree, stats && stats.__lanes, '')}</div>`;
}

// The editor. `ctx` supplies what the editor needs from the page:
//   defName(id), defRisk(id), defOutputs(id) → [names], ineligible(id) → text|null
function flowEditorHtml(tree, ctx) {
  let n = 0;
  const seen = [];   // steps before the current point, for the condition builder
  const stepHtml = (s, lanePath, i) => {
    n += 1;
    seen.push(s);
    const at = `${lanePath}|${i}`;
    const why = ctx.ineligible(s.def);
    const nOv = Object.values(s.ov || {}).reduce((k, p) => k + Object.keys(p).length, 0);
    const swatches = ['', ...FLOW_COLORS].map(c => `<button type="button" class="flow-swatch ${_flowColorClass(c)}${(s.color || '') === c ? ' on' : ''}"
        data-flow-color="${escAttr(at)}" data-c="${escAttr(c)}" aria-label="${escAttr(c ? 'Colour ' + c : 'No colour')}" title="${escAttr(c || 'no colour')}"></button>`).join('');
    const siblings = flowLane(tree, lanePath);
    return `<div class="flow-step ${_flowColorClass(s.color)}">
      <span class="flow-num">${n}</span>
      <div class="flow-step-body">
        <div class="flow-step-title">${escHtml(ctx.defName(s.def))}
          <span class="risk-badge risk-${escHtml(ctx.defRisk(s.def))}">${escHtml(ctx.defRisk(s.def))}</span>
          ${why ? `<span class="bl-step-warn">${escHtml(why)}</span>` : ''}</div>
        <div class="flow-step-opts">
          <label class="flow-opt">Step id <input class="flow-sid" data-flow-sid="${escAttr(at)}" value="${escAttr(s.sid)}" maxlength="60" spellcheck="false"></label>
          <label class="flow-opt">Not applicable
            <select data-flow-ona="${escAttr(at)}">
              <option value="stop"${s.ona !== 'skip' ? ' selected' : ''}>stop here</option>
              <option value="skip"${s.ona === 'skip' ? ' selected' : ''}>go on</option>
            </select></label>
          <label class="flow-opt">If it fails
            <select data-flow-onf="${escAttr(at)}">
              <option value="stop"${s.onf !== 'continue' ? ' selected' : ''}>stop here</option>
              <option value="continue"${s.onf === 'continue' ? ' selected' : ''}>carry on</option>
            </select></label>
          <label class="flow-opt">Outcome <input class="flow-outcome-in" data-flow-outcome="${escAttr(at)}" value="${escAttr(s.outcome || '')}" maxlength="40" placeholder="e.g. Recovered"></label>
          <span class="flow-swatches" role="group" aria-label="Step colour">${swatches}</span>
        </div>
      </div>
      <div class="flow-step-btns">
        <button type="button" class="btn btn-lav btn-xs" data-flow-inputs="${escAttr(at)}">Inputs${nOv ? ' · ' + nOv : ''}</button>
        <button type="button" class="btn btn-sky btn-xs" data-flow-view="${escAttr(s.def)}">View</button>
        <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="-1" ${i === 0 ? 'disabled' : ''} aria-label="Move up">↑</button>
        <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="1" ${i === siblings.length - 1 ? 'disabled' : ''} aria-label="Move down">↓</button>
        <button type="button" class="btn btn-rose btn-xs" data-flow-rm="${escAttr(at)}">Remove</button>
      </div>
    </div>`;
  };
  const addRow = lanePath => `<div class="flow-add">
      <button type="button" class="btn btn-mint btn-xs" data-flow-add-step="${escAttr(lanePath)}">+ Task</button>
      <button type="button" class="btn btn-outline btn-xs" data-flow-add-if="${escAttr(lanePath)}">+ If / else</button>
    </div>`;
  const branchHtml = (b, lanePath, i) => {
    const at = `${lanePath}|${i}`;
    const here = lanePath ? `${lanePath}.${i}` : String(i);
    const before = seen.slice();
    const options = before.map(s => `<option value="${escAttr(s.sid)}">${escHtml(s.sid)} — ${escHtml(ctx.defName(s.def))}</option>`).join('');
    const siblings = flowLane(tree, lanePath);
    const builder = `<div class="flow-builder" data-flow-builder="${escAttr(at)}" hidden>
        <select data-fb-step>${options || '<option value="">no earlier steps</option>'}</select>
        <select data-fb-what></select>
        <select data-fb-op></select>
        <span data-fb-value-wrap></span>
        <button type="button" class="btn btn-sky btn-xs" data-fb-use>Use this</button>
      </div>`;
    return `<div class="flow-branch">
      <div class="flow-if">
        <span class="flow-if-mark">if</span>
        <input class="flow-cond" data-flow-cond="${escAttr(at)}" value="${escAttr(b.cond || '')}" placeholder="steps.check.result.count > 0" spellcheck="false">
        <button type="button" class="btn btn-lav btn-xs" data-flow-build="${escAttr(at)}" ${before.length ? '' : 'disabled title="Add a step before this branch first"'}>Build…</button>
        <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="-1" ${i === 0 ? 'disabled' : ''} aria-label="Move up">↑</button>
        <button type="button" class="btn btn-outline btn-xs" data-flow-mv="${escAttr(at)}" data-dir="1" ${i === siblings.length - 1 ? 'disabled' : ''} aria-label="Move down">↓</button>
        <button type="button" class="btn btn-rose btn-xs" data-flow-rm="${escAttr(at)}">Remove</button>
      </div>
      ${builder}
      <div class="flow-lanes">
        <div class="flow-lane lane-then"><div class="flow-lane-label then">When true</div>${laneHtml(b.then, here + '.then')}</div>
        <div class="flow-lane lane-else"><div class="flow-lane-label else">Otherwise</div>${laneHtml(b.else, here + '.else')}</div>
      </div>
    </div>`;
  };
  const laneHtml = (nodes, lanePath) =>
    nodes.map((node, i) => (node.kind === 'if' ? branchHtml(node, lanePath, i) : stepHtml(node, lanePath, i))).join('')
    + addRow(lanePath);
  const body = tree.length ? laneHtml(tree, '') : `<div class="muted-note">No steps yet — add the first task this playbook runs.</div>${addRow('')}`;
  return `<div class="flow">${body}</div>`;
}

// The analysis panel under the editor: paths and findings.
function flowAnalysisHtml(tree, ctx) {
  const steps = flowSteps(tree);
  if (!steps.length) return '';
  const paths = flowPaths(tree);
  const lengths = paths.map(p => p.length);
  const findings = flowFindings(tree);
  const pathList = paths.slice(0, 8).map((p, i) => `<li><span class="flow-path-n">${i + 1}</span>${p.map(s =>
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
    ${paths.length > 1 ? `<details class="flow-paths"><summary>Every path a host can take</summary><ol>${pathList}</ol>${paths.length > 8 ? `<div class="muted-note">…and ${paths.length - 8} more.</div>` : ''}</details>` : ''}
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
    ${flowReadOnlyHtml(tree, flowRunStats(run.tasks))}
  </div>`;
}
