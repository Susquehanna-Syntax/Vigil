"""QA-19: POST /api/v1/stacks/validate/ — the stack editor's live validator."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile

GOOD = """services:
  web:
    image: nginx
    ports:
      - "80:80"
    environment:
      - DB_PASSWORD=${DB_PASSWORD}
      - TZ=${TZ:-UTC}
"""


class ValidateEndpointTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("a", password="x")
        UserProfile.objects.create(user=self.user, role=Role.ADMIN)
        self.client.force_login(self.user)

    def _post(self, compose_yaml, env_keys=None):
        body = {"compose_yaml": compose_yaml}
        if env_keys is not None:
            body["env_keys"] = env_keys
        return self.client.post("/api/v1/stacks/validate/", body,
                                content_type="application/json")

    def test_a_good_file_lists_services_and_env(self):
        resp = self._post(GOOD, [])
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["error"], "")
        self.assertIsNone(body["line"])
        self.assertEqual(
            body["services"],
            [{"name": "web", "image": "nginx", "ports": ["80:80"], "build": False}])
        self.assertEqual(body["env_refs"], ["DB_PASSWORD", "TZ"])
        self.assertEqual(body["missing_env"], ["DB_PASSWORD"])

    def test_env_keys_satisfy_refs(self):
        body = self._post(GOOD, ["DB_PASSWORD"]).json()
        self.assertEqual(body["missing_env"], [])
        self.assertEqual(body["env_refs"], ["DB_PASSWORD", "TZ"])

    def test_bad_yaml_reports_a_line(self):
        resp = self._post("services:\n  web:\n    image: [\n")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("not valid YAML", body["error"])
        self.assertIsInstance(body["line"], int)
        self.assertGreaterEqual(body["line"], 2)
        self.assertEqual(body["services"], [])

    def test_sandbox_refusals_come_through(self):
        body = self._post("services:\n  web:\n    image: nginx\n    privileged: true\n")
        self.assertEqual(body.status_code, 200)
        body = body.json()
        self.assertFalse(body["ok"])
        self.assertIn("privileged", body["error"])
        self.assertEqual(body["services"], [])

    def test_admin_only(self):
        viewer = get_user_model().objects.create_user("v", password="x")
        UserProfile.objects.create(user=viewer, role=Role.VIEWER)
        self.client.force_login(viewer)
        self.assertEqual(self._post(GOOD).status_code, 403)

    def test_double_dollar_is_not_a_ref(self):
        body = self._post("services:\n  web:\n    image: nginx\n"
                          "    command: echo $$HOME\n").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["env_refs"], [])
        self.assertEqual(body["missing_env"], [])
