"""The scanner half of discovery: a signed scan is run once and answered.

The scan comes from the dashboard, so these tests hold the signing key and check
that anything else (a poller that is switched off, no verify key, a wrong
signature, another agent's fingerprint, a malformed entry) runs nothing at all.
The scan itself is a fake run_scan and the thread is ``lambda fn: fn()``, so a
scan finishes inside ``learn``. Nothing here touches a network.
"""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import client
from vigil_agent.config import AgentConfig
from vigil_agent.discovery import DiscoveryError, Scanner
from vigil_agent.verify import agent_fingerprint

TOKEN = "t" * 64
OTHER_TOKEN = "u" * 64
SERVER = "https://server.example.com"
ISSUED = 1_800_000_000
COMMUNITY = "s3cret-community"


def _entry(scan_id="scan-1", cidr="10.0.0.0/29", ports=None, community=COMMUNITY):
    if ports is None:
        ports = [22, 80]
    entry = {"id": scan_id, "cidr": cidr, "ports": ports}
    if community is not None:
        entry["community"] = community
    return entry


def _result(scanned=8):
    return {
        "cidr": "10.0.0.0/29",
        "scanned": scanned,
        "found": [{"ip": "10.0.0.1", "mac": "", "hostname": "", "vendors": [],
                   "ports": [22], "snmp": {}, "alive_via": ["tcp"]}],
        "truncated": False,
        "seconds": 1.5,
    }


class _Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.signing = SigningKey.generate()
        self.verify_key = self.signing.verify_key
        self.calls = []

        def fake_run(cidr, ports=None, community=None):
            self.calls.append({"cidr": cidr, "ports": ports, "community": community})
            return _result()

        self.fake_run = fake_run

    def config(self, poller=True, token=TOKEN):
        return AgentConfig(server_url=SERVER, agent_token=token, data_dir=self.data_dir,
                           mode="managed", poller=poller)

    def scanner(self, run_scan=None, start_thread=None, poller=True, token=TOKEN):
        return Scanner(
            self.config(poller, token),
            run_scan=self.fake_run if run_scan is None else run_scan,
            start_thread=start_thread or (lambda fn: fn()),
        )

    def signed(self, scan, issued=ISSUED, agent=None, key=None):
        payload = {"v": 1, "type": "discovery-scan",
                   "agent": agent if agent is not None else agent_fingerprint(TOKEN),
                   "issued": issued, "scan": scan}
        signing = self.signing if key is None else key
        signature = base64.b64encode(
            signing.sign(json.dumps(payload, sort_keys=True).encode()).signature
        ).decode()
        return {"scan": scan, "scan_issued": issued, "scan_sig": signature}


class LearnAndRunTests(_Fixture):
    def test_a_verified_scan_runs_once_with_cidr_ports_and_community(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry()), self.verify_key)
        self.assertEqual(self.calls,
                         [{"cidr": "10.0.0.0/29", "ports": [22, 80],
                           "community": COMMUNITY}])

    def test_the_answer_awaits_one_checkin_and_then_there_is_none(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry()), self.verify_key)
        answer = scanner.take_result()
        self.assertEqual(answer["id"], "scan-1")
        self.assertEqual(answer["scanned"], 8)
        self.assertEqual(len(answer["found"]), 1)
        self.assertIs(answer["truncated"], False)
        self.assertEqual(scanner.take_result(), None)

    def test_a_scan_without_community_passes_none(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry(community=None)), self.verify_key)
        self.assertEqual(self.calls[0]["community"], None)

    def test_a_poller_switched_off_runs_nothing(self):
        scanner = self.scanner(poller=False)
        scanner.learn(self.signed(_entry()), self.verify_key)
        self.assertEqual(self.calls, [])

    def test_no_verify_key_runs_nothing(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry()), None)
        self.assertEqual(self.calls, [])

    def test_a_response_without_a_scan_runs_nothing(self):
        scanner = self.scanner()
        scanner.learn({"tasks": []}, self.verify_key)
        scanner.learn("not a dict", self.verify_key)
        self.assertEqual(self.calls, [])

    def test_a_wrong_signature_runs_nothing(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry(), key=SigningKey.generate()), self.verify_key)
        self.assertEqual(self.calls, [])
        self.assertEqual(scanner.take_result(), None)

    def test_a_scan_signed_for_another_agent_runs_nothing(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry(), agent=agent_fingerprint(OTHER_TOKEN)),
                      self.verify_key)
        self.assertEqual(self.calls, [])

    def test_a_tampered_scan_runs_nothing(self):
        scanner = self.scanner()
        response = self.signed(_entry())
        response["scan"] = _entry(scan_id="other")
        scanner.learn(response, self.verify_key)
        self.assertEqual(self.calls, [])

    def test_a_malformed_entry_runs_nothing(self):
        scanner = self.scanner()
        for broken in (_entry(ports="22"), _entry(cidr=12345), {"id": "x", "cidr": "10.0.0.0/29"},
                       {"id": "x", "ports": [22]}, _entry(community=7)):
            scanner.learn(self.signed(broken), self.verify_key)
        self.assertEqual(self.calls, [])

    def test_a_malformed_envelope_runs_nothing(self):
        scanner = self.scanner()
        for envelope in ({"scan": "not-a-dict", "scan_issued": ISSUED,
                          "scan_sig": "AAAA"},
                         {"scan": _entry(), "scan_issued": "1800000000",
                          "scan_sig": "AAAA"},
                         {"scan": _entry(), "scan_issued": True, "scan_sig": "AAAA"},
                         {"scan": _entry(), "scan_issued": ISSUED, "scan_sig": 9}):
            scanner.learn(envelope, self.verify_key)
        self.assertEqual(self.calls, [])


class OnceOnlyTests(_Fixture):
    def test_the_same_scan_while_waiting_or_after_taken_runs_nothing(self):
        scanner = self.scanner()
        response = self.signed(_entry())

        scanner.learn(response, self.verify_key)   # the inline thread already ran
        self.assertEqual(len(self.calls), 1)
        scanner.learn(response, self.verify_key)   # its answer waits to be sent
        self.assertEqual(len(self.calls), 1)

        answer = scanner.take_result()
        self.assertEqual(answer["id"], "scan-1")
        scanner.learn(response, self.verify_key)   # already answered
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(scanner.take_result(), None)

    def test_a_scan_resend_while_one_is_running_starts_nothing(self):
        started = []
        scanner = self.scanner(start_thread=lambda fn: started.append(fn))
        first = self.signed(_entry("scan-1"), issued=ISSUED)

        scanner.learn(first, self.verify_key)      # accepted, waiting to start
        scanner.learn(first, self.verify_key)      # it is the running scan
        self.assertEqual(len(started), 1)

    def test_an_older_issue_after_a_newer_one_runs_nothing(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry("scan-2"), issued=ISSUED), self.verify_key)
        self.assertEqual(len(self.calls), 1)
        scanner.learn(self.signed(_entry("scan-1"), issued=ISSUED - 60), self.verify_key)
        self.assertEqual(len(self.calls), 1)

    def test_a_second_scan_waits_for_the_first_to_be_answered(self):
        started = []
        scanner = self.scanner(start_thread=lambda fn: started.append(fn))
        first = self.signed(_entry("scan-1"), issued=ISSUED)
        second = self.signed(_entry("scan-2", cidr="10.0.1.0/29"), issued=ISSUED + 1)

        scanner.learn(first, self.verify_key)      # accepted, waiting to start
        scanner.learn(second, self.verify_key)     # one scan at a time
        self.assertEqual(len(started), 1)

        started[0]()                               # the first scan finishes
        self.assertEqual(scanner.take_result()["id"], "scan-1")

        scanner.learn(second, self.verify_key)     # the server sends it again
        self.assertEqual(len(started), 2)
        started[1]()
        self.assertEqual(scanner.take_result()["id"], "scan-2")
        self.assertEqual([c["cidr"] for c in self.calls], ["10.0.0.0/29", "10.0.1.0/29"])


class ErrorTests(_Fixture):
    def test_a_discovery_error_becomes_the_answer(self):
        def refusing(cidr, ports=None, community=None):
            raise DiscoveryError("only private networks can be scanned")

        scanner = self.scanner(run_scan=refusing)
        scanner.learn(self.signed(_entry(cidr="8.8.8.0/29")), self.verify_key)
        answer = scanner.take_result()
        self.assertEqual(answer, {"id": "scan-1",
                                  "error": "only private networks can be scanned"})

    def test_an_unexpected_error_is_a_plain_sentence_without_the_community(self):
        def exploding(cidr, ports=None, community=None):
            raise RuntimeError(f"socket died while scanning {community}")

        scanner = self.scanner(run_scan=exploding)
        with self.assertLogs("vigil.discovery", level="INFO") as records:
            scanner.learn(self.signed(_entry()), self.verify_key)
        answer = scanner.take_result()
        self.assertEqual(answer, {"id": "scan-1", "error": "the scan failed on the agent"})
        for record in records.records:
            self.assertNotIn(COMMUNITY, record.getMessage())

    def test_the_finish_line_counts_found_of_scanned(self):
        scanner = self.scanner()
        with self.assertLogs("vigil.discovery", level="INFO") as records:
            scanner.learn(self.signed(_entry()), self.verify_key)
        messages = [r.getMessage() for r in records.records]
        self.assertIn("discovery scan of 10.0.0.0/29 finished: 1 found of 8", messages)

    def test_the_failure_line_names_the_cidr_only(self):
        scanner = self.scanner(run_scan=self._refusing())
        with self.assertLogs("vigil.discovery", level="INFO") as records:
            scanner.learn(self.signed(_entry(cidr="8.8.8.0/29")), self.verify_key)
        messages = [r.getMessage() for r in records.records]
        self.assertIn("discovery scan of 8.8.8.0/29 failed: address out of range", messages)

    @staticmethod
    def _refusing():
        def refusing(cidr, ports=None, community=None):
            raise DiscoveryError("address out of range")

        return refusing


class RestoreTests(_Fixture):
    def test_a_failed_checkin_puts_the_answer_back_and_no_rerun_happens(self):
        scanner = self.scanner()
        response = self.signed(_entry())
        scanner.learn(response, self.verify_key)
        answer = scanner.take_result()
        self.assertEqual(scanner.take_result(), None)

        scanner.restore_result(answer)
        scanner.learn(response, self.verify_key)   # must not run the scan again
        self.assertEqual(len(self.calls), 1)

        resent = scanner.take_result()
        self.assertEqual(resent, answer)
        self.assertEqual(len(self.calls), 1)

    def test_a_restored_answer_never_overwrites_one_that_landed_since(self):
        scanner = self.scanner()
        scanner.learn(self.signed(_entry("scan-1")), self.verify_key)
        taken = scanner.take_result()
        scanner.restore_result(taken)               # the check-in failed; put it back

        newer = {"id": "scan-2", "cidr": "10.0.1.0/29", "scanned": 8, "found": [],
                 "truncated": False, "seconds": 1.0}
        scanner.restore_result(newer)               # a newer answer is already in line
        self.assertEqual(scanner.take_result(), taken)


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

    def test_scan_result_is_sent_only_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AgentConfig(server_url="http://127.0.0.1:1", agent_token="t" * 40,
                              data_dir=Path(tmp), mode="managed", poller=True)
            client.checkin(cfg, metrics=[])
            self.assertNotIn("scan_result", self.sent)

            answer = {"id": "scan-1", "cidr": "10.0.0.0/29", "scanned": 8,
                      "found": [], "truncated": False, "seconds": 1.5}
            client.checkin(cfg, metrics=[], scan_result=answer)
            self.assertEqual(self.sent["scan_result"], answer)

            failure = {"id": "scan-1", "error": "only private networks can be scanned"}
            client.checkin(cfg, metrics=[], scan_result=failure)
            self.assertEqual(self.sent["scan_result"], failure)


if __name__ == "__main__":
    unittest.main()
