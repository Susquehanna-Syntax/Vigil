"""``GET /api/v1/instance-health/``: Vigil's own counts for the About page.

An instance admin sees the install-wide numbers ``/metrics`` serves; a viewer
and a per-site admin are refused, because these counts cover every site.
"""
import base64
import os
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Role, UserProfile
from apps.hosts.models import Host
from vigil import signing

URL = "/api/v1/instance-health/"
METRICS_URL = "/metrics"
METRICS_TOKEN = "t0ken-for-tests"
NEXT_SEED = base64.b64encode(b"\x07" * 32).decode()


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="x")
    UserProfile.objects.create(user=user, role=role)
    return user


def _host(name, status, checked_in_minutes_ago=None, signing_key_ack=""):
    return Host.objects.create(
        hostname=name,
        agent_token=f"t-{name}",
        status=status,
        signing_key_ack=signing_key_ack,
        last_checkin=(timezone.now() - timezone.timedelta(minutes=checked_in_minutes_ago)
                      if checked_in_minutes_ago is not None else None),
    )


class InstanceHealthTests(TestCase):
    def setUp(self):
        self.admin = _user("admin", Role.ADMIN)
        self.viewer = _user("wendell", Role.VIEWER)

    def test_an_instance_admin_sees_the_counts(self):
        _host("on-1", Host.Status.ONLINE, 0)
        _host("new-1", Host.Status.PENDING)
        self.client.force_login(self.admin)
        with mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=3):
            response = self.client.get(URL)
            body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["hosts"]["online"], 1)
        self.assertEqual(body["hosts"]["pending"], 1)
        self.assertIs(body["celery_broker_up"], True)
        self.assertEqual(body["celery_queue_depth"], 3)
        self.assertEqual(len(body["signing_key"]["active"]), 44)
        self.assertIsNone(body["signing_key"]["next"])

    def test_a_viewer_is_refused(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_a_site_admin_is_refused(self):
        if not settings.BUSINESS_INSTALLED:
            self.skipTest("Business not installed")
        from apps_business.sites.models import Site, UserSiteRole

        site = Site.objects.create(name="West", slug="west")
        UserSiteRole.objects.create(user=self.viewer, site=site, role=Role.ADMIN)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(URL).status_code, 403)


class SigningRotationTests(TestCase):
    def setUp(self):
        self.admin = _user("keyadmin", Role.ADMIN)
        signing.reset_cache()
        self.addCleanup(signing.reset_cache)

    def test_during_a_rotation_it_counts_acknowledgements(self):
        with override_settings(VIGIL_SIGNING_KEY_NEXT_SEED=NEXT_SEED):
            signing.reset_cache()
            nxt = signing.public_b64(signing.next_signing_key())
            _host("ack-1", Host.Status.ONLINE, 0, signing_key_ack=nxt)
            _host("quiet-1", Host.Status.ONLINE, 0)
            self.client.force_login(self.admin)
            with mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=3):
                body = self.client.get(URL).json()
        self.assertEqual(body["signing_key"]["next"], nxt)
        self.assertEqual(body["signing_key"]["acknowledged"], 1)
        self.assertEqual(body["signing_key"]["hosts"], 2)


class MetricsAgreementTests(TestCase):
    def setUp(self):
        self.admin = _user("bothviews", Role.ADMIN)
        get_user_model().objects.create_user("someoneelse", password="x")

    def test_metrics_and_health_agree(self):
        _host("agreed-1", Host.Status.ONLINE, 0)
        self.client.force_login(self.admin)
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": METRICS_TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=3):
            metrics_body = self.client.get(
                METRICS_URL, HTTP_AUTHORIZATION=f"Bearer {METRICS_TOKEN}").content.decode()
            health = self.client.get(URL).json()
        self.assertIn(f'vigil_hosts{{status="online"}} {health["hosts"]["online"]}', metrics_body)
