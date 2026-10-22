"""What the Connect menu is told it may offer, and what it must grey out.

``GET /api/v1/sessions/options/`` answers "can *this* user open *this* kind of
session on *this* host" — so its reasons are ``_start``'s refusals read before
the TOTP code is spent. These tests assert the wording as well as the verdict:
an operator who reads "Turn on allow_remote_shell in this host's agent.yml."
should be able to act on that sentence without opening the source.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host

from vigil.testsupport import require_business_module

# Exercises core with the Business apps; a source checkout has none.
require_business_module()
from apps_business.sites.models import (  # noqa: E402
    HostSiteAssignment,
    Site,
    SiteCapability,
    UserSiteRole,
)

AGENT_TOKEN = "a" * 64


def _token(n: int) -> str:
    """Distinct per host: the token column is unique by design."""
    return f"{n:064x}"


def make_user(username: str, role: str):
    user = get_user_model().objects.create_user(username=username, password="pw")
    UserProfile.objects.create(user=user, role=role)
    return user


def enroll_totp(user, secret: str = "JBSWY3DPEHPK3PXP") -> None:
    from django.utils.timezone import now

    profile = user.profile
    profile.totp_secret = secret
    profile.totp_confirmed_at = now()
    profile.save(update_fields=["totp_secret_encrypted", "totp_confirmed_at"])


@override_settings(VIGIL_SESSION_BUS="memory")
class OptionsTests(TestCase):
    """What the menu is told. Asking never writes a session row."""

    def setUp(self):
        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="linux-box", agent_token=AGENT_TOKEN, os="Ubuntu 24.04",
            ip_address="10.0.0.7", status=Host.Status.ONLINE, mode=Host.Mode.MANAGED,
            agent_allow_remote_shell=True)
        self.admin = make_user("root", Role.ADMIN)
        enroll_totp(self.admin)
        self.client.force_authenticate(self.admin)

    def options(self, host=None):
        target = self.host if host is None else host
        resp = self.client.get("/api/v1/sessions/options/",
                               {"host": str(target.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    # -- the shape ---------------------------------------------------------

    def test_an_admin_with_two_factor_on_an_opted_in_linux_host_can_shell(self):
        body = self.options()
        self.assertEqual(body["host"], "linux-box")
        self.assertFalse(body["is_windows"])
        self.assertEqual(body["ssh_target"], "10.0.0.7")
        self.assertEqual(body["shell"], {"available": True, "reason": ""})
        self.assertEqual(body["container"]["available"], True)
        self.assertFalse(body["rdp"]["available"])
        self.assertEqual(body["rdp"]["reason"], "Web RDP is for Windows hosts.")

    def test_the_hostname_is_the_fallback_when_no_address_was_reported(self):
        Host.objects.filter(pk=self.host.pk).update(ip_address=None)
        self.assertEqual(self.options()["ssh_target"], "linux-box")

    # -- the reasons, one per check ---------------------------------------

    def test_a_host_that_has_not_opted_in_says_which_switch_to_turn_on(self):
        Host.objects.filter(pk=self.host.pk).update(agent_allow_remote_shell=False)
        body = self.options()
        self.assertFalse(body["shell"]["available"])
        self.assertEqual(body["shell"]["reason"],
                         "Turn on allow_remote_shell in this host's agent.yml.")

    def test_a_viewer_is_told_it_is_a_question_of_permission(self):
        self.client.force_authenticate(make_user("watcher", Role.VIEWER))
        body = self.options()
        for item in ("shell", "container", "rdp"):
            self.assertEqual(body[item]["reason"],
                             "You don't have permission to open shells here.")

    def test_an_operator_without_the_shell_grant_is_refused_by_permission(self):
        operator = make_user("op", Role.OPERATOR)
        row = UserSiteRole.objects.create(user=operator, site=Site.objects.global_site(),
                                          role=Role.OPERATOR)
        SiteCapability.objects.create(user_site_role=row, app="hosts", verb="edit")
        self.client.force_authenticate(operator)
        self.assertEqual(self.options()["shell"]["reason"],
                         "You don't have permission to open shells here.")

    def test_monitor_mode_beats_the_opt_in_it_would_otherwise_report(self):
        Host.objects.filter(pk=self.host.pk).update(mode=Host.Mode.MONITOR,
                                                    agent_allow_remote_shell=False)
        self.assertEqual(self.options()["shell"]["reason"], "This host is in monitor mode.")

    def test_a_user_with_no_second_factor_is_pointed_at_enrolment(self):
        profile = self.admin.profile
        profile.totp_confirmed_at = None
        profile.save(update_fields=["totp_confirmed_at"])
        self.assertEqual(self.options()["shell"]["reason"],
                         "Set up two-factor authentication before opening a session.")

    def test_a_windows_host_refuses_container_shells_but_offers_the_desktop(self):
        windows = Host.objects.create(
            hostname="win-box", agent_token=_token(1), os="Windows Server 2022",
            mode=Host.Mode.MANAGED, agent_allow_remote_shell=True,
            agent_allow_remote_desktop=True)
        with override_settings(VIGIL_GUACD_ADDR="guacd:4822"):
            body = self.options(windows)
        self.assertTrue(body["is_windows"])
        self.assertEqual(body["shell"]["available"], True)
        self.assertEqual(body["container"],
                         {"available": False, "reason": "Container shells need a Linux host."})
        self.assertEqual(body["rdp"], {"available": True, "reason": ""})

    def test_the_desktop_needs_guacd_running_and_says_so_in_plain_words(self):
        windows = Host.objects.create(
            hostname="win2", agent_token=_token(2), os="Windows 11",
            mode=Host.Mode.MANAGED, agent_allow_remote_desktop=True)

        self.assertEqual(self.options(windows)["rdp"]["reason"],
                         "Start the guacd service to use Web RDP (see the wiki).")
        with override_settings(VIGIL_GUACD_ADDR="guacd:4822"):
            self.assertEqual(self.options(windows)["rdp"], {"available": True, "reason": ""})

    def test_the_desktop_reports_its_own_switch_rather_than_the_shell_one(self):
        # Opted in for shells but not for the desktop: the two switches are
        # separate, and each item must name the one that applies to it.
        windows = Host.objects.create(
            hostname="win3", agent_token=_token(3), os="Windows 11",
            mode=Host.Mode.MANAGED, agent_allow_remote_shell=True)
        with override_settings(VIGIL_GUACD_ADDR="guacd:4822"):
            body = self.options(windows)
        self.assertEqual(body["shell"]["available"], True)
        self.assertEqual(body["rdp"]["reason"],
                         "Turn on allow_remote_desktop in this host's agent.yml.")

    # -- recording ---------------------------------------------------------

    def test_recorded_follows_the_audit_log_feature(self):
        with patch("apps.remote.views.has_feature", return_value=False):
            self.assertFalse(self.options()["recorded"])
        with patch("apps.remote.views.has_feature", return_value=True):
            self.assertTrue(self.options()["recorded"])

    # -- who may ask -------------------------------------------------------

    def test_anonymous_may_not_ask(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get("/api/v1/sessions/options/",
                                      {"host": str(self.host.id)}).status_code,
                      (401, 403))

    def test_a_host_in_another_site_is_not_found_rather_than_denied(self):
        elsewhere = Site.objects.create(name="Elsewhere", slug="elsewhere")
        HostSiteAssignment.objects.create(host=self.host, site=elsewhere)
        west_admin = make_user("west", Role.VIEWER)
        UserSiteRole.objects.create(user=west_admin,
                                   site=Site.objects.create(name="West", slug="west2"),
                                   role=Role.ADMIN)
        self.client.force_authenticate(west_admin)

        resp = self.client.get("/api/v1/sessions/options/", {"host": str(self.host.id)})
        self.assertEqual(resp.status_code, 404)

    def test_an_id_that_is_not_a_host_answered_the_same_way_as_no_host(self):
        # Not a 500: the id reaches this view from a card in the browser, and a
        # typo there must not read as a server fault.
        self.client.get("/api/v1/sessions/options/", {"host": "not-a-uuid"})
        resp = self.client.get("/api/v1/sessions/options/", {"host": "not-a-uuid"})
        self.assertEqual(resp.status_code, 404)


@override_settings(VIGIL_SESSION_BUS="memory")
class StartResponseRecordedTests(TestCase):
    def setUp(self):
        from unittest.mock import patch as _patch

        self.client = APIClient()
        self.host = Host.objects.create(
            hostname="taped-box", agent_token=AGENT_TOKEN, mode=Host.Mode.MANAGED,
            agent_allow_remote_shell=True, agent_allow_remote_desktop=True)
        self.admin = make_user("root", Role.ADMIN)
        enroll_totp(self.admin)
        self.client.force_authenticate(self.admin)
        self._patcher = _patch("apps.accounts.totp.consume_totp", return_value=(True, None))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def post(self, **body):
        payload = {"host": str(self.host.id), "kind": "shell", "totp": "123456"}
        payload.update(body)
        resp = self.client.post("/api/v1/sessions/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_a_recorded_install_says_so_in_the_start_response(self):
        with patch("apps.remote.views.has_feature", return_value=True):
            self.assertTrue(self.post()["recorded"])

    def test_without_the_feature_there_is_nothing_to_tell_them_about(self):
        with patch("apps.remote.views.has_feature", return_value=False):
            self.assertFalse(self.post()["recorded"])

    def test_the_desktop_is_not_recorded_here_so_the_pill_stays_off(self):
        # The cast file is the agent's PTY stream; guacd records its own session
        # (QA-59), so a "Recorded" pill on an rdp dialog would be a lie.
        with patch("apps.remote.views.has_feature", return_value=True):
            self.assertFalse(self.post(kind="rdp")["recorded"])
