"""A firewall snapshot has to land in the database and say so when it cannot.

Same lesson as Trivy ingest: a read that silently fails looks exactly like a
host with no rules, which is the most misleading thing this feature could show.
"""
import json
import secrets

from django.test import TestCase

from apps.hosts.models import Host, HostFirewall
from apps.tasks.models import Task
from apps.tasks.views import _maybe_ingest_firewall_rules

SNAPSHOT = {
    "tool": "ufw", "supported": True, "enabled": True,
    "defaults": {"incoming": "deny", "outgoing": "allow"},
    "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
               "source": "any", "interface": ""}],
}


class FirewallIngestTests(TestCase):
    def setUp(self):
        self.host = Host.objects.create(
            hostname="fw1", agent_token="f" * 32, status=Host.Status.ONLINE)

    def _complete(self, output, action="list_firewall_rules"):
        task = Task.objects.create(
            host=self.host, action=action, state=Task.State.COMPLETED,
            nonce=secrets.token_hex(16), result_output=output)
        _maybe_ingest_firewall_rules(task, output)
        task.refresh_from_db()
        return task

    def test_a_snapshot_is_stored(self):
        self._complete(json.dumps(SNAPSHOT))
        fw = HostFirewall.objects.get(host=self.host)
        self.assertEqual(fw.tool, "ufw")
        self.assertTrue(fw.enabled)
        self.assertEqual(fw.rules[0]["port"], 22)

    def test_re_reading_replaces_rather_than_duplicates(self):
        self._complete(json.dumps(SNAPSHOT))
        second = {**SNAPSHOT, "rules": []}
        self._complete(json.dumps(second))
        self.assertEqual(HostFirewall.objects.filter(host=self.host).count(), 1)
        self.assertEqual(HostFirewall.objects.get(host=self.host).rules, [])

    def test_a_multi_step_transcript_is_handled(self):
        out = f"[OK] read: {json.dumps(SNAPSHOT)}"
        task = Task.objects.create(
            host=self.host, action="_script",
            params={"steps": [{"action": "list_firewall_rules"}]},
            state=Task.State.COMPLETED, nonce=secrets.token_hex(16),
            result_output=out)
        _maybe_ingest_firewall_rules(task, out)
        self.assertTrue(HostFirewall.objects.filter(host=self.host).exists())

    def test_an_unsupported_host_is_recorded_as_unsupported(self):
        self._complete(json.dumps({"tool": None, "supported": False,
                                   "enabled": False,
                                   "defaults": {"incoming": "unknown",
                                                "outgoing": "unknown"},
                                   "rules": []}))
        self.assertFalse(HostFirewall.objects.get(host=self.host).supported)

    def test_unparseable_output_is_surfaced_in_run_details(self):
        task = self._complete("ufw: command not found")
        self.assertIn("[FIREWALL INGEST FAILED]", task.result_output)

    def test_unparseable_output_does_not_wipe_a_good_snapshot(self):
        self._complete(json.dumps(SNAPSHOT))
        self._complete("ufw: command not found")
        self.assertEqual(
            HostFirewall.objects.get(host=self.host).rules[0]["port"], 22)

    def test_a_task_that_is_not_a_firewall_read_is_ignored(self):
        self._complete(json.dumps(SNAPSHOT), action="restart_service")
        self.assertFalse(HostFirewall.objects.filter(host=self.host).exists())

    def test_unparsed_items_are_stored_and_round_trip(self):
        snapshot = {**SNAPSHOT, "unparsed": ["OpenSSH  ALLOW IN  Anywhere"]}
        self._complete(json.dumps(snapshot))
        fw = HostFirewall.objects.get(host=self.host)
        self.assertEqual(fw.unparsed, ["OpenSSH  ALLOW IN  Anywhere"])
        fw.refresh_from_db()
        self.assertEqual(fw.unparsed, ["OpenSSH  ALLOW IN  Anywhere"])

    def test_windows_unknown_enabled_state_stores_none(self):
        snapshot = {
            "tool": "windows", "supported": True, "enabled": None,
            "defaults": {"incoming": "unknown", "outgoing": "unknown"},
            "profiles": [], "rules": [],
            "unparsed": ["<failed to read Windows firewall profiles>"],
        }
        self._complete(json.dumps(snapshot))
        fw = HostFirewall.objects.get(host=self.host)
        self.assertIsNone(fw.enabled)

    def test_unknown_defaults_are_stored_unchanged(self):
        snapshot = {**SNAPSHOT,
                    "defaults": {"incoming": "unknown", "outgoing": "unknown"}}
        self._complete(json.dumps(snapshot))
        fw = HostFirewall.objects.get(host=self.host)
        self.assertEqual(fw.defaults, {"incoming": "unknown", "outgoing": "unknown"})

    def test_off_contract_enabled_value_does_not_raise_or_corrupt_the_task(self):
        # "enabled": "unknown" is well-formed JSON, merely off-contract --
        # Django's nullable BooleanField raises a ValidationError for any
        # value outside {True, False, None, "t"/"True"/"1", "f"/"False"/"0"}
        # when the row is written. That must be caught inside the hook: an
        # escape here runs inside task_result's transaction and would roll
        # back the task's own COMPLETED state write alongside it, 500-ing
        # the endpoint every agent uses to report every task.
        bad = {**SNAPSHOT, "enabled": "unknown"}
        task = self._complete(json.dumps(bad))
        self.assertEqual(task.state, Task.State.COMPLETED)
        self.assertIn("[FIREWALL INGEST FAILED]", task.result_output)

    def test_non_list_rules_value_does_not_raise_or_corrupt_the_task(self):
        bad = {**SNAPSHOT, "rules": "not-a-list"}
        task = self._complete(json.dumps(bad))
        self.assertEqual(task.state, Task.State.COMPLETED)
        self.assertIn("[FIREWALL INGEST FAILED]", task.result_output)

    def test_prior_good_snapshot_survives_both_kinds_of_bad_write(self):
        self._complete(json.dumps(SNAPSHOT))
        self._complete(json.dumps({**SNAPSHOT, "enabled": "unknown"}))
        self._complete(json.dumps({**SNAPSHOT, "rules": "not-a-list"}))
        fw = HostFirewall.objects.get(host=self.host)
        self.assertTrue(fw.enabled)
        self.assertEqual(fw.rules[0]["port"], 22)

    def test_an_earlier_steps_generic_rules_key_does_not_win(self):
        # A step's own output could legitimately be JSON with a "rules" key
        # for unrelated reasons. The real firewall snapshot must still be
        # the one selected -- the discriminator requires "tool" or
        # "supported" alongside "rules", which only a real snapshot has.
        decoy = json.dumps({"rules": ["not", "a", "firewall", "snapshot"]})
        out = (f"[OK] earlier_step: {decoy}\n"
               f"[OK] read: {json.dumps(SNAPSHOT)}")
        task = Task.objects.create(
            host=self.host, action="_script",
            params={"steps": [{"action": "some_other_step"},
                               {"action": "list_firewall_rules"}]},
            state=Task.State.COMPLETED, nonce=secrets.token_hex(16),
            result_output=out)
        _maybe_ingest_firewall_rules(task, out)
        fw = HostFirewall.objects.get(host=self.host)
        self.assertEqual(fw.tool, "ufw")
        self.assertEqual(fw.rules[0]["port"], 22)
