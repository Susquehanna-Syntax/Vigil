"""The hunt framework: one result shape, hard limits, low priority (M5 phase 01).

Every hunt handler calls ``run_hunt`` with a probe; the framework owns the
result shape, the result/timeout caps and the per-thread priority lowering.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import hunt


class HuntResultShapeTests(unittest.TestCase):
    def _run(self, probe, **params):
        return hunt.run_hunt(probe, params)

    def test_result_shape(self):
        def probe(result, params):
            result.add("file", name="a.txt", size=1)
            result.add("file", name="b.txt", size=2)

        out = self._run(probe)
        body = json.loads(out)
        self.assertEqual(set(body), {"matches", "truncated", "duration"})
        for match in body["matches"]:
            self.assertIn("evidence_type", match)
        self.assertEqual(body["truncated"], False)
        self.assertIsInstance(body["duration"], float)

    def test_max_results_truncates(self):
        def probe(result, params):
            for i in range(10):
                result.add("file", name=f"f{i}")

        out = self._run(probe, max_results=3)
        body = json.loads(out)
        self.assertEqual(len(body["matches"]), 3)
        self.assertEqual(body["truncated"], True)
        self.assertEqual(out.data["truncated"], True)
        self.assertEqual(out.data["count"], 3)

    def test_limits_are_clamped(self):
        max_results, timeout = hunt.limits_from(
            {"max_results": 999999, "timeout": 99999})
        self.assertEqual((max_results, timeout), (hunt.MAX_RESULTS_CEILING,
                                                  hunt.TIMEOUT_CEILING))

    def test_bad_limits_refused(self):
        with self.assertRaises(ValueError):
            hunt.limits_from({"max_results": 0})
        with self.assertRaises(ValueError):
            hunt.limits_from({"timeout": 0})

    def test_timeout_keeps_partial_matches(self):
        def probe(result, params):
            result.add("file", name="first")
            while True:
                result.check_deadline()
                time.sleep(0.05)

        out = self._run(probe, timeout=1)
        body = json.loads(out)
        self.assertEqual(body["timed_out"], True)
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["matches"][0]["name"], "first")

    def test_probe_error_fails_the_step(self):
        def probe(result, params):
            raise OSError("boom")

        with self.assertRaises(RuntimeError) as ctx:
            self._run(probe)
        self.assertIn("hunt failed", str(ctx.exception))

    def test_outputs_for_when(self):
        def probe(result, params):
            result.add("file", name="a")
            result.add("file", name="b")

        out = self._run(probe)
        self.assertEqual(out.data, {"matched": True, "count": 2,
                                    "truncated": False})

        def empty_probe(result, params):
            pass

        self.assertEqual(self._run(empty_probe).data["matched"], False)

    def test_non_json_values_are_stringified(self):
        def probe(result, params):
            result.add("file", path=Path("/tmp/x.txt"))

        body = json.loads(self._run(probe))
        self.assertEqual(body["matches"][0]["path"], str(Path("/tmp/x.txt")))

    def test_priority_lowered_in_worker_only(self):
        calls = []
        real = hunt._lower_thread_priority

        def spy():
            calls.append(threading.get_native_id())
            real()

        def probe(result, params):
            pass

        with patch.object(hunt, "_lower_thread_priority", spy):
            self._run(probe)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_native_id())

    def test_default_scopes_exist(self):
        for scope in hunt.default_scopes():
            self.assertTrue(Path(scope).is_dir(), scope)


if __name__ == "__main__":
    unittest.main()


class HuntReviewFixTests(unittest.TestCase):
    """Architect review of phase 01: separate timed_out, close after the answer, never raise on priority."""

    def test_max_results_is_not_a_timeout(self):
        def probe(result, params):
            for i in range(10):
                result.add("x", i=i)
        out = json.loads(hunt.run_hunt(probe, {"max_results": 3}))
        self.assertTrue(out["truncated"])
        self.assertNotIn("timed_out", out)

    def test_closed_result_accepts_nothing(self):
        r = hunt.HuntResult(10, 60)
        self.assertTrue(r.add("x"))
        r.close()
        self.assertFalse(r.add("x"))
        with self.assertRaises(hunt.HuntTimeout):
            r.check_deadline()
        self.assertEqual(len(r.to_dict()["matches"]), 1)

    def test_priority_errors_never_escape(self):
        class Boom(Exception):
            pass
        with patch.object(hunt.os, "setpriority", side_effect=Boom("denied")):
            hunt._lower_thread_priority()  # must not raise
