import hashlib
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.reprovision.images import ImportError_, verify_checksum
from apps.reprovision.models import OSImage


class ChecksumTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fake.iso"
        self.path.write_bytes(b"pretend this is an ISO")
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_checksum_passes(self):
        verify_checksum(self.path, self.digest)  # must not raise

    def test_mismatched_checksum_raises(self):
        with self.assertRaises(ImportError_) as ctx:
            verify_checksum(self.path, "0" * 64)
        self.assertIn("checksum", str(ctx.exception).lower())

    def test_checksum_comparison_is_case_insensitive(self):
        verify_checksum(self.path, self.digest.upper())

    def test_empty_expected_checksum_is_refused(self):
        # An image registered without a digest must never import silently.
        with self.assertRaises(ImportError_):
            verify_checksum(self.path, "")

    def test_reads_in_chunks_not_all_at_once(self):
        # A multi-GB ISO must not be loaded into memory in one go.
        big = Path(self.tmp.name) / "big.iso"
        size = 64 * 1024 * 1024
        with big.open("wb") as fh:
            fh.truncate(size)
        digest = hashlib.sha256(b"\0" * size).hexdigest()
        verify_checksum(big, digest)


class PathTraversalTests(TestCase):
    """ISO entry names are attacker-controlled the moment anyone imports an
    image they did not build. A crafted Rock Ridge / Joliet name containing
    '..' must not let extraction write outside the image directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "images"
        self.root.mkdir()
        self.addCleanup(self.tmp.cleanup)

    def test_plain_entry_stays_under_the_root(self):
        from apps.reprovision.images import _safe_join

        out = _safe_join(self.root, "/casper", "vmlinuz")
        self.assertEqual(out, (self.root / "casper" / "vmlinuz").resolve())

    def test_dotdot_in_a_directory_is_refused(self):
        from apps.reprovision.images import _safe_join

        with self.assertRaises(ImportError_):
            _safe_join(self.root, "/../../etc", "passwd")

    def test_dotdot_in_a_filename_is_refused(self):
        from apps.reprovision.images import _safe_join

        with self.assertRaises(ImportError_):
            _safe_join(self.root, "/casper", "../../../../etc/cron.d/pwn")

    def test_absolute_entry_is_reanchored_not_honoured(self):
        from apps.reprovision.images import _safe_join

        out = _safe_join(self.root, "/etc", "shadow")
        self.assertTrue(str(out).startswith(str(self.root.resolve())))

    def test_deep_traversal_is_refused(self):
        from apps.reprovision.images import _safe_join

        with self.assertRaises(ImportError_):
            _safe_join(self.root, "a/b/../../../..", "x")


class ImportFailureTests(TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        override = override_settings(VIGIL_IMAGE_ROOT=self.root.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self.root.cleanup)

    def test_import_failure_records_the_reason_and_marks_failed(self):
        from apps.reprovision.images import import_iso

        image = OSImage.objects.create(
            name="Bad", os_family=OSImage.Family.UBUNTU, version="1",
            architecture="x86_64", sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-an-iso.iso"
            path.write_bytes(b"definitely not an ISO")
            import_iso(image, path)

        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertTrue(image.import_error)

    def test_failed_import_leaves_no_partial_directory_behind(self):
        from apps.reprovision.images import import_iso

        image = OSImage.objects.create(
            name="Bad2", os_family=OSImage.Family.UBUNTU, version="1",
            architecture="x86_64", sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.iso"
            path.write_bytes(b"not an ISO either")
            import_iso(image, path)
        self.assertFalse((Path(self.root.name) / str(image.id)).exists())

    def test_unwritable_image_root_still_does_not_raise(self):
        """image_root() is pure, so a bad path fails inside the handler.

        Regression guard: it used to mkdir eagerly, outside import_iso's try,
        which broke the never-raises contract this Celery task relies on.
        """
        from apps.reprovision.images import import_iso

        image = OSImage.objects.create(
            name="Bad3", os_family=OSImage.Family.UBUNTU, version="1",
            architecture="x86_64", sha256="0" * 64,
        )
        with override_settings(VIGIL_IMAGE_ROOT="/proc/nonexistent/images"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "x.iso"
                path.write_bytes(b"x")
                import_iso(image, path)
        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.FAILED)

    def test_failed_import_does_not_leave_a_ready_image(self):
        # A half-imported image must never be selectable in the ceremony.
        self.assertFalse(
            OSImage.objects.filter(status=OSImage.Status.READY).exists())

    def test_unsupported_family_fails_before_touching_the_iso(self):
        from apps.reprovision.images import KERNEL_PATHS

        self.assertEqual(set(KERNEL_PATHS), {"ubuntu", "debian", "rhel"})

    def test_import_does_not_raise_on_a_missing_file(self):
        # Never raises: a broken import must not take the Celery worker down.
        from apps.reprovision.images import import_iso

        image = OSImage.objects.create(
            name="Gone", os_family=OSImage.Family.DEBIAN, version="12",
            architecture="x86_64", sha256="1" * 64,
        )
        import_iso(image, Path("/nonexistent/never.iso"))
        image.refresh_from_db()
        self.assertEqual(image.status, OSImage.Status.FAILED)


class ResolveKernelPathsTests(TestCase):
    """The layout drifts between derivatives, and a mismatch is only
    discovered after the whole ISO has been downloaded — the most expensive
    moment to learn a filename. Ubuntu ships /casper/initrd; Linux Mint, built
    on the same casper, ships initrd.lz."""

    class _FakeIso:
        """Stands in for pycdlib: knows which paths the ISO contains."""

        def __init__(self, present):
            self.present = set(present)
            self.asked = []

        def get_record(self, iso_path):
            self.asked.append(iso_path)
            if iso_path not in self.present:
                raise OSError(f"not in ISO: {iso_path}")
            return object()

    def test_the_first_matching_candidate_wins(self):
        from .images import _resolve_kernel_paths

        iso = self._FakeIso(["/casper/vmlinuz", "/casper/initrd"])
        self.assertEqual(_resolve_kernel_paths(iso, "ubuntu"),
                         ("/casper/vmlinuz", "/casper/initrd"))

    def test_a_mint_style_compressed_initrd_is_found(self):
        from .images import _resolve_kernel_paths

        iso = self._FakeIso(["/casper/vmlinuz", "/casper/initrd.lz"])
        self.assertEqual(_resolve_kernel_paths(iso, "ubuntu"),
                         ("/casper/vmlinuz", "/casper/initrd.lz"))

    def test_an_iso_with_no_installer_is_refused_with_what_was_tried(self):
        """A live-only image has no installer kernel. The error names the
        candidates so the operator can tell 'wrong variant' from 'Vigil does
        not know this layout'."""
        from .images import ImportError_, _resolve_kernel_paths

        iso = self._FakeIso(["/boot/grub/grub.cfg"])
        with self.assertRaises(ImportError_) as ctx:
            _resolve_kernel_paths(iso, "ubuntu")
        msg = str(ctx.exception)
        self.assertIn("/casper/initrd", msg)
        self.assertIn("/casper/initrd.lz", msg)

    def test_an_unknown_family_is_refused(self):
        from .images import ImportError_, _resolve_kernel_paths

        with self.assertRaises(ImportError_):
            _resolve_kernel_paths(self._FakeIso([]), "haiku")

    def test_a_half_present_candidate_is_not_accepted(self):
        """Kernel present, initrd absent: the pair must match, or extraction
        writes a kernel and then fails."""
        from .images import ImportError_, _resolve_kernel_paths

        iso = self._FakeIso(["/images/pxeboot/vmlinuz"])
        with self.assertRaises(ImportError_):
            _resolve_kernel_paths(iso, "rhel")
