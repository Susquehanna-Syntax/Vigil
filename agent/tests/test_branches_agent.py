"""Phase 06: the agent follows a signed ``flow`` (if/then/else).

The server signed every leaf step into ``task.params["steps"]`` plus the
branch structure into ``task.params["flow"]`` (phase 05). The agent turns
the flow into a guard expression per branch step, evaluates it before the
step's own ``when:``, and skips a step in a branch that is not taken —
recorded exactly like a when-skip so later steps see
``steps.<id>.status == "skipped"``.
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
from vigil_agent import client, executor, features
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


# The spec example: svc, then stop/upd, else upd2.
_SPEC_FLOW = [
    {"step": "svc"},
    {"id": "b1", "if": "steps.svc.result.count > 0",
     "then": [{"step": "stop"}, {"step": "upd"}],
     "else": [{"step": "upd2"}]},
]

_SPEC_STEPS = [
    {"id": "svc", "action": "hunt_service", "params": {"name": "nginx"}},
    {"id": "stop", "action": "stop_service", "params": {"service_name": "nginx"}},
    {"id": "upd", "action": "update_package", "params": {"package_name": "openssl"}},
    {"id": "upd2", "action": "update_package", "params": {"package_name": "openssl"}},
]


class BranchGuardsTests(unittest.TestCase):
    """``_branch_guards`` folds the flow into one guard per branch step."""

    def test_guards_from_flow(self):
        guards = agent_main._branch_guards(_SPEC_FLOW)
        self.assertNotIn("svc", guards)
        self.assertEqual(
            guards["stop"],
            ("(steps.svc.result.count > 0)", "b1.then"),
        )
        self.assertEqual(
            guards["upd"],
            ("(steps.svc.result.count > 0)", "b1.then"),
        )
        self.assertEqual(
            guards["upd2"],
            ("not (steps.svc.result.count > 0)", "b1.else"),
        )

    def test_nested_guards_join_with_and(self):
        flow = [
            {"step": "svc"},
            {"id": "b1", "if": "steps.svc.result.count > 0",
             "then": [
                 {"id": "b2", "if": "steps.svc.result.count == 1",
                  "then": [{"step": "stop"}],
                  "else": [{"step": "upd2"}]},
             ],
             "else": []},
        ]
        guards = agent_main._branch_guards(flow)
        self.assertEqual(
            guards["stop"],
            ("(steps.svc.result.count > 0) and (steps.svc.result.count == 1)",
             "b1.then.b2.then"),
        )
        self.assertEqual(
            guards["upd2"],
            ("(steps.svc.result.count > 0) and not (steps.svc.result.count == 1)",
             "b1.then.b2.else"),
        )

    def test_steps_outside_branches_absent(self):
        flow = [{"step": "a"}, {"step": "b"}]
        self.assertEqual(agent_main._branch_guards(flow), {})


class BranchExecutionTests(unittest.TestCase):
    """``_execute_script_task`` drives the runtime with the guards attached."""

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

    def _run(self, params, side_effect):
        with patch.object(executor, "execute_action", side_effect=side_effect):
            agent_main._execute_script_task("task-1", params, self.config, _task())

    def _statuses(self):
        state, output, steps = self.reported[0]
        return state, output, {s["id"]: s for s in (steps or [])}

    def test_then_branch_runs_else_skipped(self):
        params = {"steps": _SPEC_STEPS, "flow": _SPEC_FLOW}

        def fake(atype, params_, config, **kwargs):
            if atype == "hunt_service":
                return _hunt(1)
            return executor.ActionOutput("ok")

        self._run(params, fake)
        state, _, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["svc"]["status"], "ok")
        self.assertEqual(by_id["stop"]["status"], "ok")
        self.assertEqual(by_id["upd"]["status"], "ok")
        self.assertEqual(by_id["upd2"]["status"], "skipped")

    def test_else_branch_not_taken_message(self):
        params = {"steps": [
            {"id": "svc", "action": "hunt_service", "params": {"name": "nginx"}},
            {"id": "stop", "action": "stop_service", "params": {"service_name": "nginx"}},
            {"id": "upd2", "action": "update_package", "params": {"package_name": "openssl"}},
        ],
            "flow": [
                {"step": "svc"},
                {"id": "b1", "if": "steps.svc.result.count > 0",
                 "then": [{"step": "stop"}],
                 "else": [{"step": "upd2"}]},
            ]}

        def fake(atype, params_, config, **kwargs):
            if atype == "hunt_service":
                return _hunt(1)
            return executor.ActionOutput("ok")

        self._run(params, fake)
        state, output, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["svc"]["status"], "ok")
        self.assertEqual(by_id["stop"]["status"], "ok")
        self.assertEqual(by_id["upd2"]["status"], "skipped")
        self.assertIn("branch b1.else not taken", output)

    def test_else_branch_runs_then_skipped(self):
        params = {"steps": _SPEC_STEPS, "flow": _SPEC_FLOW}
        calls = []

        def fake(atype, params_, config, **kwargs):
            calls.append(atype)
            if atype == "hunt_service":
                return _hunt(0)
            return executor.ActionOutput("ok")

        self._run(params, fake)
        state, output, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["svc"]["status"], "ok")
        self.assertEqual(by_id["stop"]["status"], "skipped")
        self.assertEqual(by_id["upd"]["status"], "skipped")
        self.assertEqual(by_id["upd2"]["status"], "ok")
        self.assertIn("branch b1.then not taken", output)
        self.assertNotIn("stop_service", calls)
        self.assertEqual(calls.count("update_package"), 1)

    def test_skipped_branch_step_is_visible_later(self):
        params = {"steps": [
            {"id": "svc", "action": "hunt_service", "params": {"name": "nginx"}},
            {"id": "upd2", "action": "update_package", "params": {"package_name": "openssl"}},
            {"id": "final", "action": "hunt_file",
             "params": {"path": "/tmp"},
             "when": 'steps.upd2.status == "skipped"'},
        ],
            "flow": [
                {"step": "svc"},
                {"id": "b1", "if": "steps.svc.result.count > 0",
                 "then": [{"step": "upd2"}], "else": []},
            ]}

        def fake(atype, params_, config, **kwargs):
            if atype == "hunt_service":
                return _hunt(0)
            return _hunt(2)

        self._run(params, fake)
        state, _, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["upd2"]["status"], "skipped")
        self.assertEqual(by_id["final"]["status"], "ok")

    def test_bad_guard_fails_before_any_step(self):
        params = {"steps": _SPEC_STEPS,
                  "flow": [
                      {"step": "svc"},
                      {"id": "b1", "if": "steps.svc.result.count > 0",
                       "then": [{"step": "stop"}], "else": []},
                      {"id": "b2", "if": "steps.svc.result.count >",
                       "then": [{"step": "upd2"}], "else": []},
                  ]}
        calls = []

        def fake(atype, params_, config, **kwargs):
            calls.append(atype)
            return _hunt(1)

        self._run(params, fake)
        state, output, by_id = self._statuses()
        self.assertEqual(state, "failed")
        self.assertEqual(calls, [])
        self.assertIn("guard", output)
        self.assertNotIn("svc", by_id)

    def test_condition_that_cannot_be_evaluated_never_runs_the_step(self):
        # Guards and when: are parse-checked up front, but if evaluation still
        # raises, the step must be recorded as an error — not executed anyway.
        from vigil_agent import runtime
        params = {"steps": _SPEC_STEPS[:2], "flow": [
            {"step": "svc"},
            {"id": "b1", "if": "steps.svc.result.count > 0",
             "then": [{"step": "stop"}], "else": []},
        ]}
        ran = []

        def fake(atype, params_, config, **kwargs):
            ran.append(atype)
            return _hunt(1)

        real = runtime._evaluate_when
        with patch.object(runtime, "_evaluate_when",
                          side_effect=lambda e, c: (_ for _ in ()).throw(ValueError("boom"))
                          if "count" in e else real(e, c)):
            self._run(params, fake)
        self.assertEqual(ran, ["hunt_service"])
        state, _, by_id = self._statuses()
        self.assertEqual(state, "failed")
        self.assertEqual(by_id["stop"]["status"], "error")

    def test_no_flow_unchanged(self):
        params = {"steps": [
            {"id": "svc", "action": "hunt_service", "params": {"name": "nginx"}},
            {"id": "final", "action": "hunt_file",
             "params": {"path": "/tmp"},
             "when": 'steps.svc.result.count == 0'},
        ]}

        def fake(atype, params_, config, **kwargs):
            if atype == "hunt_service":
                return _hunt(0)
            return _hunt(2)

        self._run(params, fake)
        state, output, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["svc"]["status"], "ok")
        self.assertEqual(by_id["final"]["status"], "ok")
        self.assertNotIn("branch", output)

    def test_branch_step_with_false_when_still_skips(self):
        # A true guard falls through to the step's own when:.
        params = {"steps": _SPEC_STEPS + [
            {"id": "final", "action": "hunt_file",
             "params": {"path": "/tmp"},
             "when": 'steps.upd.status == "ok"'},
        ], "flow": _SPEC_FLOW}

        def fake(atype, params_, config, **kwargs):
            if atype == "hunt_service":
                return _hunt(1)
            return executor.ActionOutput("ok")

        self._run(params, fake)
        state, _, by_id = self._statuses()
        self.assertEqual(state, "completed")
        self.assertEqual(by_id["stop"]["status"], "ok")
        self.assertEqual(by_id["final"]["status"], "ok")


class BranchFeaturesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = _config(self._tmp.name)

    def test_features_include_branches(self):
        self.assertIn("branches", features.FEATURES)
        self.assertEqual(features.FEATURES, ("relevant", "branches", "boost"))

    def test_checkin_sends_branches(self):
        resp = MagicMock()
        resp.json.return_value = {"status": "online", "tasks": []}
        with patch.object(client.requests, "post", return_value=resp) as post:
            client.checkin(self.config, {})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["features"], ["relevant", "branches", "boost"])


if __name__ == "__main__":
    unittest.main()
