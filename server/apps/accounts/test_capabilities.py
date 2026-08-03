"""The capability vocabulary must exist in exactly one place.

The operator matrix UI used to carry its own copy of CAPABILITIES in
JavaScript. It went stale the moment a new app was added server-side — the
new verbs could not be granted through the UI at all, silently. These tests
make that failure mode impossible to reintroduce.
"""
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile
from apps.accounts.permissions import CAPABILITIES

User = get_user_model()

JS = Path(__file__).resolve().parents[2] / "static" / "js" / "vigil-usersites.js"


class CapabilityEndpointTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="cap", password="pw")
        UserProfile.objects.create(user=user, role=Role.ADMIN)
        self.client.force_login(user)

    def test_endpoint_returns_the_whole_vocabulary(self):
        payload = self.client.get("/api/v1/accounts/capabilities/").json()
        self.assertEqual(set(payload), set(CAPABILITIES))
        for app, verbs in payload.items():
            self.assertEqual(set(verbs), set(CAPABILITIES[app]), app)

    def test_view_is_listed_first(self):
        # Stable order: frozensets have none, and the matrix must not
        # reshuffle its checkboxes between requests.
        payload = self.client.get("/api/v1/accounts/capabilities/").json()
        for app, verbs in payload.items():
            if "view" in verbs:
                self.assertEqual(verbs[0], "view", app)

    def test_reprovision_is_grantable(self):
        # The capability this whole fix was prompted by: it existed
        # server-side but could not be granted through the UI.
        payload = self.client.get("/api/v1/accounts/capabilities/").json()
        self.assertIn("reprovision", payload)
        self.assertIn("rebuild", payload["reprovision"])

    def test_endpoint_requires_authentication(self):
        self.client.logout()
        resp = self.client.get("/api/v1/accounts/capabilities/")
        self.assertIn(resp.status_code, (401, 403))


class NoDuplicateVocabularyTests(TestCase):
    """Static guard: the frontend must not reintroduce its own copy."""

    def test_js_does_not_hardcode_the_matrix(self):
        source = JS.read_text()
        literal = re.search(
            r"CAPABILITY_MATRIX\s*=\s*\{[^}]*[a-z_]+\s*:\s*\[", source)
        self.assertIsNone(
            literal,
            "vigil-usersites.js hardcodes CAPABILITY_MATRIX again. Fetch it "
            "from /api/v1/accounts/capabilities/ instead — a second copy goes "
            "stale silently and makes new capabilities ungrantable.")

    def test_js_fetches_the_endpoint(self):
        self.assertIn("/api/v1/accounts/capabilities/", JS.read_text())
