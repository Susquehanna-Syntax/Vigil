"""The Patching tab: compiled steps, deferral, decisions, reboots and drift."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils.timezone import now

from apps.software.models import PendingUpdate, UpdateDecision
from apps.tasks.models import Task

from .compile import compile_policy
from .drift import policy_drift
from .patch import eligible_kbs, patch_steps
from .run import run_policy
from .test_drift import make_host
from .test_policy_api import admin_client
from .test_run import _TOTP, make_policy


def pending(host, key, days_ago=0, kind="windows", classification="Security Updates"):
    stamp = now() - timedelta(days=days_ago)
    return PendingUpdate.objects.create(host=host, kind=kind, key=key, title=key,
                                        classification=classification,
                                        first_seen=stamp, last_seen=now())


def steps_by_id(policy):
    return {s["id"]: s for s in patch_steps(policy)}


class PatchStepTests(TestCase):
    def setUp(self):
        self.host = make_host("win-1")

    def test_off_means_no_steps(self):
        self.assertEqual(patch_steps(make_policy(rules=[])), [])

    def test_deferral_decides_the_include_list(self):
        pending(self.host, "KB1", days_ago=10)
        pending(self.host, "KB2", days_ago=2)
        UpdateDecision.objects.create(kind="windows", key="KB2", decision="approved")
        pending(self.host, "KB3", days_ago=30)
        UpdateDecision.objects.create(kind="windows", key="KB3", decision="declined")
        policy = make_policy(rules=[], patch_enabled=True, deferral_days=7,
                             windows_classifications=["Security Updates"])
        self.assertEqual(eligible_kbs(policy), ["KB1", "KB2"])
        wu = steps_by_id(policy)["windows_updates"]
        self.assertEqual(wu["params"], {"classifications": ["Security Updates"],
                                        "include_kb": ["KB1", "KB2"],
                                        "exclude_kb": ["KB3"]})
        self.assertEqual(wu["when"], 'agent.os == "windows"')

    def test_nothing_eligible_installs_nothing_on_windows(self):
        pending(self.host, "KB9", days_ago=1)
        ids = steps_by_id(make_policy(rules=[], patch_enabled=True, deferral_days=7))
        self.assertNotIn("windows_updates", ids,
                         "an empty include_kb would mean every update to the agent")
        self.assertIn("linux_updates", ids)

    def test_no_deferral_needs_no_include_list(self):
        wu = steps_by_id(make_policy(rules=[], patch_enabled=True, deferral_days=0))
        self.assertNotIn("include_kb", wu["windows_updates"]["params"])

    def test_reboot_modes(self):
        never = steps_by_id(make_policy(name="n", rules=[], patch_enabled=True,
                                        deferral_days=0, reboot="never"))
        self.assertNotIn("reboot", never)
        in_window = steps_by_id(make_policy(name="w", rules=[], patch_enabled=True,
                                            deferral_days=0, reboot="in_window"))
        self.assertIn("steps.windows_updates.result.reboot_required == True",
                      in_window["reboot"]["when"])
        self.assertNotIn("defer_limit", in_window["reboot"]["params"])
        ask = steps_by_id(make_policy(name="a", rules=[], patch_enabled=True,
                                      deferral_days=0, reboot="ask"))
        self.assertTrue(ask["reboot"]["params"]["notify"])
        self.assertGreater(ask["reboot"]["params"]["defer_limit"], 0)

    def test_linux_updates_mode(self):
        sec = steps_by_id(make_policy(name="s", rules=[], patch_enabled=True))
        self.assertEqual(sec["linux_updates"]["params"], {"security_only": True})
        every = steps_by_id(make_policy(name="e", rules=[], patch_enabled=True,
                                        linux_updates="all"))
        self.assertEqual(every["linux_updates"]["params"], {"security_only": False})

    def test_compiled_task_validates_and_is_high_risk_with_a_reboot(self):
        policy = make_policy(patch_enabled=True, deferral_days=0, reboot="in_window")
        definition = compile_policy(policy)
        self.assertEqual(definition.risk_level, "high")
        self.assertEqual([a["id"] for a in definition.parsed_spec["actions"]],
                         ["app-1", "windows_updates", "reboot", "linux_updates"])
        quiet = compile_policy(make_policy(name="q", patch_enabled=True, reboot="never"))
        self.assertEqual(quiet.risk_level, "standard")


class PatchDriftAndRunTests(TestCase):
    def setUp(self):
        self.client, self.user = admin_client()
        self.win = make_host("win-1")
        self.lin = make_host("lin-1")
        pending(self.win, "KB1", days_ago=10)
        pending(self.win, "KB5", days_ago=10, classification="Drivers")
        pending(self.lin, "curl", kind="linux", classification="dpkg")

    def test_pending_updates_join_drift(self):
        policy = make_policy(rules=[], patch_enabled=True, deferral_days=7,
                             windows_classifications=["Security Updates"], reboot="never")
        drift = {r["hostname"]: r["changes"] for r in policy_drift(policy)["hosts"]}
        self.assertEqual([c["update"] for c in drift["win-1"]], ["KB1"])
        self.assertEqual([c["update"] for c in drift["lin-1"]], ["curl"])
        UpdateDecision.objects.create(kind="windows", key="KB1", decision="declined")
        drift = {r["hostname"] for r in policy_drift(policy)["hosts"]}
        self.assertEqual(drift, {"lin-1"})

    def test_a_reboot_policy_refuses_to_run_until_opted_in(self):
        policy = make_policy(rules=[], patch_enabled=True, deferral_days=0,
                             reboot="in_window")
        result = run_policy(policy, user=self.user)
        self.assertIn("high risk", result["error"])
        self.assertFalse(Task.objects.exists())

        with mock.patch(_TOTP, return_value="bad"):
            resp = self.client.put(f"/api/v1/policies/{policy.id}/",
                                   {"allow_high_risk": True}, format="json")
        self.assertEqual(resp.status_code, 401)
        with mock.patch(_TOTP, return_value=None):
            resp = self.client.put(f"/api/v1/policies/{policy.id}/",
                                   {"allow_high_risk": True, "totp": "1"}, format="json")
        self.assertTrue(resp.json()["allow_high_risk"])
        self.assertTrue(resp.json()["high_risk"])
        policy.refresh_from_db()
        result = run_policy(policy, user=self.user)
        self.assertEqual(result["dispatched"], 2)
        task = Task.objects.filter(host=self.win).get()
        self.assertEqual(task.risk_level, "high")
