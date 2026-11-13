"""Policy CRUD: admin-only, and every malformed policy refused with a reason."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import Role, UserProfile

from .models import AppRule, UpdatePolicy


def admin_client(username="pol-admin", role=Role.ADMIN):
    user = get_user_model().objects.create_user(username, password="pw")
    UserProfile.objects.create(user=user, role=role)
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


class PolicyCrudTests(TestCase):
    def setUp(self):
        self.client, self.user = admin_client()

    def _post(self, **body):
        body.setdefault("name", "Workstations")
        return self.client.post("/api/v1/policies/", body, format="json")

    def test_create_with_rules_and_read_back(self):
        resp = self._post(target_tags=["office"], cron_hour="3", window_hours=6,
                          app_rules=[
                              {"app": "Mozilla.Firefox", "source": "winget",
                               "state": "latest"},
                              {"app": "openssl", "state": "pinned",
                               "version": "3.0.13-1"},
                          ])
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["window_hours"], 6)
        self.assertEqual([r["app"] for r in body["app_rules"]],
                         ["Mozilla.Firefox", "openssl"])
        policy = UpdatePolicy.objects.get(pk=body["id"])
        self.assertEqual(policy.created_by, self.user)
        listed = self.client.get("/api/v1/policies/").json()["results"]
        self.assertEqual([p["name"] for p in listed], ["Workstations"])

    def test_put_replaces_rules_as_a_whole(self):
        pid = self._post(app_rules=[{"app": "curl", "state": "present"},
                                    {"app": "vim", "state": "absent"}]).json()["id"]
        resp = self.client.put(f"/api/v1/policies/{pid}/",
                               {"app_rules": [{"app": "git", "state": "latest"}]},
                               format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(list(AppRule.objects.values_list("app", flat=True)), ["git"])

    def test_put_with_a_bad_rule_keeps_the_old_rules(self):
        pid = self._post(app_rules=[{"app": "curl", "state": "present"}]).json()["id"]
        resp = self.client.put(f"/api/v1/policies/{pid}/",
                               {"app_rules": [{"app": "git", "state": "sideways"}]},
                               format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(list(AppRule.objects.values_list("app", flat=True)), ["curl"])

    def test_delete(self):
        pid = self._post().json()["id"]
        self.assertEqual(self.client.delete(f"/api/v1/policies/{pid}/").status_code, 204)
        self.assertFalse(UpdatePolicy.objects.exists())

    def test_refusals(self):
        self._post(name="Taken")
        cases = {
            "blank name": {"name": "  "},
            "duplicate name": {"name": "Taken"},
            "bad cron": {"cron_hour": "3; rm"},
            "zero window": {"window_hours": 0},
            "long window": {"window_hours": 25},
            "unknown classification": {"windows_classifications": ["Hotfixes"]},
            "bad app id": {"app_rules": [{"app": "-oProxy=x", "state": "present"}]},
            "pinned without version": {"app_rules": [{"app": "curl", "state": "pinned"}]},
            "version on latest": {"app_rules": [{"app": "curl", "state": "latest",
                                                 "version": "1.0"}]},
            "pinned snap": {"app_rules": [{"app": "lxd", "source": "snap",
                                           "state": "pinned", "version": "5.0"}]},
            "duplicate rule": {"app_rules": [{"app": "curl", "state": "present"},
                                             {"app": "curl", "state": "absent"}]},
            "unknown site": {"site_id": "9b2f6d3c-1111-4222-8333-944455556666"},
            "malformed site": {"site_id": "not-a-uuid"},
            "unknown source": {"app_rules": [{"app": "curl", "source": "brew2",
                                              "state": "present"}]},
            "bad approval mode": {"approval_mode": "sometimes"},
            "bad reboot": {"reboot": "always"},
        }
        for label, body in cases.items():
            with self.subTest(label):
                payload = {"name": "Fresh", **body}
                resp = self.client.post("/api/v1/policies/", payload, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertIn("detail", resp.json())
        self.assertEqual(UpdatePolicy.objects.count(), 1)

    def test_same_app_from_two_sources_is_allowed(self):
        resp = self._post(app_rules=[
            {"app": "git", "source": "dpkg", "state": "latest"},
            {"app": "git", "source": "winget", "state": "latest"}])
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_viewer_cannot_read_or_write(self):
        viewer, _ = admin_client("pol-viewer", role=Role.VIEWER)
        self.assertEqual(viewer.get("/api/v1/policies/").status_code, 403)
        resp = viewer.post("/api/v1/policies/", {"name": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(UpdatePolicy.objects.exists())


class PolicySchemaShapeTests(TestCase):
    """Every body these endpoints return is the shape its schema documents."""

    def setUp(self):
        self.client, self.user = admin_client("schema-admin")

    def test_responses_match_the_api_schema(self):
        from unittest import mock

        from vigil.schema_check import assert_shape

        from .api_schema import (
            PolicyApproveResultSerializer,
            PolicyChangeListSerializer,
            PolicyComplianceSerializer,
            PolicyDetailErrorSerializer,
            PolicyDriftSerializer,
            PolicyListSerializer,
            PolicyRejectResultSerializer,
            PolicyRunResultSerializer,
            PolicySerializer,
        )
        from .models import PolicyChange
        from .run import run_policy
        from .test_drift import give, make_host
        from .test_run import _TOTP, make_policy

        created = self.client.post("/api/v1/policies/", {
            "name": "Schema", "patch_enabled": True,
            "app_rules": [{"app": "curl", "source": "dpkg", "state": "latest"}],
        }, format="json")
        assert_shape(self, created.json(), PolicySerializer)
        pid = created.json()["id"]
        assert_shape(self, self.client.get("/api/v1/policies/").json(),
                     PolicyListSerializer)
        assert_shape(self, self.client.put(f"/api/v1/policies/{pid}/",
                                           {"name": "Schema"},
                                           format="json").json(), PolicySerializer)
        assert_shape(self, self.client.post("/api/v1/policies/", {"name": ""},
                                            format="json").json(),
                     PolicyDetailErrorSerializer)

        host = make_host("schema-host")
        give(host, "curl", "8.4", "8.5")
        assert_shape(self, self.client.get(f"/api/v1/policies/{pid}/drift/").json(),
                     PolicyDriftSerializer)
        with mock.patch(_TOTP, return_value="bad code"):
            refused = self.client.post(f"/api/v1/policies/{pid}/run/", {"totp": "1"},
                                       format="json")
        assert_shape(self, refused.json(), PolicyDetailErrorSerializer)
        with mock.patch(_TOTP, return_value=None):
            ran = self.client.post(f"/api/v1/policies/{pid}/run/", {"totp": "1"},
                                   format="json")
        assert_shape(self, ran.json(), PolicyRunResultSerializer)

        approval = make_policy(name="Schema-approve", approval_mode="approve")
        run_policy(approval, user=self.user)
        change_id = str(PolicyChange.objects.filter(policy=approval).first().pk)
        assert_shape(self, self.client.get("/api/v1/policies/changes/").json(),
                     PolicyChangeListSerializer)
        assert_shape(self, self.client.post("/api/v1/policies/changes/reject/",
                                            {"ids": [change_id]},
                                            format="json").json(),
                     PolicyRejectResultSerializer)
        run_policy(approval, user=self.user)
        for change in PolicyChange.objects.exclude(policy=approval):
            change.delete()
        change_id = str(PolicyChange.objects.filter(policy=approval,
                                                    state="pending").first().pk)
        with mock.patch(_TOTP, return_value=None):
            approved = self.client.post("/api/v1/policies/changes/approve/",
                                        {"ids": [change_id]}, format="json")
        assert_shape(self, approved.json(), PolicyApproveResultSerializer)
        assert_shape(self, self.client.get("/api/v1/policies/compliance/").json(),
                     PolicyComplianceSerializer)
