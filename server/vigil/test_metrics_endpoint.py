"""``GET /metrics``: a bearer token in, Prometheus text out (S3 05b).

Without ``VIGIL_METRICS_TOKEN`` the route is a 404; with it, the body is the
database's own counts at scrape time — never a per-worker counter.
"""
import os
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.hosts.models import Host
from vigil import metrics_endpoint

URL = "/metrics"
TOKEN = "t0ken-for-tests"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
SAMPLE = re.compile(r'^[a-z_]+(\{[a-z_]+="[^"]*"(,[a-z_]+="[^"]*")*\})? -?[0-9.]+$')


def _host(name, status, checked_in_minutes_ago=None):
    return Host.objects.create(
        hostname=name,
        agent_token=f"t-{name}",
        status=status,
        last_checkin=(timezone.now() - timezone.timedelta(minutes=checked_in_minutes_ago)
                      if checked_in_minutes_ago is not None else None),
    )


class MetricsEndpointTests(TestCase):
    def setUp(self):
        # An account exists, as on any installed Vigil: otherwise
        # SetupRedirectMiddleware sends /metrics to /setup/ before the view runs.
        get_user_model().objects.create_user("someone", password="x")

    def test_no_token_configured_is_404(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("VIGIL_METRICS_TOKEN", None)
            response = self.client.get(URL, AUTH)
        self.assertEqual(response.status_code, 404)

    def test_wrong_or_missing_bearer_is_401(self):
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}):
            missing = self.client.get(URL)
            wrong = self.client.get(URL, HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing["WWW-Authenticate"], "Bearer")
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn(TOKEN, missing.content.decode())
        self.assertNotIn(TOKEN, wrong.content.decode())

    def test_post_is_405(self):
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}):
            response = self.client.post(URL, **AUTH)
        self.assertEqual(response.status_code, 405)

    def test_counts(self):
        _host("now-1", Host.Status.ONLINE, 0)
        _host("stale-1", Host.Status.ONLINE, 30)
        _host("new-1", Host.Status.PENDING)
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=7):
            body = self.client.get(URL, **AUTH).content.decode()
        for line in ('vigil_hosts{status="online"} 2',
                     'vigil_hosts{status="pending"} 1',
                     'vigil_hosts{status="rejected"} 0',
                     'vigil_hosts_checked_in{window="5m"} 1',
                     'vigil_hosts_checked_in{window="1h"} 2'):
            self.assertIn(line, body)

    def test_broker_down(self):
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=None):
            body = self.client.get(URL, **AUTH).content.decode()
        self.assertIn("vigil_celery_broker_up 0", body)
        self.assertFalse([ln for ln in body.splitlines() if ln.startswith("vigil_celery_queue_depth ")])
        self.assertFalse([ln for ln in body.splitlines()
                          if "vigil_celery_queue_depth" in ln and ln.startswith("vigil_")])

    def test_broker_up(self):
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=7):
            body = self.client.get(URL, **AUTH).content.decode()
        self.assertIn("vigil_celery_broker_up 1", body)
        self.assertIn("vigil_celery_queue_depth 7", body)

    def test_format(self):
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=7):
            response = self.client.get(URL, **AUTH)
        self.assertTrue(response["Content-Type"].startswith("text/plain; version=0.0.4"))
        self.assertEqual(response["Cache-Control"], "no-store")
        for line in response.content.decode().splitlines():
            if not line:
                continue
            self.assertTrue(line.startswith("# ") or SAMPLE.match(line), line)

    def test_counts_ignore_sites(self):
        try:
            from apps_business.sites.models import HostSiteAssignment, Site
        except ImportError:
            self.skipTest("Business not installed")
        site = Site.objects.create(name="West", slug="west")
        host = _host("west-1", Host.Status.ONLINE, 0)
        HostSiteAssignment.objects.create(host=host, site=site)
        with mock.patch.dict(os.environ, {"VIGIL_METRICS_TOKEN": TOKEN}), \
                mock.patch("vigil.metrics_endpoint._celery_queue_depth", return_value=None):
            body = self.client.get(URL, **AUTH).content.decode()
        self.assertIn('vigil_hosts{status="online"} 1', body)

    def test_the_view_declares_its_system_scope(self):
        self.assertIn("bearer token", metrics_endpoint.metrics._vigil_scope_reason)
