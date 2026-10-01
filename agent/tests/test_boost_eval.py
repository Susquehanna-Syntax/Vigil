"""M10: a detection task's boost: probes raise confidence, never decide."""
import tempfile
import unittest
from unittest.mock import patch

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
from tests.test_relevant_eval import _config, _hunt, _probe, _task, _tree_all

from vigil_agent import __main__ as agent_main
from vigil_agent import client, executor


def _boost(n, ptype="hunt_process"):
    return {"id": f"boost-{n}", "type": ptype, "params": {"cmdline": "java"}}


class BoostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = _config(self._tmp.name, allowlist={"run_command", "hunt_file", "hunt_process"})
        self.reported = []
        p = patch.object(client, "report_result",
                         lambda cfg, tid, state, output, steps=None:
                         self.reported.append((state, output, steps)))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, params, outputs):
        with patch.object(executor, "execute_action", side_effect=outputs):
            agent_main._execute_script_task("task-1", params, self.config, _task())
        return self.reported[-1]

    def _params(self, **extra):
        return {"relevant": _tree_all(_probe(1)), "boost": [_boost(1)], "steps": [
            {"id": "fix", "action": "run_command", "params": {"command": "echo fix"}}], **extra}

    def test_a_matching_boost_reports_high_confidence(self):
        state, output, steps = self._run(self._params(), [_hunt(1), _hunt(2), executor.ActionOutput("ok")])
        self.assertEqual(state, "completed")
        self.assertEqual([s["id"] for s in steps], ["relevant-1", "boost-1", "detection", "fix"])
        detection = next(s for s in steps if s["id"] == "detection")
        self.assertEqual(detection["result"], {"confidence": "high", "boost_matches": 1})
        self.assertIn("[BOOST] boost-1 (hunt_process): 2 match(es)", output)

    def test_no_boost_match_still_runs_at_normal_confidence(self):
        state, _out, steps = self._run(self._params(), [_hunt(1), _hunt(0), executor.ActionOutput("ok")])
        self.assertEqual(state, "completed")
        self.assertEqual(next(s for s in steps if s["id"] == "detection")["result"]["confidence"], "normal")

    def test_boost_never_runs_when_not_applicable(self):
        state, _out, steps = self._run(self._params(), [_hunt(0)])
        self.assertEqual(state, "not_applicable")
        self.assertEqual([s["id"] for s in steps], ["relevant-1"])

    def test_a_failing_boost_is_reported_not_fatal(self):
        def outputs():
            yield _hunt(1)
            raise RuntimeError("process table unreadable")
        it = outputs()
        calls = []

        def fake(action, params, config, **_kw):
            calls.append(action)
            if action == "run_command":
                return executor.ActionOutput("ok")
            return next(it)
        with patch.object(executor, "execute_action", side_effect=fake):
            agent_main._execute_script_task("task-1", self._params(), self.config, _task())
        state, output, steps = self.reported[-1]
        self.assertEqual(state, "completed")
        self.assertIn("[BOOST] boost-1 (hunt_process): failed — process table unreadable", output)
        self.assertEqual(calls[-1], "run_command")

    def test_non_hunt_boost_is_ignored(self):
        params = self._params(boost=[{"id": "boost-1", "type": "run_command", "params": {}}])
        state, _out, steps = self._run(params, [_hunt(1), executor.ActionOutput("ok")])
        self.assertEqual(state, "completed")
        self.assertEqual(next(s for s in steps if s["id"] == "detection")["result"]["boost_matches"], 0)


if __name__ == "__main__":
    unittest.main()
