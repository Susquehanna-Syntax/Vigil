"""Or-if exits in the editor, canvas, analysis and run counts (QA-45).

QA-44 put ``elif`` on the stored flow node; these are the browser-side
functions that read and write it. They are the whole contract between the
editor and ``apps/playbooks/flow.py``, so they run in `node`, taken from the
shipped file: a test that goes red means the shipped behaviour changed. A
decision without or-ifs must lay out, serialize and count exactly as it did
before, and that is pinned here with the numbers the two-way code produced
before any of this existed.
"""

import json
import re
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
FLOW_JS = ROOT / "static/js/vigil-playbook-flow.js"
PLAYBOOKS_JS = ROOT / "static/js/vigil-playbooks.js"
CSS = ROOT / "static/css/vigil.css"

# The shipped flow file uses the page's escapers; in node they stand in as
# identity functions so markup tests see the tree's own text.
_STUBS = ("global.escHtml = s => String(s == null ? '' : s);\n"
          "global.escAttr = s => String(s == null ? '' : s);\n")


@skipUnless(shutil.which("node"), "node is not installed")
class NodeFlowTests(SimpleTestCase):
    """Runs a body of JS against the whole shipped flow file, in node."""

    def _node(self, body: str):
        code = _STUBS + FLOW_JS.read_text() + "\n" + body
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True,
                             timeout=30, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)


class LayoutTests(NodeFlowTests):
    def test_two_way_layout_unchanged(self):
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const tree = [S('a'), { kind: 'if', bid: 'b1', cond: 'steps.a.status == "ok"',
  then: [S('b'), S('c')], else: [S('d')] },
  { kind: 'if', bid: 'b2', cond: 'steps.a.status == "ok"', then: [S('e')], else: [] }, S('f')];
const L = flowLayout(tree);
console.log(JSON.stringify({ w: L.w, h: L.h, anchor: Math.round(L.anchor), edges: L.edges.length,
  nodes: L.nodes.map(n => [n.type, n.node ? (n.node.sid || n.at) : null,
    Math.round(n.x), Math.round(n.y), n.w, n.h]) }));
""")
        self.assertEqual((got["w"], got["h"], got["anchor"], got["edges"]),
                         (2216, 253, 134, 13))
        self.assertEqual(got["nodes"], [
            ["start", None, 24, 119, 64, 30],
            ["step", "a", 148, 94, 200, 80],
            ["if", "|1", 408, 101, 208, 66],
            ["join", None, 1196, 128, 12, 12],
            ["step", "b", 676, 39, 200, 80],
            ["step", "c", 936, 39, 200, 80],
            ["step", "d", 806, 149, 200, 80],
            ["if", "|2", 1268, 101, 208, 66],
            ["join", None, 1796, 128, 12, 12],
            ["step", "e", 1536, 24, 200, 80],
            ["step", "f", 1868, 94, 200, 80],
            ["end", None, 2128, 119, 64, 30],
        ])

    def test_multi_exit_layout(self):
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const tree = [S('a'), { kind: 'if', bid: 'b1', cond: 'steps.a.status == "ok"',
  then: [S('b')],
  elifs: [{ cond: 'steps.a.status == "warn"', then: [S('c')] }, { cond: 'x', then: [] }],
  else: [S('d')] }, S('z')];
const L = flowLayout(tree);
const ifNode = L.nodes.find(n => n.type === 'if');
const mine = L.edges.filter(e => e.branch && e.branch.bid === 'b1' && !e.tail);
// Each arm's band centre is where its edge ends: the first step node's
// centre line, or the join's for an empty arm.
const bandY = side => {
  const edge = mine.find(e => e.branch.side === side);
  const hit = L.nodes.find(n => Math.round(n.x) === Math.round(edge.b.x)
    && Math.abs((n.y + n.h / 2) - edge.b.y) < 1);
  return Math.round(hit ? hit.y + hit.h / 2 : edge.b.y);
};
console.log(JSON.stringify({
  h: ifNode.h,
  arms: flowArms(tree[1]).map(a => a.key),
  tones: mine.map(e => e.tone),
  sides: mine.map(e => e.branch.side),
  bands: ['then', 'elif1', 'elif2', 'else'].map(bandY),
  emptyInsert: mine.find(e => e.branch.side === 'elif2').insert,
  ports: _flIfNodeHtml(ifNode, { ctx: {} }).match(/top:[^"]*">[^<]+/g),
}));
""")
        self.assertEqual(got["arms"], ["then", "elif1", "elif2", "else"])
        self.assertEqual(got["h"], 18 * 4 + 12)
        self.assertEqual(got["tones"], ["then", "elif", "elif", "else"])
        self.assertEqual(got["sides"], ["then", "elif1", "elif2", "else"])
        bands = got["bands"]
        self.assertEqual(bands, sorted(bands))
        self.assertEqual(len(set(bands)), 4, f"bands overlap: {bands}")
        self.assertEqual(got["emptyInsert"], "1.elif2|0")
        self.assertEqual(got["ports"], ['top:7px">true', 'top:25px">or if 1',
                                        'top:43px">or if 2', 'top:61px">otherwise'])

    def test_arms_that_need_more_room_do_not_collide(self):
        """A band holds its own contents: a tall `then` lane, with a nested if
        in it, does not squash the or-if bands under it."""
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const nested = { kind: 'if', bid: 'bx', cond: 'z', then: [S('x')], else: [S('y')] };
const tree = [{ kind: 'if', bid: 'b1', cond: 'c0', then: [S('a'), nested],
  elifs: [{ cond: 'c1', then: [S('e1')] }, { cond: 'c2', then: [] }], else: [S('o')] }];
const L = flowLayout(tree);
const mine = L.edges.filter(e => e.branch && e.branch.bid === 'b1' && !e.tail);
console.log(JSON.stringify({
  ports: mine.map(e => Math.round(e.a.y)),
  bandEdges: mine.map(e => [e.branch.side, Math.round(e.a.y), Math.round(e.b.y)]),
  inBands: L.nodes.filter(n => n.node && /^(0\\.(then|elif\\d|else))/.test(n.at || ''))
    .map(n => [n.at, Math.round(n.y), Math.round(n.y + n.h)]),
  top: Math.min(...L.nodes.map(n => n.y)) }));
""")
        self.assertEqual(got["ports"], sorted(got["ports"]))
        self.assertGreaterEqual(got["top"], 0)
        by_band = {}
        for at, top, bottom in got["inBands"]:
            by_band.setdefault(re.match(r"0[.](then|elif\d|else)", at).group(1), []).append((top, bottom))
        order = ["then", "elif1", "elif2", "else"]
        for a, b in pairwise(order):
            if a not in by_band or b not in by_band:
                continue   # an empty arm holds nothing to collide with
            self.assertLess(max(x[1] for x in by_band[a]),
                            min(x[0] for x in by_band[b]),
                            f"{a} intrudes into {b}: {got['inBands']}")
            # The band's centre line — where its edge leaves the decision —
            # sits in the gap above the band below it.
            centre = next(y for side, _a, y in got["bandEdges"] if side == a)
            self.assertLess(centre, min(x[0] for x in by_band[b]),
                            f"{a}'s line runs through {b}: {got['bandEdges']}")


class SerializeTests(NodeFlowTests):
    def test_serialize_round_trip(self):
        got = self._node("""
const row = { steps: [
    { step_id: 'a', definition_id: 'd1', definition_name: 'A' },
    { step_id: 'b', definition_id: 'd2', definition_name: 'B', branch: 'b1.then' },
    { step_id: 'c', definition_id: 'd3', definition_name: 'C', branch: 'b1.elif1' },
    { step_id: 'e', definition_id: 'd4', definition_name: 'E', branch: 'b1.elif2' },
    { step_id: 'd', definition_id: 'd5', definition_name: 'D', branch: 'b1.else' }],
  flow: [{ step: 'a' },
    { id: 'b1', if: "steps.a.status == 'ok'", then: [{ step: 'b' }],
      elif: [{ if: "steps.a.status == 'warn'", then: [{ step: 'c' }] },
             { if: "steps.a.status == 'x'", then: [{ step: 'e' }] }],
      else: [{ step: 'd' }] }] };
const twoWay = flowTreeFromRow({ steps: row.steps.slice(0, 2), flow: [
  { step: 'a' }, { id: 'b1', if: "steps.a.status == 'ok'", then: [{ step: 'b' }], else: [] }] });
console.log(JSON.stringify({
  roundTrip: flowSerialize(flowTreeFromRow(row)),
  twoWayKeys: Object.keys(flowSerialize(twoWay)[1]),
  elifs: flowTreeFromRow(row)[1].elifs.map(a => [a.cond, a.then.map(s => s.sid)]),
}));
""")
        decision = got["roundTrip"][1]
        self.assertEqual(list(decision), ["if", "then", "elif", "else"])
        self.assertEqual(decision["elif"], [
            {"if": "steps.a.status == 'warn'",
             "then": [{"definition_id": "d3", "id": "c", "params_override": {}, "inputs": {},
                       "on_not_applicable": "stop", "on_failure": "stop",
                       "color": "", "outcome": ""}]},
            {"if": "steps.a.status == 'x'",
             "then": [{"definition_id": "d4", "id": "e", "params_override": {}, "inputs": {},
                       "on_not_applicable": "stop", "on_failure": "stop",
                       "color": "", "outcome": ""}]},
        ])
        self.assertEqual(got["twoWayKeys"], ["if", "then", "else"])
        self.assertEqual(got["elifs"], [["steps.a.status == 'warn'", ["c"]],
                                        ["steps.a.status == 'x'", ["e"]]])

    def test_lane_addressing(self):
        got = self._node("""
const tree = [{ kind: 'step', def: 'd', sid: 'top' },
  { kind: 'if', bid: 'b1', cond: 'c0', then: [{ kind: 'step', def: 'd', sid: 'in_then' }],
    elifs: [{ cond: 'c1', then: [{ kind: 'step', def: 'd', sid: 'in_e1' }] },
            { cond: 'c2', then: [{ kind: 'step', def: 'd', sid: 'in_e2' }] }],
    else: [] },
  { kind: 'step', def: 'd', sid: 'after' }];
const out = { lane: flowLane(tree, '1.elif2').map(s => s.sid),
              get: flowCondGet(tree, '|1'), get2: flowCondGet(tree, '|1#elif2') };
flowCondSet(tree, '|1#elif2', 'changed');
flowCondSet(tree, '|1', 'main');
out.after = [tree[1].cond, tree[1].elifs[1].cond];
out.targets = flowGotoTargets(tree, '1.elif1|0');
console.log(JSON.stringify(out));
""")
        self.assertEqual(got["lane"], ["in_e2"])
        self.assertEqual((got["get"], got["get2"]), ("c0", "c2"))
        self.assertEqual(got["after"], ["main", "changed"])
        self.assertEqual(got["targets"], ["top", "in_e1", "after"])

    def test_paths_and_findings(self):
        got = self._node("""
const S = (sid) => ({ kind: 'step', def: 'd', sid });
const wide = [{ kind: 'step', def: 'd', sid: 'a' },
  { kind: 'if', bid: 'b1', cond: 'c0', then: [S('b')],
    elifs: [{ cond: 'c1', then: [S('c')] }, { cond: 'c2', then: [S('e')] }], else: [S('f')] }];
const dup = [{ kind: 'step', def: 'd', sid: 'a', onf: 'continue' },
  { kind: 'if', bid: 'b1', cond: 'steps.a.status == "ok"', then: [S('b')],
    elifs: [{ cond: 'steps.a.status == "ok"', then: [S('c')] },
            { cond: '   ', then: [S('e')] }], else: [] }];
console.log(JSON.stringify({
  paths: flowPaths(wide).map(p => p.map(s => s.sid)),
  dup: flowFindings(dup).map(f => f.text),
  twoWay: flowFindings([{ kind: 'step', def: 'd', sid: 'a' },
    { kind: 'if', bid: 'b1', cond: '', then: [], else: [] }]).map(f => f.text),
}));
""")
        self.assertEqual(got["paths"], [["a", "b"], ["a", "c"], ["a", "e"], ["a", "f"]])
        self.assertIn(
            "“or if steps.a.status == \"ok\"” can never be true: an earlier exit "
            "of the same decision already catches it.", got["dup"])
        self.assertIn("An or-if has no condition yet.", got["dup"])
        self.assertEqual(got["twoWay"], ["A branch has no condition yet."])

    def test_run_stats_per_arm(self):
        got = self._node("""
const tasks = [
  { step_ref: 'b', branch: 'b1.then', state: 'completed', host: 1 },
  { step_ref: 'c', branch: 'b1.elif1', state: 'completed', host: 2 },
  { step_ref: 'c2', branch: 'b1.elif1', state: 'completed', host: 3 },
  { step_ref: 'd', branch: 'b1.else', state: 'completed', host: 1 }];
const both = [
  { step_ref: 'b', branch: 'b1.then', state: 'completed', host: 1 },
  { step_ref: 'd', branch: 'b1.else', state: 'completed', host: 2 }];
const oneSided = [{ step_ref: 'd', branch: 'b1.else', state: 'completed', host: 1 }];
const elifOneSided = [{ step_ref: 'c', branch: 'b1.elif1', state: 'completed', host: 1 }];
console.log(JSON.stringify({
  wide: flowRunStats(tasks).__lanes,
  twoWay: flowRunStats(both).__lanes,
  oneSided: flowRunStats(oneSided).__lanes,
  elifOneSided: flowRunStats(elifOneSided).__lanes }));
""")
        self.assertEqual(got["wide"]["b1"], {"then": 1, "elif1": 2, "else": 1})
        self.assertEqual(got["twoWay"]["b1"], {"then": 1, "else": 1})
        self.assertEqual(got["oneSided"]["b1"], {"else": 1, "then": 0})
        # An or-if's counts appear only for the arms a run actually took.
        self.assertEqual(got["elifOneSided"]["b1"], {"elif1": 1})


class MarkupTests(SimpleTestCase):
    def test_inspector_markup(self):
        flow = FLOW_JS.read_text()
        playbooks = PLAYBOOKS_JS.read_text()
        for needle in ("data-flow-elif-add", "data-flow-elif-rm", "elifs",
                       "Add an or-if exit", "fi-elif", "flowCondSummary(arm.cond)",
                       "flowCondSet(", "flowCondGet(", "#elif"):
            self.assertIn(needle, flow)
        for needle in ("flowCondSet(", "flowCondGet(", "elifs", "_blElifAdd",
                       "_blElifRemove"):
            self.assertIn(needle, playbooks)
        # The QA-34 condition-row contract is untouched.
        for needle in ("data-fb-rows", "data-fb-mode", "data-fb-add", "data-fb-reset",
                       "data-fb-raw", "flowCondParse(", "flowCondJoin(",
                       "escHtml(flowCondSummary(node.cond))"):
            self.assertIn(needle, flow)
        for needle in ("[data-fb-rows]", "flowCondParse(", "flowCondJoin(",
                       "data-flow-builder", "flowCondSet(_editingTree",
                       "flowCondGet(_editingTree", "_blElifAdd", "_blElifRemove",
                       "data-flow-elif-add", "data-flow-elif-rm"):
            self.assertIn(needle, playbooks)

    def test_css_appended(self):
        css = CSS.read_text()
        self.assertGreater(len(css.splitlines()), 4566)
        for needle in (".modal {", ".fcn-if {", ".fce.t-elif", ".fcn-port.p-elif",
                       ".fce-add.t-elif", ".fi-elif", "QA-45: or-if exits"):
            self.assertIn(needle, css)
        block = css[css.index("QA-45: or-if exits"):]
        self.assertNotIn("border-left", block)
        self.assertNotIn("border-right", block)


class ReviewFixTests(NodeFlowTests):
    def test_an_exit_nobody_took_reads_zero(self):
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const tree = [S('a'), { kind: 'if', bid: 'b1', cond: 'x == 0', then: [S('b')],
  elifs: [{ cond: 'x == 1', then: [S('c')] }, { cond: 'x == 2', then: [S('d')] }], else: [S('e')] }];
const stats = flowRunStats([
  { step_ref: 'b', branch: 'b1.then', host: 'h1', state: 'completed' },
  { step_ref: 'c', branch: 'b1.elif1', host: 'h2', state: 'completed' }]);
console.log(JSON.stringify(flowReadOnlyHtml(tree, stats)));
""")
        self.assertNotIn("undefined", got)
        self.assertIn("or if 2 <b>0</b>", got)

    def test_inspector_markup_is_balanced(self):
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const tree = [S('a'), { kind: 'if', bid: 'b1', cond: '', then: [S('b')],
  elifs: [{ cond: '', then: [S('c')] }], else: [] }];
const ctx = { ineligible: () => null, defName: () => '', defs: [] };
console.log(JSON.stringify(flowInspectorHtml(tree, '|1', ctx)));
""")
        self.assertEqual(len(re.findall(r"<div\b", got)), got.count("</div>"))

    def test_an_empty_or_if_runs_along_its_own_band(self):
        got = self._node("""
const S = sid => ({ kind: 'step', sid, def: '1' });
const tree = [S('a'), { kind: 'if', bid: 'b1', cond: 'x == 0', then: [S('b')],
  elifs: [{ cond: 'x == 1', then: [S('c')] }, { cond: 'x == 2', then: [] }], else: [S('e')] }];
const L = flowLayout(tree);
const e = L.edges.find(e => e.insert === '1.elif2|0');
const mid = { x: (e.a.x + e.b.x) / 2, y: (e.a.y + e.b.y) / 2 };
const hits = L.nodes.filter(n => n.type === 'step'
  && mid.x >= n.x && mid.x <= n.x + n.w && mid.y >= n.y && mid.y <= n.y + n.h).map(n => n.node.sid);
console.log(JSON.stringify(hits));
""")
        self.assertEqual(got, [])
