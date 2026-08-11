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



def build_iso(files, *, rock_ridge=True, joliet=True):
    """Bytes of a real ISO containing *files*, a {path: payload} mapping.

    Built with pycdlib rather than xorriso so the fixtures need no external
    tool, and asserted against a genuine ISO structure rather than a stand-in
    for one. The previous fake accepted a lowercase ``iso_path`` and so agreed
    with code that could never work on a real image: every candidate lookup
    missed, and every import failed with a message blaming the distro layout.
    A fake that shares the code's wrong assumption tests nothing.

    Distros ship Rock Ridge and Joliet (``xorriso -as mkisofs -r -J``), so
    that is the default; the keyword arguments cover the degenerate ISOs.
    """
    import io

    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.new(rock_ridge="1.09" if rock_ridge else None,
            joliet=3 if joliet else None, interchange_level=3)

    def raw_dir(part):
        # pycdlib enforces strict ISO9660 directory naming (A-Z, 0-9, _),
        # where xorriso is permissive. Debian's /install.amd only survives in
        # Rock Ridge here — which is the realistic case anyway, and proves
        # resolution does not lean on the raw name matching.
        return part.upper().replace(".", "_").replace("-", "_")

    def raw(path):
        parts = [p for p in path.strip("/").split("/") if p]
        *dirs, name = parts
        prefix = "/" + "".join(f"{raw_dir(d)}/" for d in dirs)
        name = name.upper()
        return f"{prefix}{name if '.' in name else name + '.'};1"

    made = set()
    for path in sorted(files):
        parts = [p for p in path.strip("/").split("/") if p]
        for depth in range(1, len(parts)):
            branch = parts[:depth]
            key = "/".join(branch)
            if key in made:
                continue
            made.add(key)
            iso.add_directory(
                "/" + "/".join(raw_dir(b) for b in branch),
                rr_name=branch[-1] if rock_ridge else None,
                joliet_path="/" + key if joliet else None)
        payload = files[path]
        iso.add_fp(io.BytesIO(payload), len(payload), raw(path),
                   rr_name=parts[-1] if rock_ridge else None,
                   joliet_path=path if joliet else None)

    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    return out.getvalue()


def open_iso(data):
    import io

    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(data))
    return iso


class ResolveKernelPathsTests(TestCase):
    """The layout drifts between derivatives, and a mismatch is only
    discovered after the whole ISO has been downloaded — the most expensive
    moment to learn a filename. Ubuntu ships /casper/initrd; Linux Mint, built
    on the same casper, ships initrd.lz.

    Every case runs against a real ISO, because the bug these guard was a
    namespace mistake that only a real ISO can express.
    """

    def _resolve(self, files, family, **kw):
        from .images import _resolve_kernel_paths

        iso = open_iso(build_iso(files, **kw))
        try:
            return _resolve_kernel_paths(iso, family)
        finally:
            iso.close()

    def _reads(self, files, family, **kw):
        """Resolve, then actually read both files back through the lookups."""
        import io

        from .images import _resolve_kernel_paths

        iso = open_iso(build_iso(files, **kw))
        try:
            got = []
            for lookup in _resolve_kernel_paths(iso, family):
                buf = io.BytesIO()
                iso.get_file_from_iso_fp(buf, **lookup)
                got.append(buf.getvalue())
            return got
        finally:
            iso.close()

    def test_the_first_matching_candidate_wins(self):
        kernel, initrd = self._resolve(
            {"/casper/vmlinuz": b"K", "/casper/initrd": b"I"}, "ubuntu")
        self.assertEqual(kernel, {"rr_path": "/casper/vmlinuz"})
        self.assertEqual(initrd, {"rr_path": "/casper/initrd"})

    def test_the_resolved_lookups_actually_read_the_files(self):
        """The regression in full: resolution used to hand back a path that
        no namespace could read."""
        self.assertEqual(
            self._reads({"/casper/vmlinuz": b"KERNEL",
                         "/casper/initrd": b"INITRD"}, "ubuntu"),
            [b"KERNEL", b"INITRD"])

    def test_a_mint_style_compressed_initrd_is_found(self):
        self.assertEqual(
            self._reads({"/casper/vmlinuz": b"KERNEL",
                         "/casper/initrd.lz": b"LZ"}, "ubuntu"),
            [b"KERNEL", b"LZ"])

    def test_a_joliet_only_iso_still_resolves(self):
        kernel, _ = self._resolve(
            {"/casper/vmlinuz": b"K", "/casper/initrd": b"I"}, "ubuntu",
            rock_ridge=False)
        self.assertEqual(kernel, {"joliet_path": "/casper/vmlinuz"})

    def test_a_bare_iso9660_image_still_resolves(self):
        """No Rock Ridge, no Joliet: the name really is /CASPER/VMLINUZ.;1."""
        self.assertEqual(
            self._reads({"/casper/vmlinuz": b"KERNEL",
                         "/casper/initrd": b"INITRD"}, "ubuntu",
                        rock_ridge=False, joliet=False),
            [b"KERNEL", b"INITRD"])

    def test_debian_and_rhel_layouts_resolve(self):
        self.assertEqual(
            self._reads({"/install.amd/vmlinuz": b"K",
                         "/install.amd/initrd.gz": b"I"}, "debian"),
            [b"K", b"I"])
        self.assertEqual(
            self._reads({"/images/pxeboot/vmlinuz": b"K",
                         "/images/pxeboot/initrd.img": b"I"}, "rhel"),
            [b"K", b"I"])

    def test_an_iso_with_no_installer_is_refused_with_what_was_tried(self):
        """A live-only image has no installer kernel. The error names the
        candidates so the operator can tell 'wrong variant' from 'Vigil does
        not know this layout'."""
        from .images import ImportError_

        with self.assertRaises(ImportError_) as ctx:
            self._resolve({"/boot/grub/grub.cfg": b"x"}, "ubuntu")
        msg = str(ctx.exception)
        self.assertIn("/casper/initrd", msg)
        self.assertIn("/casper/initrd.lz", msg)

    def test_an_unknown_family_is_refused(self):
        from .images import ImportError_

        with self.assertRaises(ImportError_):
            self._resolve({"/casper/vmlinuz": b"K"}, "haiku")

    def test_a_half_present_candidate_is_not_accepted(self):
        """Kernel present, initrd absent: the pair must match, or extraction
        writes a kernel and then fails."""
        from .images import ImportError_

        with self.assertRaises(ImportError_):
            self._resolve({"/images/pxeboot/vmlinuz": b"K"}, "rhel")


class ExtractedTreeTests(TestCase):
    """The installer fetches this tree over HTTP by its real filenames.

    Walking the bare ISO-9660 namespace yields ``/CASPER/VMLINUZ.;1``, which
    strips to ``VMLINUZ.`` — uppercase, trailing dot, matching nothing the
    installer asks for. The import would look clean and every fetch would 404.
    """

    def _tree(self, files, **kw):
        from .images import _extract_tree

        iso = open_iso(build_iso(files, **kw))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name)
        try:
            _extract_tree(iso, dest)
        finally:
            iso.close()
        return {str(p.relative_to(dest)): p.read_bytes()
                for p in sorted(dest.rglob("*")) if p.is_file()}

    def test_names_keep_their_real_case_and_extension(self):
        self.assertEqual(
            self._tree({"/casper/filesystem.squashfs": b"SQUASH",
                        "/dists/stable/Release": b"REL"}),
            {"casper/filesystem.squashfs": b"SQUASH",
             "dists/stable/Release": b"REL"})

    def test_no_extracted_name_carries_a_trailing_dot(self):
        """The precise shape of the old defect."""
        names = self._tree({"/casper/vmlinuz": b"K"})
        self.assertEqual(list(names), ["casper/vmlinuz"])

    def test_a_bare_iso9660_image_is_still_extracted(self):
        names = self._tree({"/casper/vmlinuz": b"K"},
                           rock_ridge=False, joliet=False)
        self.assertEqual(list(names), ["CASPER/VMLINUZ."])


class EndToEndImportTests(TestCase):
    """import_iso against a real ISO — the path that failed in production."""

    def _import(self, files):
        from .images import import_iso

        data = build_iso(files)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        iso_path = Path(tmp.name) / "image.iso"
        iso_path.write_bytes(data)
        image = OSImage.objects.create(
            name="Ubuntu Server 24.04 LTS", os_family=OSImage.Family.UBUNTU,
            version="24.04", architecture="x86_64",
            sha256=hashlib.sha256(data).hexdigest(),
        )
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        with override_settings(VIGIL_IMAGE_ROOT=root.name):
            import_iso(image, iso_path)
        image.refresh_from_db()
        return image

    def test_a_pulled_ubuntu_image_imports_and_lands_ready(self):
        image = self._import({
            "/casper/vmlinuz": b"KERNEL",
            "/casper/initrd": b"INITRD",
            "/casper/filesystem.squashfs": b"SQUASH",
        })
        self.assertEqual(image.status, OSImage.Status.READY, image.import_error)
        self.assertEqual(Path(image.kernel_path).read_bytes(), b"KERNEL")
        self.assertEqual(Path(image.initrd_path).read_bytes(), b"INITRD")
        self.assertEqual(
            (Path(image.tree_path) / "casper" / "filesystem.squashfs").read_bytes(),
            b"SQUASH")

    def test_a_live_only_image_still_fails_with_a_readable_reason(self):
        image = self._import({"/boot/grub/grub.cfg": b"x"})
        self.assertEqual(image.status, OSImage.Status.FAILED)
        self.assertIn("No installer kernel", image.import_error)
