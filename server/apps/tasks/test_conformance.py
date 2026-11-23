"""The wire-protocol vectors, run against the server's own code.

`docs/wire-protocol.md` is the prose; `protocol/conformance/*.json` are the
vectors; this is one of the two runners (the other is
`agent/tests/test_conformance.py`). Both sides read the same files, so a change
to signing or to the expression language has to change the vectors in the same
commit — anything else fails here or there.

The signing vectors carry the exact canonical JSON text and its signature,
produced once by `sign_task` / `sign_task_v2` with the test seed in the file.
Ed25519 is deterministic, so re-signing must reproduce them byte for byte.
"""

import base64
import json
import types
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from nacl.signing import SigningKey

from apps.tasks import expression
from vigil import signing

VECTORS = Path(settings.BASE_DIR).parent / "protocol" / "conformance"


def _load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def _task(case: dict):
    return types.SimpleNamespace(**case["task"])


def _fields(case: dict) -> dict:
    """The signed field set, built the way the signer builds it."""
    task = case["task"]
    fields = {
        "id": task["id"],
        "host_id": task["host_id"],
        "action": task["action"],
        "params": task["params"],
        "nonce": task["nonce"],
        "ttl_seconds": task["ttl_seconds"],
    }
    if case["version"] == 2:
        fields.update({
            "dispatched_at": case["dispatched_at"],
            "agent": signing.agent_fingerprint(case["agent_token"]),
            "v": 2,
        })
    return fields


class ExpressionVectorTests(SimpleTestCase):
    """Every expression vector, evaluated and validated as the server does."""

    def test_evaluate_vectors(self):
        cases = _load("expressions.json")["cases"]
        evaluated = [c for c in cases if "result" in c]
        self.assertTrue(evaluated)
        for case in evaluated:
            with self.subTest(case["name"]):
                self.assertEqual(expression.evaluate(case["expr"], case["context"]),
                                 case["result"])

    def test_validate_rejects_the_error_vectors(self):
        cases = _load("expressions.json")["cases"]
        refused = [c for c in cases if c.get("error")]
        self.assertTrue(refused)
        for case in refused:
            with self.subTest(case["name"]), self.assertRaises(expression.ExprError):
                expression.validate(case["expr"])

    def test_steps_shape_vectors_are_refused_by_the_step_ref_check(self):
        """`validate` lets a misshapen `steps` read through; the spec layer is
        what refuses it, so those vectors pin `referenced_steps`."""
        cases = _load("expressions.json")["cases"]
        shaped = [c for c in cases if c.get("steps_shape_error")]
        self.assertTrue(shaped)
        for case in shaped:
            with self.subTest(case["name"]), self.assertRaises(expression.ExprError):
                expression.validate(case["expr"])
                expression.referenced_steps(case["expr"])


class SigningVectorTests(SimpleTestCase):
    """The server reproduces every canonical payload and signature."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vectors = _load("signing.json")
        cls.key = SigningKey(base64.b64decode(cls.vectors["seed_b64"]))
        patcher = mock.patch.object(signing, "get_signing_key", return_value=cls.key)
        patcher.start()
        cls.addClassCleanup(patcher.stop)

    def test_public_key_matches_the_seed(self):
        self.assertEqual(
            self.vectors["public_key_b64"],
            base64.b64encode(bytes(self.key.verify_key)).decode())

    def test_canonical_payload_text(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(
                    json.dumps(_fields(case), sort_keys=True), case["canonical"])

    def test_signature_matches_the_vector(self):
        for case in self.vectors["cases"]:
            with self.subTest(case["name"]):
                task = _task(case)
                if case["version"] == 1:
                    signature = signing.sign_task(task)
                else:
                    signature = signing.sign_task_v2(
                        task, case["dispatched_at"],
                        signing.agent_fingerprint(case["agent_token"]))
                self.assertEqual(signature, case["signature_b64"])
