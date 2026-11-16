"""The poller: a signed check list is learned, run on schedule, and reported.

The list comes from the dashboard, so these tests hold the signing key and
check that anything else (another agent's list, a tampered field, no
signature, a poller that is switched off) changes nothing. The checks
themselves are a fake run_check, and time is a fake clock.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# isort: split
from vigil_agent import client
from vigil_agent.config import AgentConfig
from vigil_agent.poller import MAX_RESULTS, Poller
from vigil_agent.verify import agent_fingerprint

TOKEN = "t" * 64
SERVER = "https://server.example.com"
ISSUED = 1_800_000_000


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _check(check_id, kind="tcp", params=None, interval=60):
    if params is None:
        params = {"host": "10.0.0.5", "port": 22} if kind == "tcp" else {"host": "10.0.0.5"}
    return {"id": check_id, "kind": kind, "params": params, "interval": interval}


class _Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.signing = SigningKey.generate()
        self.verify_key = self.signing.verify_key
        self.clock = _Clock()
        self.calls = []

        def fake_run(check, **_kwargs):
            self.calls.append(check)
            return {"kind": check["kind"], "target": check.get("host", ""), "ok": True,
                    "latency_ms": 1.5, "detail": "reachable", "port": check.get("port")}

        self.fake_run = fake_run

    def config(self, poller=True, token=TOKEN):
        return AgentConfig(server_url=SERVER, agent_token=token, data_dir=self.data_dir,
                           mode="managed", poller=poller)

    def poller(self, poller=True, token=TOKEN):
        return Poller(self.config(poller, token), run_check=self.fake_run,
                      clock=self.clock, wall=lambda: float(ISSUED))

    def signed(self, checks, issued=ISSUED, agent=None):
        payload = {"v": 1, "type": "check-list",
                   "agent": agent if agent is not None else agent_fingerprint(TOKEN),
                   "issued": issued, "checks": checks}
        signature = base64.b64encode(
            self.signing.sign(json.dumps(payload, sort_keys=True).encode()).signature
        ).decode()
        return {"checks": checks, "checks_issued": issued, "checks_sig": signature}


class LearnAndRunTests(_Fixture):
    def test_signed_list_runs_on_schedule_and_reports(self):
        poller = self.poller()
        poller.learn(self.signed([_check("c1", interval=60)]), self.verify_key)

        self.assertEqual(poller.run_due(), 1)
        self.assertEqual(poller.run_due(), 0, "not again before the interval passes")
        self.clock.advance(59)
        self.assertEqual(poller.run_due(), 0)
        self.clock.advance(1)
        self.assertEqual(poller.run_due(), 1)

        results = poller.take_results()
        self.assertEqual(len(results), 2)
        first = results[0]
        self.assertEqual(first["check"], "c1")
        self.assertIs(first["ok"], True)
        self.assertEqual(first["latency_ms"], 1.5)
        self.assertEqual(first["detail"], "reachable")
        self.assertEqual(first["port"], 22)
        self.assertEqual(first["at"], "2027-01-15T08:00:00+00:00")
        self.assertNotIn("target", first)
        self.assertNotIn("kind", first)


class RefusedListTests(_Fixture):
    def test_list_signed_for_another_agent_is_ignored(self):
        poller = self.poller()
        other = agent_fingerprint("u" * 64)
        poller.learn(self.signed([_check("c1")], agent=other), self.verify_key)
        self.assertEqual(poller.run_due(), 0)

    def test_tampered_checks_field_is_ignored(self):
        poller = self.poller()
        response = self.signed([_check("c1", interval=60)])
        response["checks"][0]["interval"] = 120
        poller.learn(response, self.verify_key)
        self.assertEqual(poller.run_due(), 0)

    def test_unsigned_list_is_ignored(self):
        poller = self.poller()
        response = self.signed([_check("c1")])
        del response["checks_sig"]
        poller.learn(response, self.verify_key)
        self.assertEqual(poller.run_due(), 0)

    def test_non_poller_config_ignores_a_valid_list(self):
        poller = self.poller(poller=False)
        poller.learn(self.signed([_check("c1")]), self.verify_key)
        self.assertEqual(poller.run_due(), 0)
        self.assertEqual(self.calls, [])

    def test_no_verify_key_ignores_the_list(self):
        poller = self.poller()
        poller.learn(self.signed([_check("c1")]), None)
        self.assertEqual(poller.run_due(), 0)


class ListReplacementTests(_Fixture):
    def test_older_issue_time_is_refused_and_newer_replaces(self):
        poller = self.poller()
        poller.learn(self.signed([_check("c1")], issued=ISSUED), self.verify_key)
        poller.learn(self.signed([_check("c2")], issued=ISSUED - 100), self.verify_key)
        self.assertEqual(poller.run_due(), 1)
        self.assertEqual(self.calls[-1]["host"], "10.0.0.5")
        self.assertEqual(poller.take_results()[0]["check"], "c1")

        poller.learn(self.signed([_check("c2")], issued=ISSUED + 100), self.verify_key)
        self.assertEqual(poller.run_due(), 1)
        self.assertEqual(poller.take_results()[-1]["check"], "c2")

    def test_removed_check_stops_and_kept_check_keeps_its_schedule(self):
        poller = self.poller()
        poller.learn(self.signed([_check("c1", interval=60), _check("c2", interval=60)]),
                     self.verify_key)
        self.assertEqual(poller.run_due(), 2)

        self.clock.advance(30)
        poller.learn(self.signed([_check("c1", interval=60)], issued=ISSUED + 60),
                     self.verify_key)
        self.assertEqual(poller.run_due(), 0, "c1 keeps its schedule, c2 is gone")
        self.clock.advance(30)
        self.assertEqual(poller.run_due(), 1)
        ids = [r["check"] for r in poller.take_results()]
        self.assertEqual(ids, ["c1", "c2", "c1"])


class MalformedEntryTests(_Fixture):
    def test_bad_entries_are_dropped_and_the_rest_kept(self):
        poller = self.poller()
        checks = [
            _check("good"),
            {"id": "bad-kind", "kind": "ping", "params": {"host": "x"}, "interval": 60},
            {"id": "short", "kind": "tcp", "params": {"host": "x"}, "interval": 5},
            {"id": "params", "kind": "tcp", "params": "10.0.0.5", "interval": 60},
            {"id": 7, "kind": "tcp", "params": {"host": "x"}, "interval": 60},
            {"id": "bool-interval", "kind": "tcp", "params": {"host": "x"}, "interval": True},
            "not-a-dict",
        ]
        poller.learn(self.signed(checks), self.verify_key)
        self.assertEqual(poller.run_due(), 1)
        self.assertEqual(poller.take_results()[0]["check"], "good")


class ResultQueueTests(_Fixture):
    def test_take_drains_and_restore_puts_back_first(self):
        poller = self.poller()
        poller.learn(self.signed([_check("c1")]), self.verify_key)
        poller.run_due()
        taken = poller.take_results()
        self.assertEqual(len(taken), 1)
        self.assertEqual(poller.take_results(), [])

        poller.restore_results(taken)
        self.clock.advance(60)
        poller.run_due()
        results = poller.take_results()
        self.assertEqual([r["check"] for r in results], ["c1", "c1"])
        self.assertIs(results[0], taken[0])

    def test_more_than_500_keeps_the_newest(self):
        poller = self.poller()
        poller.restore_results([{"check": "old", "n": i} for i in range(600)])
        taken = poller.take_results()
        self.assertEqual(len(taken), MAX_RESULTS)
        self.assertEqual(taken[0]["n"], 100)
        self.assertEqual(taken[-1]["n"], 599)

    def test_queue_is_capped_on_run(self):
        checks = [_check(f"c{i}") for i in range(MAX_RESULTS + 5)]
        poller = self.poller()
        poller.learn(self.signed(checks), self.verify_key)
        self.assertEqual(poller.run_due(), MAX_RESULTS + 5)
        self.assertEqual(len(poller.take_results()), MAX_RESULTS)


class RunFailureTests(_Fixture):
    def test_a_raising_check_becomes_a_failed_result(self):
        def boom(check, **_kwargs):
            raise RuntimeError("boom")

        poller = Poller(self.config(), run_check=boom, clock=self.clock,
                        wall=lambda: float(ISSUED))
        poller.learn(self.signed([_check("c1"), _check("c2")]), self.verify_key)
        self.assertEqual(poller.run_due(), 2)
        results = poller.take_results()
        self.assertEqual(len(results), 2)
        self.assertIs(results[0]["ok"], False)
        self.assertEqual(results[0]["detail"], "boom")


class CheckinPayloadTests(unittest.TestCase):
    def setUp(self):
        self.sent = {}

        def fake_post(url, json=None, **kwargs):
            self.sent.clear()
            self.sent.update(json or {})

            class _Resp:
                status_code = 200

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"status": "ok", "tasks": []}

            return _Resp()

        p = patch.object(client.requests, "post", fake_post)
        p.start()
        self.addCleanup(p.stop)

    def test_poller_is_always_sent_and_results_only_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                              data_dir=Path(tmp), mode="managed", poller=True)
            client.checkin(cfg, metrics=[])
            self.assertIs(self.sent["poller"], True)
            self.assertNotIn("check_results", self.sent)

            client.checkin(cfg, metrics=[], check_results=[{"check": "c1", "ok": True}])
            self.assertEqual(self.sent["check_results"], [{"check": "c1", "ok": True}])

            client.checkin(cfg, metrics=[], check_results=[])
            self.assertNotIn("check_results", self.sent)

            off = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                              data_dir=Path(tmp), mode="managed")
            client.checkin(off, metrics=[])
            self.assertIs(self.sent["poller"], False)


if __name__ == "__main__":
    unittest.main()
