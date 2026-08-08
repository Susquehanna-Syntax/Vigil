"""Pulling an image is admin-only and refuses destinations Vigil will not
fetch from. The refusal comes back as a message the operator can act on, not
a bare 400."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils.timezone import now
from datetime import timedelta

from .catalog import CATALOG
from .models import InstallProfile, OSImage, RebuildJob


class ImageApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def _pull(self, payload):
        with patch("apps.reprovision.views.fetch_image") as task:
            resp = self.client.post("/api/v1/reprovision/images/pull/",
                                    payload, content_type="application/json")
            return resp, task

    def test_catalog_is_served(self):
        resp = self.client.get("/api/v1/reprovision/catalog/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), len(CATALOG))

    def test_pulling_a_catalog_entry_creates_an_image_and_queues_the_task(self):
        resp, task = self._pull({"catalog_id": CATALOG[0]["id"]})
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertTrue(OSImage.objects.filter(
            source_url=CATALOG[0]["url"]).exists())
        task.delay.assert_called_once()

    def test_a_catalog_pull_carries_the_published_digest(self):
        """The operator never types it, so it cannot be typo'd."""
        self._pull({"catalog_id": CATALOG[0]["id"]})
        image = OSImage.objects.get(source_url=CATALOG[0]["url"])
        self.assertEqual(image.sha256, CATALOG[0]["sha256"])

    def test_a_catalog_pull_ignores_a_conflicting_caller_supplied_digest(self):
        """catalog_id plus a caller-supplied sha256 uses the catalog's
        digest — the whole point of the catalog is that the operator never
        types a 64-char hash, so a request smuggling one in cannot win."""
        resp, task = self._pull({"catalog_id": CATALOG[0]["id"],
                                 "sha256": "b" * 64})
        self.assertEqual(resp.status_code, 202, resp.content)
        image = OSImage.objects.get(source_url=CATALOG[0]["url"])
        self.assertEqual(image.sha256, CATALOG[0]["sha256"])
        self.assertNotEqual(image.sha256, "b" * 64)
        task.delay.assert_called_once()

    def test_an_unknown_catalog_id_is_400(self):
        resp, _ = self._pull({"catalog_id": "no-such-image"})
        self.assertEqual(resp.status_code, 400)

    def test_a_custom_url_requires_a_digest(self):
        resp, _ = self._pull({"url": "https://mirror.example.com/x.iso",
                              "os_family": "ubuntu", "name": "Custom"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sha256", resp.json()["error"].lower())

    def test_a_bad_os_family_is_rejected(self):
        resp, task = self._pull({"url": "https://mirror.example.com/x.iso",
                                 "sha256": "a" * 64, "os_family": "windows",
                                 "name": "Custom"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(OSImage.objects.exists())
        task.delay.assert_not_called()

    def test_a_blocked_url_is_refused_with_the_reason(self):
        with patch("apps.reprovision.views.check_url",
                   lambda *a, **k: "Refusing to fetch from metadata"):
            resp, task = self._pull({
                "url": "https://169.254.169.254/x.iso", "sha256": "a" * 64,
                "os_family": "ubuntu", "name": "Evil"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("metadata", resp.json()["error"])
        task.delay.assert_not_called()

    def test_a_blocked_url_creates_no_image_row(self):
        with patch("apps.reprovision.views.check_url",
                   lambda *a, **k: "Refusing to fetch from metadata"):
            self._pull({"url": "https://169.254.169.254/x.iso",
                        "sha256": "a" * 64, "os_family": "ubuntu",
                        "name": "Evil"})
        self.assertFalse(OSImage.objects.exists())

    def test_a_non_admin_cannot_pull(self):
        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        resp, _ = self._pull({"catalog_id": CATALOG[0]["id"]})
        self.assertIn(resp.status_code, (401, 403))

    def test_deleting_an_image_removes_it(self):
        image = OSImage.objects.create(name="X", os_family="ubuntu",
                                       version="1", sha256="a" * 64,
                                       status=OSImage.Status.READY)
        resp = self.client.delete(f"/api/v1/reprovision/images/{image.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(OSImage.objects.filter(pk=image.id).exists())

    def test_deleting_an_image_referenced_by_a_job_is_a_conflict(self):
        from apps.hosts.models import Host

        image = OSImage.objects.create(name="X", os_family="ubuntu",
                                       version="1", sha256="a" * 64,
                                       status=OSImage.Status.READY)
        profile = InstallProfile.objects.create(
            name="p", image=image, disk_target="/dev/sda",
            admin_username="v")
        host = Host.objects.create(hostname="held-01", agent_token="tok-held")
        job = RebuildJob.objects.create(
            host=host, image=image, profile=profile,
            deadline=now() + timedelta(minutes=60))

        resp = self.client.delete(f"/api/v1/reprovision/images/{image.id}/")

        self.assertEqual(resp.status_code, 409)
        self.assertIn(str(job.id), resp.json()["error"])
        self.assertTrue(OSImage.objects.filter(pk=image.id).exists())

    def test_deleting_an_image_removes_the_extracted_tree_from_disk(self):
        with tempfile.TemporaryDirectory() as root:
            image = OSImage.objects.create(name="X", os_family="ubuntu",
                                           version="1", sha256="a" * 64,
                                           status=OSImage.Status.READY)
            tree_dir = Path(root) / str(image.id)
            (tree_dir / "tree").mkdir(parents=True)
            (tree_dir / "vmlinuz").write_bytes(b"x")
            self.assertTrue(tree_dir.exists())

            with override_settings(VIGIL_IMAGE_ROOT=root):
                resp = self.client.delete(
                    f"/api/v1/reprovision/images/{image.id}/")

            self.assertEqual(resp.status_code, 204)
            self.assertFalse(tree_dir.exists())
