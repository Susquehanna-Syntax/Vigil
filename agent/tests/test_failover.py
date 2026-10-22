"""Failover: moving to another address when the primary stops answering.

Two halves, and the second is the reason the first is safe. The mechanics —
which address to try next, what counts as "did not answer", when to try the
primary again — are checked against a fake ``call`` that reads
``config.server_url``, exactly like the real request does. The learned list comes
from the dashboard and travels through a relay, so it is signed: these tests hold
the key and check that anything else — another agent's list, a tampered url, no
signature at all — changes nothing.

Nothing here touches the network: the request function is always injected.
"""

import base64
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

import requests
from nacl.signing import SigningKey

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

# isort: split
from vigil_agent.config import AgentConfig, load_config
from vigil_agent.failover import QUEUED_LIMIT, Endpoints, checkin
from vigil_agent.verify import agent_fingerprint

TOKEN = "t" * 64
PRIMARY = "https://primary.example.com"
FALLBACK_1 = "https://fb1.example.com"
FALLBACK_2 = "https://fb2.example.com"
LEARNED = "https://learned.example.com"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Key:
    """The dashboard's signing key and its agent-side counterpart."""

    def __init__(self):
        self.signing = SigningKey.generate()

    def sign(self, payload: dict) -> str:
        return base64.b64encode(
            self.signing.sign(json.dumps(payload, sort_keys=True).encode()).signature
        ).decode()

    @property
    def verify_key(self):
        return self.signing.verify_key


def _response(relays, direct=True, agent=None, key=None, issued=1_800_000_000):
    payload = {"v": 1, "type": "relay-list", "issued": issued,
               "agent": agent if agent is not None else agent_fingerprint(TOKEN),
               "relays": relays, "direct": direct}
    body = {"relays": relays, "direct": direct, "relays_issued": issued}
    if key is not None:
        body["relays_sig"] = key.sign(payload)
    return body


def _error(status):
    response = requests.Response()
    response.status_code = status
    response.reason = "nope"
    return requests.HTTPError(response=response)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.clock = _Clock()
        log = logging.getLogger("vigil")
        previous_level = log.level
        log.setLevel(logging.CRITICAL)
        self.addCleanup(setattr, log, "level", previous_level)

    def config(self, fallbacks=()):
        return AgentConfig(server_url=PRIMARY, agent_token=TOKEN,
                           fallback_urls=list(fallbacks), primary_url=PRIMARY,
                           data_dir=self.dir)

    def endpoints(self, fallbacks=(), now=None):
        """A fresh Endpoints over a fresh config — like a restart."""
        config = self.config(fallbacks)
        return Endpoints(config, now=now or self.clock), config


class CandidateOrderTests(_Base):
    def test_primary_then_configured_then_learned(self):
        endpoints, _ = self.endpoints([FALLBACK_1])
        endpoints.learned = [LEARNED, FALLBACK_2]
        self.assertEqual(endpoints.candidates(),
                         [PRIMARY, FALLBACK_1, LEARNED, FALLBACK_2])

    def test_duplicates_are_dropped_and_the_first_place_kept(self):
        endpoints, _ = self.endpoints([FALLBACK_1, PRIMARY, FALLBACK_1])
        endpoints.learned = [FALLBACK_1, FALLBACK_2]
        self.assertEqual(endpoints.candidates(), [PRIMARY, FALLBACK_1, FALLBACK_2])

    def test_the_order_starts_at_active_and_wraps(self):
        endpoints, _ = self.endpoints([FALLBACK_1, FALLBACK_2])
        endpoints.use(FALLBACK_1)
        self.assertEqual(endpoints.order_from_active(),
                         [FALLBACK_1, FALLBACK_2, PRIMARY])


class ConstructionTests(_Base):
    def test_server_url_is_the_active_address(self):
        endpoints, config = self.endpoints([FALLBACK_1])
        self.assertEqual(config.server_url, PRIMARY)
        endpoints.use(FALLBACK_1)
        self.assertEqual(config.server_url, FALLBACK_1)

    def test_a_saved_active_address_survives_a_restart(self):
        endpoints, _ = self.endpoints([FALLBACK_1])
        endpoints.use(FALLBACK_1)
        restarted, config = self.endpoints([FALLBACK_1])
        self.assertEqual(restarted.active, FALLBACK_1)
        self.assertEqual(config.server_url, FALLBACK_1)

    def test_a_stored_active_the_operator_removed_is_ignored(self):
        endpoints, _ = self.endpoints([FALLBACK_1])
        endpoints.use(FALLBACK_1)
        restarted, config = self.endpoints([])
        self.assertEqual(restarted.active, PRIMARY)
        self.assertEqual(config.server_url, PRIMARY)


class StateFileTests(_Base):
    def test_state_file_is_owner_only_and_names_the_active_address(self):
        endpoints, _ = self.endpoints([FALLBACK_1])
        endpoints.use(FALLBACK_1)
        path = self.dir / "endpoints.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())["active"], FALLBACK_1)


class CheckinTests(_Base):
    def test_the_first_fallback_answers_when_the_primary_is_down(self):
        endpoints, config = self.endpoints([FALLBACK_1])

        def call():
            if config.server_url == PRIMARY:
                raise requests.ConnectionError("primary down")
            return {"status": "ok", "tasks": []}

        response = checkin(endpoints, call)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(config.server_url, FALLBACK_1)
        self.assertEqual(endpoints.active, FALLBACK_1)
        self.assertEqual(
            json.loads((self.dir / "endpoints.json").read_text())["active"],
            FALLBACK_1)

    def test_a_gateway_error_moves_on_but_a_401_is_the_agents_own_problem(self):
        endpoints, config = self.endpoints([FALLBACK_1])

        def refusing():
            if config.server_url == PRIMARY:
                raise _error(503)
            return {"status": "ok"}

        self.assertEqual(checkin(endpoints, refusing)["status"], "ok")
        self.assertEqual(endpoints.active, FALLBACK_1)

        endpoints.use(PRIMARY)

        def rejected():
            if config.server_url == PRIMARY:
                raise _error(401)
            return {"status": "ok"}

        with self.assertRaises(requests.HTTPError):
            checkin(endpoints, rejected)
        self.assertEqual(endpoints.active, PRIMARY)

    def test_a_timeout_also_moves_on(self):
        endpoints, config = self.endpoints([FALLBACK_1])

        def timing_out():
            if config.server_url == PRIMARY:
                raise requests.Timeout("too slow")
            return {"status": "ok"}

        self.assertEqual(checkin(endpoints, timing_out)["status"], "ok")
        self.assertEqual(endpoints.active, FALLBACK_1)

    def test_when_everything_is_down_the_agent_stays_where_it_was(self):
        endpoints, config = self.endpoints([FALLBACK_1, FALLBACK_2])

        def dead():
            raise requests.ConnectionError(f"no route to {config.server_url}")

        with self.assertRaises(requests.ConnectionError):
            checkin(endpoints, dead)
        self.assertEqual(endpoints.active, PRIMARY)
        self.assertEqual(config.server_url, PRIMARY)

    def _queued_primary(self, fallback_works):
        """A primary that only ever queues, and one fallback that is up or down."""
        endpoints, config = self.endpoints([FALLBACK_1])
        tried = []

        def call():
            tried.append(config.server_url)
            if config.server_url == PRIMARY:
                return {"status": "queued", "tasks": []}
            if fallback_works:
                return {"status": "ok", "tasks": []}
            raise requests.ConnectionError(f"{config.server_url} is down")

        for _ in range(QUEUED_LIMIT - 1):
            self.assertEqual(checkin(endpoints, call)["status"], "queued")
            self.assertEqual(endpoints.active, PRIMARY)
        self.assertNotIn(FALLBACK_1, tried)
        return endpoints, config, call, tried

    def test_the_third_wait_moves_to_the_next_address(self):
        endpoints, config, call, tried = self._queued_primary(fallback_works=True)
        self.assertEqual(checkin(endpoints, call)["status"], "ok")
        self.assertEqual(tried[-1], FALLBACK_1)
        self.assertEqual(endpoints.active, FALLBACK_1)
        self.assertEqual(config.server_url, FALLBACK_1)
        self.assertEqual(endpoints._queued, 0)

    def test_the_queued_answer_stands_when_the_next_address_is_down(self):
        endpoints, config, call, tried = self._queued_primary(fallback_works=False)
        self.assertEqual(checkin(endpoints, call)["status"], "queued")
        self.assertEqual(tried[-1], FALLBACK_1)
        # Parked on the address that answered, not the one that failed.
        self.assertEqual(endpoints.active, PRIMARY)
        self.assertEqual(config.server_url, PRIMARY)
        self.assertEqual(endpoints._queued, 0)

    def test_a_good_answer_resets_the_wait_counter(self):
        endpoints, _ = self.endpoints([FALLBACK_1])

        def queued_forever():
            return {"status": "queued", "tasks": []}

        for _ in range(QUEUED_LIMIT - 1):
            checkin(endpoints, queued_forever)
        checkin(endpoints, lambda: {"status": "ok"})
        self.assertEqual(checkin(endpoints, queued_forever)["status"], "queued")
        self.assertEqual(endpoints.active, PRIMARY)


class FailbackTests(_Base):
    @staticmethod
    def _down(config, dead_url, answer):
        """A check-in that refuses *dead_url* and answers everywhere else."""
        def call():
            if config.server_url == dead_url:
                raise requests.ConnectionError(f"{dead_url} is down")
            return answer

        return call

    @staticmethod
    def _spy(config, seen, answer):
        """A check-in that records which address each attempt used."""
        def call():
            seen.append(config.server_url)
            return answer

        return call

    def test_the_primary_is_tried_first_once_the_clock_runs_out(self):
        endpoints, config = self.endpoints([FALLBACK_1])
        checkin(endpoints, self._down(config, PRIMARY, {"status": "ok"}))
        self.assertEqual(endpoints.active, FALLBACK_1)

        # Not yet: the primary is not even attempted.
        tried = []
        self.clock.advance(599)
        checkin(endpoints, self._spy(config, tried, {"status": "ok"}))
        self.assertNotIn(PRIMARY, tried)

        self.clock.advance(1000)
        tried.clear()
        response = checkin(endpoints, self._spy(config, tried, {"status": "ok"}))
        self.assertEqual(tried[0], PRIMARY)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(endpoints.active, PRIMARY)

    def test_a_failed_failback_leaves_the_agent_on_its_fallback(self):
        endpoints, config = self.endpoints([FALLBACK_1])
        checkin(endpoints, self._down(config, PRIMARY, {"status": "ok"}))
        self.assertEqual(endpoints.active, FALLBACK_1)

        self.clock.advance(600)
        response = checkin(endpoints, self._down(config, PRIMARY, {"status": "ok"}))
        self.assertEqual(response["status"], "ok")
        self.assertEqual(endpoints.active, FALLBACK_1)


class LearnTests(_Base):
    def setUp(self):
        super().setUp()
        self.key = _Key()

    def test_a_list_signed_for_this_agent_is_stored(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key), self.key.verify_key)
        self.assertEqual(endpoints.learned, [LEARNED])

    def test_the_learned_list_survives_a_restart(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key), self.key.verify_key)
        restarted, _ = self.endpoints()
        self.assertEqual(restarted.learned, [LEARNED])
        self.assertIn(LEARNED, restarted.candidates())

    def test_the_learned_list_is_capped_at_ten(self):
        endpoints, _ = self.endpoints()
        urls = [f"https://r{i}.example.com" for i in range(15)]
        endpoints.learn(_response(urls, key=self.key), self.key.verify_key)
        self.assertEqual(endpoints.learned, urls[:10])

    def test_configured_fallbacks_survive_a_learn(self):
        endpoints, _ = self.endpoints([FALLBACK_1])
        endpoints.learn(_response([LEARNED], key=self.key), self.key.verify_key)
        self.assertEqual(endpoints.configured, [FALLBACK_1])
        self.assertEqual(endpoints.candidates(), [PRIMARY, FALLBACK_1, LEARNED])

    def test_a_list_signed_for_another_agent_is_ignored(self):
        endpoints, _ = self.endpoints()
        stolen = _response([LEARNED], agent=agent_fingerprint("other"), key=self.key)
        endpoints.learn(stolen, self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_a_url_swapped_in_after_signing_is_ignored(self):
        endpoints, _ = self.endpoints()
        body = _response([LEARNED], key=self.key)
        body["relays"] = ["https://evil.example.com"]
        endpoints.learn(body, self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_a_list_with_no_signature_is_ignored(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED]), self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_a_signature_from_someone_else_is_ignored(self):
        endpoints, _ = self.endpoints()
        stranger = _Key()
        endpoints.learn(_response([LEARNED], key=stranger), self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_an_address_that_is_not_http_is_ignored_even_signed(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response(["file:///tmp/x"], key=self.key), self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_an_https_agent_does_not_learn_plain_http_addresses(self):
        endpoints, _ = self.endpoints()
        body = _response(["http://plain.example.com", LEARNED], key=self.key)
        endpoints.learn(body, self.key.verify_key)
        self.assertEqual(endpoints.learned, [LEARNED])

    def test_an_older_list_is_a_replay_and_is_ignored(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key, issued=200), self.key.verify_key)
        old = _response(["https://removed.example.com"], key=self.key, issued=100)
        endpoints.learn(old, self.key.verify_key)
        self.assertEqual(endpoints.learned, [LEARNED])

    def test_the_newest_issue_time_survives_a_restart(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key, issued=200), self.key.verify_key)
        restarted, _ = self.endpoints()
        old = _response(["https://removed.example.com"], key=self.key, issued=100)
        restarted.learn(old, self.key.verify_key)
        self.assertEqual(restarted.learned, [LEARNED])

    def test_a_newer_list_replaces_the_old_one(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key, issued=100), self.key.verify_key)
        endpoints.learn(_response([FALLBACK_2], key=self.key, issued=200), self.key.verify_key)
        self.assertEqual(endpoints.learned, [FALLBACK_2])

    def test_an_issue_time_changed_after_signing_is_ignored(self):
        endpoints, _ = self.endpoints()
        body = _response([LEARNED], key=self.key, issued=100)
        body["relays_issued"] = 999
        endpoints.learn(body, self.key.verify_key)
        self.assertEqual(endpoints.learned, [])

    def test_nothing_happens_without_a_pinned_key_or_a_list(self):
        endpoints, _ = self.endpoints()
        endpoints.learn(_response([LEARNED], key=self.key), None)
        endpoints.learn({"status": "ok"}, self.key.verify_key)
        self.assertEqual(endpoints.learned, [])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def load(self, body):
        path = self.dir / "agent.yml"
        path.write_text(body)
        return load_config(path)

    def test_fallback_urls_parse_and_primary_url_is_kept(self):
        config = self.load(
            f"server_url: {PRIMARY}\n"
            "fallback_urls:\n"
            f"  - {FALLBACK_1}/\n"
            f"  - {FALLBACK_2}\n"
            f"agent_token: {'t' * 64}\n")
        self.assertEqual(config.fallback_urls, [FALLBACK_1, FALLBACK_2])
        self.assertEqual(config.primary_url, PRIMARY)
        self.assertEqual(config.server_url, PRIMARY)

    def test_a_duplicate_of_server_url_is_dropped(self):
        config = self.load(
            f"server_url: {PRIMARY}\n"
            "fallback_urls:\n"
            f"  - {PRIMARY}/\n"
            f"agent_token: {'t' * 64}\n")
        self.assertEqual(config.fallback_urls, [])

    def test_a_non_http_fallback_is_refused(self):
        with self.assertRaises(ValueError):
            self.load(f"server_url: {PRIMARY}\n"
                      "fallback_urls:\n"
                      "  - ftp://x\n"
                      f"agent_token: {'t' * 64}\n")

    def test_plain_http_is_refused_under_an_https_server(self):
        with self.assertRaises(ValueError):
            self.load(f"server_url: {PRIMARY}\n"
                      "fallback_urls:\n"
                      "  - http://relay.internal\n"
                      f"agent_token: {'t' * 64}\n")

    def test_plain_http_is_allowed_when_the_server_is_plain_http(self):
        config = self.load("server_url: http://vigil.local\n"
                           "fallback_urls:\n"
                           "  - http://relay.local/\n"
                           f"agent_token: {'t' * 64}\n")
        self.assertEqual(config.fallback_urls, ["http://relay.local"])

    def test_more_than_ten_addresses_is_refused(self):
        urls = "".join(f"  - https://r{i}.example.com\n" for i in range(11))
        with self.assertRaises(ValueError):
            self.load(f"server_url: {PRIMARY}\n"
                      f"fallback_urls:\n{urls}"
                      f"agent_token: {'t' * 64}\n")


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    """run_agent is too big to drive here, so check it calls both halves."""

    def test_run_agent_checks_in_through_failover_and_learns_the_list(self):
        import inspect

        from vigil_agent.__main__ import run_agent
        source = inspect.getsource(run_agent)
        self.assertIn("failover.Endpoints(config)", source)
        self.assertIn("failover.checkin(", source)
        self.assertIn("endpoints.learn(response, verify_key)", source)
