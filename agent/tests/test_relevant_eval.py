"""Phase 04: the agent evaluates ``relevant:`` before any step runs.

The server signed the tree into ``task.params`` in phase 03. Now every probe
runs as the hunt it is (allowlist and hunt limits apply), the tree decides,
and the host either runs the steps or reports ``not_applicable`` — keeping
what the probes found as evidence (user decision 2026-09-26).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import __main__ as agent_main
from vigil_agent import client, executor
from vigil_agent.config import AgentConfig


def _config(tmp, allowlist=None) -> AgentConfig:
    return AgentConfig(
        server_url="https://vigil.example.com",
        agent_token="t",
        mode="managed",
        data_dir=Path(tmp),
        allowlist=set(allowlist) if allowlist else {"run_command", "hunt_file"},
    )


def _task(task_id="task-1"):
    return {"id": task_id, "action": "_script"}


def _hunt(count: int, data: dict | None = None):
    return executor.ActionOutput(
        json.dumps({"matches": [], "truncated": False, "duration": 0.1}),
        data={"matched": count > 0, "count": count,
              "truncated": False, **(data or {})},
    )


def _tree_all(*probes):
    return {"op": "all", "items": list(probes)}


def _probe(n: int, ptype="hunt_file"):
    return {"probe": {"id": f"relevant-{n}", "type": ptype,
                      "params": {"name": f"f{n}"}}}


class RelevantHoldsTests(unittest.TestCase):
    """``_relevant_holds`` is the pure fold of the tree."""

    def test_relevant_holds_truth_table(self):
        holds = agent_main._relevant_holds

        self.assertTrue(holds(_tree_all(_probe(1)), {"relevant-1": True}))
        self.assertFalse(holds(_tree_all(_probe(1)), {"relevant-1": False}))
        self.assertFalse(holds(
            _tree_all(_probe(1), _probe(2)), {"relevant-1": True, "relevant-2": False}))
        self.assertTrue(holds(
            _tree_all(_probe(1), _probe(2)), {"relevant-1": True, "relevant-2": True}))

        any_tree = {"op": "any", "items": [_probe(1), _probe(2)]}
        self.assertTrue(holds(any_tree, {"relevant-1": False, "relevant-2": True}))
        self.assertFalse(holds(any_tree, {"relevant-1": False, "relevant-2": False}))
        self.assertTrue(holds(any_tree, {"relevant-1": True, "relevant-2": True}))

        not_tree = {"op": "not", "items": [_probe(1), _probe(2)]}
        self.assertTrue(holds(not_tree, {"relevant-1": False, "relevant-2": False}))
        self.assertFalse(holds(not_tree, {"relevant-1": True, "relevant-2": False}))
        self.assertFalse(holds(not_tree, {"relevant-1": False, "relevant-2": True}))

        # ``not`` means *none* holds, not *not all*: one hit inside the not
        # still makes the whole node fail.
        not_one = {"op": "not", "items": [_probe(1), _probe(2)]}
        self.assertFalse(holds(not_one, {"relevant-1": True, "relevant-2": True}))

        # Nested: all(any(1,2), not(3)).
        nested = _tree_all(
            {"op": "any", "items": [_probe(1), _probe(2)]},
            {"op": "not", "items": [_probe(3)]},
        )
        self.assertTrue(holds(nested, {"relevant-1": False,
                                       "relevant-2": True, "relevant-3": False}))
        self.assertFalse(holds(nested, {"relevant-1": False,
                                        "relevant-2": False, "relevant-3": False}))
        self.assertFalse(holds(nested, {"relevant-1": True,
                                        "relevant-2": True, "relevant-3": True}))


class RelevantEvaluationTests(unittest.TestCase):
    """``_evaluate_relevant`` runs every probe once, in id order."""

    def test_every_probe_runs_once_in_id_order(self):
        calls = []

        def fake_execute(atype, params, config):
            calls.append((atype, params))
            return _hunt(0)

        tree = _tree_all(_probe(2), _probe(1))
        with patch.object(executor, "execute_action", side_effect=fake_execute):
            applies, steps, lines = agent_main._evaluate_relevant(tree, None)

        self.assertEqual([c[1]["name"] for c in calls], ["f1", "f2"])
        self.assertFalse(applies)
        self.assertEqual([s["id"] for s in steps], ["relevant-1", "relevant-2"])
        self.assertEqual(
            lines,
            ["[PROBE] relevant-1 (hunt_file): 0 match(es)",
             "[PROBE] relevant-2 (hunt_file): 0 match(es)"],
        )
        for step in steps:
            self.assertEqual(step["status"], "ok")
            self.assertIn("hunt", step)
            self.assertIn("count", step["result"])

    def test_nested_probes_run_and_decide(self):
        # The spec's own example nests: all: [pkg] + not: [file]. Probes
        # under an inner node must run too, and the fold must reach them.
        tree = {"op": "all", "items": [
            {"op": "all", "items": [_probe(1)]},
            {"op": "not", "items": [_probe(2)]},
        ]}
        seen = []

        def fake(atype, params, config):
            seen.append(params["name"])
            return _hunt(1 if params["name"] == "f1" else 0)

        with patch.object(executor, "execute_action", side_effect=fake):
            applies, steps, _ = agent_main._evaluate_relevant(tree, None)
        self.assertEqual(seen, ["f1", "f2"])
        self.assertTrue(applies)
        self.assertEqual([s["id"] for s in steps], ["relevant-1", "relevant-2"])

    def test_probe_output_is_the_hunt_payload(self):
        with patch.object(executor, "execute_action", return_value=_hunt(2)):
            applies, steps, _ = agent_main._evaluate_relevant(
                _tree_all(_probe(1)), None)

        self.assertTrue(applies)
        self.assertEqual(steps[0]["result"], {"matched": True, "count": 2,
                                              "truncated": False})
        self.assertEqual(steps[0]["hunt"]["matches"], [])


class NotApplicableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = _config(self._tmp.name)
        self.reported = []
        self._patch = patch.object(client, "report_result",
                                   lambda cfg, tid, state, output, steps=None:
                                   self.reported.append((state, output, steps)))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_not_applicable_reports_probe_evidence(self):
        params = {"relevant": _tree_all(_probe(1)), "steps": [
            {"id": "s1", "action": "run_command",
             "params": {"command": "echo fix"}}]}
        with patch.object(executor, "execute_action", return_value=_hunt(0)):
            agent_main._execute_script_task("task-1", params, self.config, _task())

        self.assertEqual(len(self.reported), 1)
        state, output, steps = self.reported[0]
        self.assertEqual(state, "not_applicable")
        self.assertIn("[NOT APPLICABLE] relevant: did not match", output)
        self.assertIn("[PROBE] relevant-1 (hunt_file): 0 match(es)", output)
        self.assertEqual([s["id"] for s in steps], ["relevant-1"])

    def test_applies_runs_steps_with_probes_first(self):
        params = {"relevant": _tree_all(_probe(1)), "steps": [
            {"id": "s1", "action": "run_command",
             "params": {"command": "echo fix"}}]}
        step_out = executor.ActionOutput("fixed")
        with patch.object(executor, "execute_action",
                          side_effect=[_hunt(1), step_out]):
            agent_main._execute_script_task("task-1", params, self.config, _task())

        self.assertEqual(len(self.reported), 1)
        state, output, steps = self.reported[0]
        self.assertEqual(state, "completed")
        self.assertEqual([s["id"] for s in steps], ["relevant-1", "s1"])
        self.assertIn("[PROBE] relevant-1 (hunt_file): 1 match(es)", output)

    def test_probe_error_fails_the_task(self):
        params = {"relevant": _tree_all(_probe(1), _probe(2)), "steps": [
            {"id": "s1", "action": "run_command",
             "params": {"command": "echo fix"}}]}

        def explode(atype, params, config):
            if params["name"] == "f2":
                raise OSError("disk gone")
            return _hunt(1)

        with patch.object(executor, "execute_action", side_effect=explode):
            agent_main._execute_script_task("task-1", params, self.config, _task())

        self.assertEqual(len(self.reported), 1)
        state, output, steps = self.reported[0]
        self.assertEqual(state, "failed")
        self.assertEqual(
            output,
            "[PROBE] relevant-1 (hunt_file): 1 match(es)\n"
            "[ERROR] relevant-2 (hunt_file): disk gone — task not run")
        # The probes that did run are kept as evidence.
        self.assertEqual([s["id"] for s in steps], ["relevant-1"])

    def test_probe_goes_through_the_allowlist(self):
        # A managed config without hunt_file in its allowlist: the probe is
        # refused by the executor, the task fails, and no step runs.
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp, allowlist=["run_command"])
            params = {"relevant": _tree_all(_probe(1)), "steps": [
                {"id": "s1", "action": "run_command",
                 "params": {"command": "echo fix"}}]}
            with patch.object(executor, "execute_action",
                              side_effect=ValueError(
                                  "Action 'hunt_file' is not allowed in mode "
                                  "'managed' with current allowlist")):
                agent_main._execute_script_task("task-1", params, config, _task())

        self.assertEqual(len(self.reported), 1)
        state, output, steps = self.reported[0]
        self.assertEqual(state, "failed")
        self.assertIn("relevant-1", output)
        self.assertIn("allowlist", output)
        self.assertEqual([s["id"] for s in steps], [])


class CheckinFeaturesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = _config(self._tmp.name)

    def test_checkin_sends_features(self):
        resp = MagicMock()
        resp.json.return_value = {"status": "online", "tasks": []}
        with patch.object(client.requests, "post", return_value=resp) as post:
            client.checkin(self.config, {})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["features"], ["relevant"])

if __name__ == "__main__":
    unittest.main()
