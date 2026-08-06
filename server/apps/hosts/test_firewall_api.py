"""The Firewall tab's endpoints. Writes are 2FA-gated and guarded; reads are
not, so the tab can render before anyone has typed a code."""
import secrets

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.timezone import now

from apps.hosts.models import Host, HostFirewall
from apps.tasks.models import Task

SNAPSHOT = {"tool": "ufw", "supported": True, "enabled": True,
            "defaults": {"incoming": "deny", "outgoing": "allow"},
            "rules": [{"port": 22, "protocol": "tcp", "action": "allow",
                       "source": "any", "interface": ""}]}


class FirewallApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.host = Host.objects.create(
            hostname="fw1", agent_token=secrets.token_hex(16),
            status=Host.Status.ONLINE, mode=Host.Mode.FULL_CONTROL,
            os="Ubuntu 24.04")

    def _snapshot(self, **overrides):
        defaults = {"tool": "ufw", "supported": True, "enabled": True,
                    "defaults": SNAPSHOT["defaults"],
                    "rules": SNAPSHOT["rules"]}
        defaults.update(overrides)
        HostFirewall.objects.update_or_create(host=self.host, defaults=defaults)

    def _totp_user(self):
        """Enroll self.user with a confirmed TOTP secret and return a valid code."""
        from apps.accounts.models import UserProfile
        from apps.accounts.totp import generate_secret, generate_totp

        secret = generate_secret()
        profile, _ = UserProfile.objects.get_or_create(user=self.user)
        profile.totp_secret = secret
        profile.totp_confirmed_at = now()
        profile.save()
        return generate_totp(secret)

    def test_get_before_any_read_says_so(self):
        r = self.client.get(f"/api/v1/hosts/{self.host.id}/firewall/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["fetched_at"])

    def test_get_returns_the_snapshot(self):
        self._snapshot()
        r = self.client.get(f"/api/v1/hosts/{self.host.id}/firewall/")
        self.assertEqual(r.json()["rules"][0]["port"], 22)
        self.assertIsNotNone(r.json()["fetched_at"])

    def test_refresh_queues_a_read_task(self):
        r = self.client.post(f"/api/v1/hosts/{self.host.id}/firewall/refresh/")
        self.assertEqual(r.status_code, 202, r.content)
        self.assertTrue(Task.objects.filter(
            host=self.host, action="list_firewall_rules").exists())

    def test_refresh_does_not_stack_duplicate_reads(self):
        self.client.post(f"/api/v1/hosts/{self.host.id}/firewall/refresh/")
        self.client.post(f"/api/v1/hosts/{self.host.id}/firewall/refresh/")
        self.assertEqual(Task.objects.filter(
            host=self.host, action="list_firewall_rules").count(), 1)

    def test_apply_requires_totp(self):
        self._snapshot()
        r = self.client.post(
            f"/api/v1/hosts/{self.host.id}/firewall/apply/",
            {"action": "add_firewall_rule",
             "params": {"port": 8080, "protocol": "tcp"}},
            content_type="application/json")
        self.assertEqual(r.status_code, 401)

    def test_apply_refuses_a_lockout_before_asking_for_totp(self):
        """The refusal is about the change, not the credential — asking for a
        code first and then refusing wastes the operator's time."""
        self._snapshot()
        r = self.client.post(
            f"/api/v1/hosts/{self.host.id}/firewall/apply/",
            {"action": "set_firewall_policy",
             "params": {"direction": "outgoing", "policy": "deny"}},
            content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("check in", r.json()["error"].lower())

    def test_apply_rejects_an_unknown_action(self):
        r = self.client.post(
            f"/api/v1/hosts/{self.host.id}/firewall/apply/",
            {"action": "rm_rf_slash", "params": {}},
            content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_monitor_mode_hosts_are_refused(self):
        self.host.mode = Host.Mode.MONITOR
        self.host.save()
        r = self.client.post(f"/api/v1/hosts/{self.host.id}/firewall/refresh/")
        self.assertEqual(r.status_code, 400)

    # -- Additional coverage beyond the brief --------------------------------

    def test_get_stored_enabled_none_serializes_as_null(self):
        """``enabled=None`` means "could not read the firewall state" — the
        GET response must not collapse that into JSON `false`."""
        self._snapshot(tool="windows", enabled=None)
        r = self.client.get(f"/api/v1/hosts/{self.host.id}/firewall/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("enabled", body)
        self.assertIsNone(body["enabled"])
        self.assertNotEqual(body["enabled"], False)

    def test_get_includes_unparsed(self):
        unparsed = [{"raw": "allow OpenSSH"}]
        HostFirewall.objects.update_or_create(
            host=self.host,
            defaults={"tool": "ufw", "supported": True, "enabled": True,
                      "defaults": SNAPSHOT["defaults"],
                      "rules": SNAPSHOT["rules"], "unparsed": unparsed})
        r = self.client.get(f"/api/v1/hosts/{self.host.id}/firewall/")
        self.assertEqual(r.json()["unparsed"], unparsed)

    def test_allowlist_agrees_with_guards_known_actions(self):
        """If the endpoint's allowlist and the guard's known-action set
        drift, a legitimate action becomes permanently refused."""
        from apps.hosts.firewall_guard import check_change
        from apps.hosts.views import _FIREWALL_ACTIONS

        for action in _FIREWALL_ACTIONS:
            refusal = check_change(self.host, None, {"action": action, "params": {}})
            self.assertNotIn(
                "unrecognised firewall action", refusal,
                f"{action!r} is in _FIREWALL_ACTIONS but the guard does not "
                "recognise it — the two lists have drifted apart")

    def test_apply_lockout_refusal_without_totp_proves_guard_runs_first(self):
        """A different lockout path than the brief's own test (removing the
        rule that permits the protected SSH port), still with no ``totp``
        supplied at all — 400, never 401."""
        self._snapshot()
        r = self.client.post(
            f"/api/v1/hosts/{self.host.id}/firewall/apply/",
            {"action": "remove_firewall_rule",
             "params": {"port": 22, "protocol": "tcp"}},
            content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertNotEqual(r.status_code, 401)

    def test_apply_outgoing_deny_refused_even_with_valid_totp(self):
        """set_firewall_policy(outgoing, deny) is refused with 400 even when
        a valid TOTP code is supplied — the refusal is about the change,
        not about authentication."""
        totp = self._totp_user()
        self._snapshot()
        r = self.client.post(
            f"/api/v1/hosts/{self.host.id}/firewall/apply/",
            {"action": "set_firewall_policy",
             "params": {"direction": "outgoing", "policy": "deny"},
             "totp": totp},
            content_type="application/json")
        self.assertEqual(r.status_code, 400)
