"""SEC-4: tasks are signed over when they were sent and which agent they were sent
to, and this agent refuses anything else. v1 left the dispatch time unsigned
(the TTL could be restarted by whoever relayed a task) and named a target host
that no agent ever checked."""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import base64
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from nacl.signing import SigningKey

from vigil_agent import __main__ as agent_main
from vigil_agent import nonce_store as ns
from vigil_agent import verify

KEY = SigningKey.generate()
TOKEN = "this-agents-token"


def _now(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat()


def _task(nonce="n1", dispatched_at=None, token=TOKEN, v2=True, ttl=300):
    task = {"id": "t-" + nonce, "host_id": "h1", "action": "check_service",
            "params": {"service_name": "cron"}, "nonce": nonce, "ttl_seconds": ttl,
            "dispatched_at": dispatched_at or _now()}
    fields = {k: task[k] for k in ("id", "host_id", "action", "params", "nonce", "ttl_seconds")}
    if v2:
        fields.update({"dispatched_at": task["dispatched_at"], "agent": verify.agent_fingerprint(token), "v": 2})
        task["sig_v"] = 2
    sig = KEY.sign(json.dumps(fields, sort_keys=True).encode()).signature
    task["signature"] = base64.b64encode(sig).decode()
    return task


class _Cfg:
    mode = "full_control"
    agent_token = TOKEN


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ns.NonceStore(Path(self.tmp.name))
        self.rejected, self.ran = [], []
        for name, fn in (("_report_rejected", lambda c, t, r: self.rejected.append((t["id"], r))),
                         ("execute_action", lambda action, params, config: self.ran.append(action) or "ok"),
                         ("_report_completed", lambda *a, **k: None),
                         ("_report_failed", lambda *a, **k: None),
                         ("_privilege_mismatch", lambda c: False)):
            p = patch.object(agent_main, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def _process(self, task):
        agent_main._process_tasks([task], _Cfg(), self.store, KEY.verify_key)

    def test_a_v2_task_for_this_agent_runs(self):
        self._process(_task())
        self.assertEqual(self.ran, ["check_service"])
        self.assertEqual(self.rejected, [])

    def test_a_v1_signature_is_refused(self):
        self._process(_task(v2=False))
        self.assertEqual(self.ran, [])
        self.assertEqual(self.rejected[0][1], "Invalid signature")

    def test_restarting_the_ttl_breaks_the_signature(self):
        task = _task(dispatched_at=_now(-3600))
        task["dispatched_at"] = _now()          # a relay trying to make it fresh
        self._process(task)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.rejected[0][1], "Invalid signature")

    def test_a_task_signed_for_another_agent_is_refused(self):
        self._process(_task(token="someone-elses-token"))
        self.assertEqual(self.ran, [])

    def test_an_expired_task_is_refused(self):
        self._process(_task(dispatched_at=_now(-3600), ttl=300))
        self.assertEqual(self.ran, [])
        self.assertIn("expired", self.rejected[0][1])

    def test_a_missing_dispatch_time_is_refused(self):
        task = _task()
        del task["dispatched_at"]
        self._process(task)
        self.assertEqual(self.ran, [])

    def test_a_replay_is_refused(self):
        task = _task(nonce="again")
        self._process(task)
        self._process(dict(task))
        self.assertEqual(self.ran, ["check_service"])
        self.assertEqual(self.rejected[-1][1], "Replayed nonce")


class NonceRetentionTests(unittest.TestCase):
    def test_a_nonce_is_kept_at_least_as_long_as_its_task_could_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ns.NonceStore(Path(tmp))
            store.record("long", ttl_seconds=7 * 86400)
            with patch.object(ns.time, "time", return_value=__import__("time").time() + 6 * 86400):
                store._prune()
            self.assertTrue(store.seen("long"))

    def test_short_ttls_still_keep_an_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ns.NonceStore(Path(tmp))
            store.record("short", ttl_seconds=60)
            with patch.object(ns.time, "time", return_value=__import__("time").time() + 1800):
                store._prune()
            self.assertTrue(store.seen("short"))


if __name__ == "__main__":
    unittest.main()
