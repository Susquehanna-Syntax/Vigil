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

from apps.accounts.models import Role, UserProfile

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

    def test_a_custom_pull_with_an_overlong_name_is_a_4xx_not_a_500(self):
        """The model caps name at 160 chars. Skipping the serializer would
        let this reach `.objects.create()` uncaught — a DataError on
        Postgres, surfacing as an unhandled 500 instead of this file's usual
        {"error": ...} 4xx. check_url is stubbed to pass so the failure
        under test is the length check, not DNS."""
        with patch("apps.reprovision.views.check_url", lambda *a, **k: ""):
            resp, task = self._pull({
                "url": "https://mirror.example.com/x.iso", "sha256": "a" * 64,
                "os_family": "ubuntu", "name": "x" * 200})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("error", resp.json())
        self.assertFalse(OSImage.objects.exists())
        task.delay.assert_not_called()

    def test_a_custom_pull_with_an_overlong_version_is_a_4xx_not_a_500(self):
        """version is capped at 40 chars — same DataError/500 risk as an
        overlong name."""
        with patch("apps.reprovision.views.check_url", lambda *a, **k: ""):
            resp, task = self._pull({
                "url": "https://mirror.example.com/x.iso", "sha256": "a" * 64,
                "os_family": "ubuntu", "name": "Custom", "version": "v" * 60})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("error", resp.json())
        self.assertFalse(OSImage.objects.exists())
        task.delay.assert_not_called()

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

    def test_catalog_requires_the_view_capability(self):
        """A legacy operator (no per-site capability row) gets neither view
        nor rebuild on reprovision — see test_rbac.py's
        test_legacy_operators_get_no_rebuild_rights. The catalog read is
        gated the same way as the image library itself, so it must 404 for
        the same caller, not leak that the route exists."""
        user = get_user_model().objects.create_user("op2", password="x")
        UserProfile.objects.create(user=user, role=Role.OPERATOR)
        self.client.force_login(user)
        resp = self.client.get("/api/v1/reprovision/catalog/")
        self.assertEqual(resp.status_code, 404)


class DuplicatePullTests(TestCase):
    """One image per set of bytes.

    Every pull used to create a fresh row. Because import was broken, the
    natural response — pull it again — left the library holding several
    identical "Ubuntu Server 24.04 LTS" entries, all failed, with no way to
    tell them apart. The digest identifies the bytes exactly, which is the
    whole reason it is mandatory on this endpoint.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)
        self.entry = CATALOG[0]

    def _pull(self, payload=None):
        with patch("apps.reprovision.views.fetch_image") as task:
            resp = self.client.post(
                "/api/v1/reprovision/images/pull/",
                payload or {"catalog_id": self.entry["id"]},
                content_type="application/json")
            return resp, task

    def test_pulling_the_same_catalog_entry_twice_makes_one_row(self):
        self._pull()
        resp, _ = self._pull()
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(
            OSImage.objects.filter(sha256=self.entry["sha256"]).count(), 1)

    def test_the_conflict_says_which_image_already_has_those_bytes(self):
        self._pull()
        resp, _ = self._pull()
        body = resp.json()
        self.assertIn(self.entry["name"], body["error"])
        self.assertEqual(
            body["image_id"],
            str(OSImage.objects.get(sha256=self.entry["sha256"]).id))

    def test_a_second_pull_does_not_queue_a_second_download(self):
        """The point of refusing: not re-fetching gigabytes already held."""
        self._pull()
        _, task = self._pull()
        task.delay.assert_not_called()

    def test_retrying_a_failed_pull_reuses_the_row_and_refetches(self):
        self._pull()
        image = OSImage.objects.get(sha256=self.entry["sha256"])
        image.status = OSImage.Status.FAILED
        image.import_error = "No installer kernel/initrd found"
        image.bytes_downloaded = 1234
        image.save()

        resp, task = self._pull()
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(
            OSImage.objects.filter(sha256=self.entry["sha256"]).count(), 1)
        task.delay.assert_called_once()

        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.DOWNLOADING)
        self.assertEqual(image.import_error, "")
        self.assertEqual(image.bytes_downloaded, 0)

    def test_a_ready_image_is_refused_rather_than_refetched(self):
        self._pull()
        image = OSImage.objects.get(sha256=self.entry["sha256"])
        image.status = OSImage.Status.READY
        image.save()
        resp, task = self._pull()
        self.assertEqual(resp.status_code, 409)
        task.delay.assert_not_called()

    def test_a_different_image_is_unaffected(self):
        self._pull()
        other = CATALOG[1]
        resp, task = self._pull({"catalog_id": other["id"]})
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(OSImage.objects.count(), 2)
        task.delay.assert_called_once()

    def test_a_custom_url_matching_an_existing_digest_is_refused(self):
        """Same bytes reached by a different route is still the same image.

        check_url is stubbed because it resolves DNS, which it does *before*
        the digest check on purpose — a destination Vigil will not fetch from
        is refused before any row is touched.
        """
        self._pull()
        with patch("apps.reprovision.views.check_url", return_value=""):
            resp, _ = self._pull({
                "url": "https://mirror.example.com/other-name.iso",
                "sha256": self.entry["sha256"], "os_family": "ubuntu",
                "name": "A different name for the same bytes",
            })
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(OSImage.objects.count(), 1)
