"""The editor, canvas and run history know about a go to (QA-37).

The tree functions are the whole browser-side contract with the flow the
server stores, so they run in `node`, taken from the shipped file: a test that
goes red means the shipped behaviour changed. The lane rule they enforce is the
one ``apps/playbooks/flow.py`` applies on save.
"""

import json
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.hosts.models import Host
from apps.tasks.models import Task, TaskRun
from apps.tasks.summary import run_summary

ROOT = Path(__file__).resolve().parents[2]
FLOW_JS = ROOT / "static/js/vigil-playbook-flow.js"
PLAYBOOKS_JS = ROOT / "static/js/vigil-playbooks.js"
CSS = ROOT / "static/css/vigil.css"

_STUBS = ("global.escHtml = s => String(s == null ? '' : s).replace(/&/g,'&amp;')"
          ".replace(/</g,'&lt;').replace(/>/g,'&gt;');"
          "global.escAttr = s => String(s == null ? '' : s).replace(/&/g,'&amp;')"
          ".replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\\"/g,'&quot;');\n")


@skipUnless(shutil.which("node"), "node is not installed")
class NodeFlowTests(SimpleTestCase):
    """Runs a body of JS against the shipped flow file, in node."""

    def _node(self, body: str):
        code = _STUBS + FLOW_JS.read_text() + "\n" + body
        out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30,
                             check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def _json(self, body: str):
        return json.loads(self._node(body))

    def test_row_round_trip(self):
        row = self._json("""
const tree = flowTreeFromRow({ steps: [
    { step_id: 'check', definition_id: 'd1', definition_name: 'Check' },
    { step_id: 'patch', definition_id: 'd2', definition_name: 'Patch' },
    { step_id: 'verify', definition_id: 'd3', definition_name: 'Verify' }],
  flow: [{ step: 'check' },
         { id: 'b1', if: "steps.check.status == 'ok'",
           then: [{ step: 'patch' }, { id: 'g1', goto: 'verify', max: 3 }], else: [] },
         { step: 'verify' }] });
const then = tree[1].then;
const g = then[then.length - 1];
const saved = flowSerialize(tree)[1].then[1];
console.log(JSON.stringify({ kind: g.kind, gid: g.gid, target: g.target, max: g.max,
  saved, keys: Object.keys(saved) }));
""")
        self.assertEqual(row["kind"], "goto")
        self.assertEqual((row["gid"], row["target"], row["max"]), ("g1", "verify", 3))
        self.assertEqual(row["saved"], {"goto": "verify", "max": 3})
        self.assertNotIn("definition_id", row["keys"])

    def test_goto_is_not_a_step(self):
        row = self._json("""
const tree = [{ kind: 'step', def: 'd1', sid: 'check' },
  { kind: 'if', cond: "steps.check.status == 'ok'",
    then: [{ kind: 'step', def: 'd2', sid: 'patch' },
           { kind: 'goto', gid: 'g1', target: 'verify', max: 3 }],
    else: [] },
  { kind: 'step', def: 'd3', sid: 'verify' }];
console.log(JSON.stringify({
  sids: flowSteps(tree).map(s => s.sid),
  paths: flowPaths(tree).map(p => p.map(s => s.sid)),
  next: flowNextSid(tree) }));
""")
        self.assertEqual(row["sids"], ["check", "patch", "verify"])
        self.assertTrue(all("verify" in p for p in row["paths"]), row["paths"])
        self.assertEqual(row["next"], "s1")

    def test_goto_targets(self):
        row = self._json("""
const tree = [{ kind: 'step', def: 'd1', sid: 'check' },
  { kind: 'if', cond: "steps.check.status == 'ok'",
    then: [{ kind: 'step', def: 'd2', sid: 'patch' },
           { kind: 'goto', gid: 'g1', target: 'verify', max: 3 }],
    else: [{ kind: 'goto', gid: 'g2', target: 'check', max: 3 }] },
  { kind: 'step', def: 'd3', sid: 'verify' }];
console.log(JSON.stringify({ then: flowGotoTargets(tree, '1.then|1'),
  lane: flowLaneOf(tree, '1.then|1'),
  elseLane: flowGotoTargets(tree, '1.else|0') }));
""")
        self.assertEqual(row["then"], ["check", "patch", "verify"])
        self.assertEqual(row["lane"], "1.then")
        self.assertNotIn("patch", row["elseLane"])
        self.assertIn("check", row["elseLane"])
        self.assertIn("verify", row["elseLane"])

    def test_findings(self):
        texts = self._json("""
const step = (sid) => ({ kind: 'step', def: 'd', sid });
const tree = [step('check'),
  { kind: 'if', cond: "steps.check.status == 'ok'",
    then: [{ kind: 'goto', gid: 'g1', target: '', max: 3 }],
    else: [{ kind: 'goto', gid: 'g2', target: 'patch', max: 3 }] },
  { kind: 'if', cond: "steps.check.status == 'ok'", then: [step('patch')], else: [] },
  { kind: 'goto', gid: 'g3', target: 'nope', max: 3 },
  { kind: 'goto', gid: 'g4', target: 'check', max: 3 },
  { kind: 'goto', gid: 'g5', target: 'check', max: 3 },
  { kind: 'goto', gid: 'g6', target: 'check', max: 3 },
  { kind: 'goto', gid: 'g7', target: 'check', max: 3 },
  { kind: 'goto', gid: 'g8', target: 'check', max: 3 },
  { kind: 'goto', gid: 'g9', target: 'check', max: 3 },
  step('verify')];
console.log(JSON.stringify(flowFindings(tree).map(f => f.text)));
""")
        self.assertIn("A go to has no target yet.", texts)
        self.assertTrue(any("A go to names nope, which is not a step" in t for t in texts), texts)
        self.assertTrue(any("A go to can only jump to patch from its own lane or a lane around it"
                            in t for t in texts), texts)
        self.assertTrue(any("Nothing after a go to can run: move it to the end of its lane."
                            in t for t in texts), texts)
        self.assertIn("A playbook has at most 8 go to nodes.", texts)

    def test_layout_draws_a_jump(self):
        row = self._json("""
const tree = [{ kind: 'step', def: 'd1', sid: 'check' },
  { kind: 'if', cond: "steps.check.status == 'ok'",
    then: [{ kind: 'step', def: 'd2', sid: 'patch' },
           { kind: 'goto', gid: 'g1', target: 'check', max: 2 }],
    else: [] },
  { kind: 'step', def: 'd3', sid: 'verify' }];
const lay = flowLayout(tree);
console.log(JSON.stringify({
  gotos: lay.nodes.filter(n => n.type === 'goto').length,
  jumps: lay.jumps,
  nodeYs: lay.nodes.map(n => n.y),
  deadEnds: lay.edges.filter(e => e.after && e.insert === null).length,
  plusAtLaneEnd: lay.edges.filter(e => e.insert === '1.then|2').length }));
""")
        self.assertEqual(row["gotos"], 1)
        self.assertEqual(len(row["jumps"]), 1)
        jump = row["jumps"][0]
        self.assertTrue(jump["back"])
        self.assertEqual(jump["at"], "1.then|1")
        self.assertTrue(all(y >= 0 for y in row["nodeYs"]), row["nodeYs"])
        self.assertGreaterEqual(jump["apexY"], 0)
        self.assertGreaterEqual(jump["a"]["y"], 0)
        self.assertGreaterEqual(jump["b"]["y"], 0)
        self.assertGreaterEqual(row["deadEnds"], 1)
        self.assertEqual(row["plusAtLaneEnd"], 0)

    def test_canvas_markup(self):
        html = self._node("""
const tree = [{ kind: 'step', def: 'd1', sid: 'check' },
  { kind: 'if', cond: "steps.check.status == 'ok'",
    then: [{ kind: 'step', def: 'd2', sid: 'patch' },
           { kind: 'goto', gid: 'g1', target: 'verify', max: 3 }],
    else: [] },
  { kind: 'step', def: 'd3', sid: 'verify' }];
const ctx = { defName: d => d, defRisk: () => 'low', ineligible: () => '',
              defInputs: () => [], playbookInputs: () => [] };
console.log(flowCanvasHtml(tree, { ctx }));
""")
        self.assertIn("fcn-goto", html)
        self.assertIn("fce-jump", html)
        self.assertIn("fcj-label", html)
        self.assertNotIn('data-flow-insert="1.then|2"', html)

    def test_canvas_escapes_a_target_and_numbers_only_steps(self):
        row = self._json("""
const tree = [{ kind: 'step', def: 'd1', sid: 'check' },
  { kind: 'goto', gid: 'g1', target: '<img src=x>', max: 3 }];
const html = flowCanvasHtml(tree, { ctx: { defName: d => d, defRisk: () => 'low',
  ineligible: () => '', defInputs: () => [], playbookInputs: () => [] } });
console.log(JSON.stringify({ html, nums: (html.match(/flow-num/g) || []).length }));
""")
        self.assertNotIn("<img src=x>", row["html"])
        self.assertIn("&lt;img src=x&gt;", row["html"])
        self.assertEqual(row["nums"], 1)

    def test_run_stats_count_jumps(self):
        stats = self._json("""
const tasks = [
  { action: '_goto', step_ref: '', params: { goto_id: 'g1' },
    result_output: '[GOTO] jumped back to patch (1/3)', state: 'completed', host: 'h1' },
  { action: '_goto', step_ref: '', params: { goto_id: 'g1' },
    result_output: '[GOTO] went back to patch 3 times already — going on',
    state: 'completed', host: 'h2' }];
console.log(JSON.stringify(flowRunStats(tasks)));
""")
        self.assertEqual(stats["@g1"]["jumped"], 1)


class SummaryGotoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("qa37", password="pw")
        self.host = Host.objects.create(hostname="h1", agent_token="tok-qa37",
                                        status=Host.Status.ONLINE, mode=Host.Mode.MANAGED)

    def test_summary_ignores_goto_rows(self):
        run = TaskRun.objects.create(name_snapshot="PB", requested_by=self.user,
                                     host_count=1, step_count=2)
        Task.objects.create(host=self.host, run=run, requested_by=self.user, action="_script",
                            params={}, state=Task.State.SKIPPED, step_ref="verify",
                            nonce="n-qa37-a")
        Task.objects.create(host=self.host, run=run, requested_by=self.user, action="_goto",
                            params={"goto": "verify", "goto_id": "g1", "max": 3},
                            state=Task.State.COMPLETED,
                            result_output="[GOTO] jumped ahead to verify", nonce="n-qa37-b")
        self.assertEqual(run_summary(run)["states"], {"skipped": 1})


class SourceWiringTests(SimpleTestCase):
    def test_editor_source_wiring(self):
        playbooks = PLAYBOOKS_JS.read_text()
        for needle in ('data-m="goto"', "data-flow-goto-target", "data-flow-goto-max"):
            self.assertIn(needle, playbooks)

    def test_flow_source_wiring(self):
        flow = FLOW_JS.read_text()
        self.assertIn("escHtml(target", flow)
        self.assertNotIn("onclick=", flow)

    def test_css_appended(self):
        css = CSS.read_text()
        self.assertGreater(len(css.splitlines()), 4470)
        self.assertIn(".fcn-goto", css)
        self.assertIn(".fce-jump", css)
        block = css[css.index("/* Go to (2026.14.6)"):]
        self.assertNotIn("border-left", block)
        self.assertNotIn("border-right", block)
