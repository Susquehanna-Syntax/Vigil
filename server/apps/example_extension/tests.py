"""Seam tests — exercise the extension contract without installing the app.

These cover the mechanics every Pro/Enterprise app relies on: the hooks event
bus, the editions feature registry, the registration wiring, and an edition
view reading edition state. They run as part of the core suite so the contract
can't regress.
"""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from vigil import editions, hooks


class HooksTests(TestCase):
    def setUp(self):
        hooks.clear()

    def tearDown(self):
        hooks.clear()

    def test_subscribe_and_emit_delivers_payload(self):
        received = []
        hooks.subscribe("host_approved", lambda host, **_: received.append(host))
        hooks.emit("host_approved", host="cphm", approved_by="admin")
        self.assertEqual(received, ["cphm"])

    def test_handler_is_isolated_on_failure(self):
        calls = []

        def boom(**_):
            raise RuntimeError("handler exploded")

        hooks.subscribe("host_approved", boom)
        hooks.subscribe("host_approved", lambda **_: calls.append(1))
        # Must not raise; the second handler still runs.
        hooks.emit("host_approved", host="x")
        self.assertEqual(calls, [1])

    def test_subscribe_is_idempotent(self):
        handler = lambda **_: None
        hooks.subscribe("host_approved", handler)
        hooks.subscribe("host_approved", handler)
        self.assertEqual(len(hooks.subscribers("host_approved")), 1)

    def test_emit_with_no_subscribers_is_noop(self):
        hooks.emit("host_approved", host="x")  # Community case — nobody listening.


class EditionsTests(TestCase):
    def setUp(self):
        editions.clear()

    def tearDown(self):
        editions.clear()

    def test_community_by_default(self):
        self.assertEqual(editions.active_edition(), editions.COMMUNITY)
        self.assertFalse(editions.feature_enabled("rbac"))

    def test_pro_feature_sets_pro_edition(self):
        editions.register_feature("rbac")
        self.assertTrue(editions.feature_enabled("rbac"))
        self.assertEqual(editions.active_edition(), editions.PRO)

    def test_enterprise_feature_wins(self):
        editions.register_feature("rbac")        # pro
        editions.register_feature("audit_log")   # enterprise
        self.assertEqual(editions.active_edition(), editions.ENTERPRISE)


class RegistrationTests(TestCase):
    def setUp(self):
        hooks.clear()
        editions.clear()
        from apps.example_extension import registration
        registration.approvals_seen.clear()

    def tearDown(self):
        hooks.clear()
        editions.clear()

    def test_register_extension_wires_feature_and_hook(self):
        from apps.example_extension import registration

        registration.register_extension()
        self.assertTrue(editions.feature_enabled("ai_suggestions"))

        host = SimpleNamespace(hostname="CPHM-MAINFRAME")
        hooks.emit("host_approved", host=host, approved_by=None)
        self.assertEqual(registration.approvals_seen, ["CPHM-MAINFRAME"])


class ExtensionViewTests(TestCase):
    def setUp(self):
        editions.clear()

    def tearDown(self):
        editions.clear()

    def test_ping_reports_edition_state(self):
        from apps.example_extension.views import ping

        editions.register_feature("ai_suggestions")
        user = get_user_model().objects.create_user("seamtester", password="x")
        request = APIRequestFactory().get("/ext/example_extension/ping/")
        force_authenticate(request, user=user)

        response = ping(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["ok"])
        self.assertTrue(response.data["ai_suggestions_enabled"])
        self.assertEqual(response.data["edition"], editions.PRO)
