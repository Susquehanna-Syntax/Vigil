"""Multi-row if conditions: rows join to an expression and parse back (QA-34).

The inspector builds a condition as rows joined with all-of / any-of. Rows
serialize to an ordinary expression, so the functions here are the whole
contract between the builder and the expression language — they run in `node`,
taken from the shipped file, so a test that goes red means the shipped
behaviour changed. The joined strings are also handed to the real server-side
flow validator, which is what a saved playbook goes through.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.playbooks.flow import _check_condition

ROOT = Path(__file__).resolve().parents[2]
FLOW_JS = ROOT / "static/js/vigil-playbook-flow.js"
PLAYBOOKS_JS = ROOT / "static/js/vigil-playbooks.js"
CSS = ROOT / "static/css/vigil.css"

def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


def _runtime(src: str) -> str:
    """The condition-row functions from the shipped file, in dependency order,
    with the words table flowCondWords needs."""
    start = src.index("const _FLOW_STATUS_WORDS =")
    parts = [src[start:src.index("};", start) + 2]]
    for name in ("flowCondWords", "flowCondRow", "flowCondJoin", "_flowCondParts",
                 "_flowCondWrapped", "_flowCondValue", "flowCondParse", "flowCondSummary"):
        parts.append(_function(src, name))
    parts.append(re.search(r"const _FLOW_COND_ROW = .*", src).group(0))
    return "\n".join(parts)


class NodeRunTests(SimpleTestCase):
    """Runs a body of JS against the real row functions in the shipped file."""

    def setUp(self):
        self.src = FLOW_JS.read_text()

    def _node(self, body: str):
        self.assertTrue(shutil.which("node"), "node is not installed")
        code = _runtime(self.src) + "\n" + body
        out = subprocess.run(["node", "-e", code],
                             capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)


class ConditionRowTests(NodeRunTests):
    @skipUnless(shutil.which("node"), "node is not installed")
    def test_join_shapes(self):
        body = """
const R = (sid, what, op, value) => ({ sid, what, op, value });
console.log(JSON.stringify([
  flowCondJoin([], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok')], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok'), R('b', 'count', '>', '0')], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok'), R('b', 'count', '>', '0')], 'any'),
  flowCondJoin([R('b', 'up', '==', 'true')], 'all'),
  flowCondJoin([R('b', 'name', '==', 'web 1')], 'all'),
  flowCondJoin([R('b', 'up', '==', 'false')], 'all'),
  flowCondRow(R('b', 'count', '>=', '2.5')),
]));"""
        empty, one, all_of, any_of, out_true, out_str, out_false, num = self._node(body)
        self.assertEqual(empty, "")
        self.assertEqual(one, 'steps.a.status == "ok"')
        self.assertEqual(all_of, '(steps.a.status == "ok") and (steps.b.result.count > 0)')
        self.assertEqual(any_of, '(steps.a.status == "ok") or (steps.b.result.count > 0)')
        self.assertEqual(out_true, "steps.b.result.up == True")
        self.assertEqual(out_false, "steps.b.result.up == False")
        self.assertEqual(out_str, 'steps.b.result.name == "web 1"')
        self.assertEqual(num, "steps.b.result.count >= 2.5")

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_parse_round_trips(self):
        body = """
const R = (sid, what, op, value) => ({ sid, what, op, value });
const cases = [
  flowCondJoin([], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok')], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok'), R('b', 'count', '>', '0')], 'all'),
  flowCondJoin([R('a', 'status', '==', 'ok'), R('b', 'count', '>', '0')], 'any'),
  flowCondJoin([R('b', 'up', '==', 'true')], 'all'),
  flowCondJoin([R('b', 'up', '==', 'false')], 'any'),
  flowCondJoin([R('b', 'name', '==', 'web 1'), R('c', 'port', '==', '8080')], 'any'),
];
console.log(JSON.stringify(cases.map(c => {
  const p = flowCondParse(c);
  return p === null ? 'NULL' : flowCondJoin(p.rows, p.mode);
})));"""
        self.assertEqual(self._node(body), [
            "",
            'steps.a.status == "ok"',
            '(steps.a.status == "ok") and (steps.b.result.count > 0)',
            '(steps.a.status == "ok") or (steps.b.result.count > 0)',
            "steps.b.result.up == True",
            "steps.b.result.up == False",
            '(steps.b.result.name == "web 1") or (steps.c.result.port == 8080)',
        ])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_parse_keeps_quoted_and_in_one_row(self):
        body = "console.log(JSON.stringify(flowCondParse('steps.a.status == \"x and y\"')));"
        parsed = self._node(body)
        self.assertEqual(parsed["mode"], "all")
        self.assertEqual(parsed["rows"], [{"sid": "a", "what": "status", "op": "==", "value": "x and y"}])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_parse_accepts_unparenthesized_and(self):
        body = ("console.log(JSON.stringify("
                "flowCondParse('steps.a.status == \"ok\" and steps.b.status != \"failed\"')));")
        parsed = self._node(body)
        self.assertEqual(parsed["mode"], "all")
        self.assertEqual([r["op"] for r in parsed["rows"]], ["==", "!="])
        self.assertEqual([r["value"] for r in parsed["rows"]], ["ok", "failed"])

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_parse_refuses_what_rows_cannot_hold(self):
        body = """
console.log(JSON.stringify([
  'not (steps.a.status == "ok")',
  '(steps.a.status == "ok") and (steps.b.status == "ok") or (steps.c.status == "ok")',
  'inputs.mode == "full"',
  'steps.a.status > "ok"',
  'steps.a.result.n in (1, 2)',
].map(c => flowCondParse(c))));"""
        self.assertEqual(self._node(body), [None] * 5)

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_summary_reads_each_row(self):
        body = ("console.log(JSON.stringify([" + ",".join(
            f"flowCondSummary({c})" for c in [
                "'(steps.a.status == \"ok\") or (steps.b.status == \"failed\")'",
                "'(steps.a.status == \"ok\") and (steps.b.status == \"failed\")'",
                "'steps.a.status == \"ok\"'",
                "''",
                "'inputs.mode == \"full\"'",
            ]) + "]));")
        rows, both, single, blank, hand = self._node(body)
        self.assertEqual(rows, "a is ok or b failed")
        self.assertEqual(both, "a is ok and b failed")
        self.assertEqual(single, "a is ok")
        self.assertEqual(blank, "")
        self.assertIn("inputs.mode", hand)


class JoinedConditionValidationTests(NodeRunTests):
    """The builder's joined strings are ordinary expressions: the flow
    validator that every saved playbook goes through accepts them. hunt_process
    produces count (int), so ordering comparisons on it are legitimate."""

    def _accept(self, expr):
        earlier = {"s1": {"count": "int", "matched": "bool"},
                   "s2": {"count": "int", "matched": "bool"}}
        _check_condition(expr, earlier, "branch b1")  # raises FlowError if refused

    @skipUnless(shutil.which("node"), "node is not installed")
    def test_the_servers_flow_validator_accepts_joined_rows(self):
        body = """
const R = (sid, what, op, value) => ({ sid, what, op, value });
console.log(JSON.stringify([
  flowCondJoin([R('s1', 'status', '==', 'ok')], 'all'),
  flowCondJoin([R('s1', 'status', '==', 'ok'), R('s2', 'count', '>', '0')], 'all'),
  flowCondJoin([R('s1', 'status', '==', 'ok'), R('s2', 'matched', '==', 'true')], 'any'),
  flowCondJoin([R('s1', 'count', '>=', '2'), R('s2', 'count', '<=', '9')], 'all'),
]));"""
        joined = self._node(body)
        self.assertEqual(len(joined), 4)
        for expr in joined:
            self._accept(expr)


class ConditionRowSourceTests(SimpleTestCase):
    def test_builder_markup(self):
        flow = FLOW_JS.read_text()
        for needle in ("data-fb-rows", "data-fb-add", "data-fb-mode", "data-fb-raw", "data-fb-reset"):
            self.assertIn(needle, flow)
        playbooks = PLAYBOOKS_JS.read_text()
        # The rows themselves are drawn by the wiring, not the inspector.
        for needle in ("data-fb-rm", "data-fb-step", "data-fb-what", "data-fb-value"):
            self.assertIn(needle, playbooks)
        self.assertIn("flowCondParse(", playbooks)
        self.assertIn("flowCondJoin(", playbooks)
        self.assertNotIn("data-fb-use", playbooks)
        self.assertNotIn("data-fb-use", flow)
        # The canvas node and the inspector's "in words" line both read a
        # multi-row condition row by row.
        self.assertIn("escHtml(flowCondSummary(cond))", _function(flow, "_flIfNodeHtml"))
        self.assertIn("escHtml(flowCondSummary(node.cond))", _function(flow, "flowInspectorHtml"))
        self.assertIn("flowCondSummary(", playbooks)
        # No inline handlers: every control is bound from JS.
        self.assertNotIn("onclick=", flow)
        self.assertNotIn("onchange=", flow)
        self.assertNotRegex(playbooks, r"\bon(click|change|input)\s*=\s*\"")

    def test_no_side_stripe(self):
        css = CSS.read_text()
        appended = css[css.index("/* QA-34:"):]
        self.assertIn(".fb-row", appended)
        self.assertNotIn("border-left", appended)
        self.assertNotIn("border-right", appended)
