"""Uploading an ISO must never read the whole file into memory and must
never leave a half-written file (or a half-imported tree) behind on any
non-success exit — the same contract test_fetcher.py already covers for
the download path. This file mirrors its structure for the upload path.
"""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import (InMemoryUploadedFile,
                                            SimpleUploadedFile)
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .models import OSImage

PAYLOAD = b"not really an iso, but it hashes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class ImageUploadTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.admin = get_user_model().objects.create_user(
            "root", password="x", is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def _file(self, payload=PAYLOAD):
        return SimpleUploadedFile("image.iso", payload,
                                  content_type="application/octet-stream")

    def _upload(self, payload=None, **fields):
        data = {
            "name": "Custom", "os_family": "ubuntu", "version": "1",
            "sha256": DIGEST,
        }
        data.update(fields)
        if payload is not False:
            data["iso"] = self._file(payload if payload is not None else PAYLOAD)
        with override_settings(VIGIL_IMAGE_ROOT=self.tmp.name):
            return self.client.post("/api/v1/reprovision/images/upload/",
                                    data, format="multipart")

    def _leftovers(self):
        return [p.name for p in Path(self.tmp.name).iterdir()]

    @staticmethod
    def _fake_successful_import(image, path):
        """Stands in for the real `import_iso`, which needs an actual
        ISO9660 filesystem — PAYLOAD is not one. Mirrors test_fetcher.py's
        reasoning: a no-op mock is indistinguishable from "hasn't run yet",
        so this drives the stand-in to behave like the real thing, which
        never raises and lands the row itself."""
        image.status = OSImage.Status.READY
        image.save(update_fields=["status"])

    def test_a_good_upload_reaches_ready(self):
        with patch("apps.reprovision.fetcher.import_iso",
                   self._fake_successful_import):
            resp = self._upload()
        self.assertEqual(resp.status_code, 201, resp.content)
        image = OSImage.objects.get()
        self.assertEqual(image.status, OSImage.Status.READY)

    def test_a_good_upload_leaves_no_temp_file(self):
        with patch("apps.reprovision.fetcher.import_iso",
                   self._fake_successful_import):
            self._upload()
        self.assertEqual(self._leftovers(), [])

    def test_a_checksum_mismatch_fails_the_image_and_leaves_no_file(self):
        resp = self._upload(sha256="0" * 64)
        self.assertEqual(resp.status_code, 400)
        image = OSImage.objects.get()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("checksum", image.import_error.lower())
        self.assertEqual(self._leftovers(), [])

    def test_an_absent_sha256_is_refused_before_any_row_is_created(self):
        resp = self._upload(sha256="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("sha256", resp.json()["error"].lower())
        self.assertFalse(OSImage.objects.exists())
        self.assertEqual(self._leftovers(), [])

    def test_a_bad_os_family_is_refused(self):
        resp = self._upload(os_family="windows")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(OSImage.objects.exists())
        self.assertEqual(self._leftovers(), [])

    def test_a_missing_file_is_refused(self):
        resp = self._upload(payload=False)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(OSImage.objects.exists())

    def test_a_non_admin_cannot_upload(self):
        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        resp = self._upload()
        self.assertIn(resp.status_code, (401, 403))
        self.assertFalse(OSImage.objects.exists())

    def test_a_failed_import_fails_the_image_and_cleans_up(self):
        def boom(image, path):
            raise RuntimeError("bad iso structure")

        with patch("apps.reprovision.fetcher.import_iso", boom):
            resp = self._upload()
        self.assertEqual(resp.status_code, 400)
        image = OSImage.objects.get()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertEqual(self._leftovers(), [])

    def test_a_failed_import_leaves_no_partial_tree(self):
        """import_iso itself is responsible for cleaning up its own
        half-extracted tree on failure (see images.py); this proves the
        upload path doesn't leave anything of its own behind on top of
        that."""
        def quietly_fails(image, path):
            image.status = OSImage.Status.FAILED
            image.import_error = "corrupt ISO9660 tree"
            image.save(update_fields=["status", "import_error"])

        with patch("apps.reprovision.fetcher.import_iso", quietly_fails):
            resp = self._upload()
        self.assertEqual(resp.status_code, 400)
        image = OSImage.objects.get()
        self.assertEqual(image.import_error, "corrupt ISO9660 tree")
        tree_dir = Path(self.tmp.name) / str(image.id)
        self.assertFalse(tree_dir.exists())
        self.assertEqual(self._leftovers(), [])

    def test_a_write_error_partway_through_cleans_up(self):
        """Simulates a disk error while streaming the upload to its temp
        file — the one exit fetcher.py's helpers don't cover, since it
        happens before there is a digest to compare."""
        def boom_chunks(*a, **k):
            raise OSError("No space left on device")

        with patch.object(InMemoryUploadedFile, "chunks", boom_chunks):
            resp = self._upload()
        self.assertEqual(resp.status_code, 400)
        image = OSImage.objects.get()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertEqual(self._leftovers(), [])
