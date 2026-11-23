"""PATCH /api/v1/hosts/<id>/depends-on/: what an operator may say a host sits
behind. The view is the only place the loop and same-site rules can be checked,
and it must not confirm that a host in another site exists."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.hosts.models import Host


def _host(hostname, **kwargs):
    return Host.objects.create(
        hostname=hostname, agent_token=f"tok-{hostname}" + "a" * 28,
        os="linux", status=Host.Status.ONLINE, mode=Host.Mode.MONITOR,
        **kwargs)


class DependsOnEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        self.admin = User.objects.create_user("depadmin", password="x",
                                              is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.child = _host("dep-child")
        self.parent = _host("dep-parent")

    def _patch(self, host, depends_on):
        return self.client.patch(f"/api/v1/hosts/{host.id}/depends-on/",
                                 {"depends_on": depends_on},
                                 content_type="application/json")

    def _error(self, response):
        return response.json()["error"]

    def test_setting_a_parent_is_stored_and_returned(self):
        r = self._patch(self.child, str(self.parent.id))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["depends_on"], str(self.parent.id))
        self.child.refresh_from_db()
        self.assertEqual(self.child.depends_on_id, self.parent.id)

    def test_null_clears_it(self):
        self._patch(self.child, str(self.parent.id))
        r = self._patch(self.child, None)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["depends_on"])
        self.child.refresh_from_db()
        self.assertIsNone(self.child.depends_on_id)

    def test_a_host_cannot_depend_on_itself(self):
        r = self._patch(self.child, str(self.child.id))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._error(r), "A host cannot depend on itself")
        self.child.refresh_from_db()
        self.assertIsNone(self.child.depends_on_id)

    def test_a_malformed_id_is_refused(self):
        r = self._patch(self.child, "not-a-uuid")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._error(r), "depends_on must be a host id or null")

    def test_an_unknown_id_is_no_such_host(self):
        r = self._patch(self.child, "5f7c9c2e-6b1e-4a1a-8b52-c9d3d0a1b2c4")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._error(r), "No such host")

    def test_a_two_host_loop_is_refused_and_leaves_the_child_unchanged(self):
        self._patch(self.child, str(self.parent.id))
        r = self._patch(self.parent, str(self.child.id))
        self.assertEqual(r.status_code, 400)
        self.assertIn("already depends on", self._error(r))
        self.parent.refresh_from_db()
        self.assertIsNone(self.parent.depends_on_id)

    def test_a_three_host_loop_is_refused(self):
        third = _host("dep-third")
        self._patch(self.child, str(self.parent.id))
        self._patch(self.parent, str(third.id))
        r = self._patch(third, str(self.child.id))
        self.assertEqual(r.status_code, 400)
        self.assertIn("loop", self._error(r))
        third.refresh_from_db()
        self.assertIsNone(third.depends_on_id)

    def test_a_plain_user_is_refused_and_nothing_changes(self):
        viewer = get_user_model().objects.create_user("depviewer", password="x")
        self.client.force_login(viewer)
        r = self._patch(self.child, str(self.parent.id))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._error(r), "Not permitted")
        self.child.refresh_from_db()
        self.assertIsNone(self.child.depends_on_id)

    def test_deleting_the_parent_clears_the_child(self):
        self._patch(self.child, str(self.parent.id))
        self.parent.delete()
        self.child.refresh_from_db()
        self.assertIsNone(self.child.depends_on_id)
