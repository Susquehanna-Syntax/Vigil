"""hunt_file version bounds (M5 phase 02b).

A file's version comes from, in order, a JAR manifest, a Windows version
resource, or the first dotted-numeric token in the file name. Files whose
version cannot be determined never match a version bound.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own
import ctypes
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import hunt


def _make_jar(path: Path, manifest: str | None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/whatever", "payload")
        if manifest is not None:
            archive.writestr("META-INF/MANIFEST.MF", manifest)
    return path


class FileVersionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_version_from_jar_manifest(self):
        manifest = (
            "Manifest-Version: 1.0\n"
            "Implementation-Title: log4j-core\n"
            "Implementation-Version: 2.14.1\n"
        )
        jar = _make_jar(self.root / "log4j-core.jar", manifest)
        self.assertEqual(hunt._file_version(str(jar)), "2.14.1")

        # Bundle-Version is read when Implementation-Version is absent.
        jar2 = _make_jar(self.root / "osgi.jar",
                         "Manifest-Version: 1.0\nBundle-Version: 1.3.0\n")
        self.assertEqual(hunt._file_version(str(jar2)), "1.3.0")

        # The manifest wins over the version in the file name.
        jar3 = _make_jar(self.root / "app-9.9.9.jar",
                         "Manifest-Version: 1.0\nImplementation-Version: 1.2.3\n")
        self.assertEqual(hunt._file_version(str(jar3)), "1.2.3")

    def test_version_from_filename(self):
        self.assertEqual(hunt._file_version("/opt/lib/log4j-core-2.14.1.jar"),
                         "2.14.1")
        self.assertEqual(hunt._file_version("/usr/share/doc/notes.txt"),
                         None)
        # A non-archive name keeps the filename token; a corrupt jar falls
        # back to the filename rather than raising.
        (self.root / "log4j-core-2.14.1.jar").write_bytes(b"not a zip")
        self.assertEqual(hunt._file_version(str(self.root / "log4j-core-2.14.1.jar")),
                         "2.14.1")

    def test_windows_pe_version(self):
        # 3.14.5.6 as packed by Windows: MS = (3<<16)|14, LS = (5<<16)|6.
        class _Fixed:
            # dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS
            contents = (ctypes.c_uint32 * 4)(0xFEEF04BD, 0x00010000,
                                             0x0003000E, 0x00050006)

        def _fake_verquery(_buf, _root, value_ref, length_ref):
            length_ref.value = 16
            return True
        windll = mock.Mock()
        windll.version.GetFileVersionInfoSizeW.return_value = 512
        windll.version.GetFileVersionInfoW.return_value = True
        windll.version.VerQueryValueW.side_effect = _fake_verquery

        fixed_ptr = ctypes.c_void_p(512)

        def _fake_cast(pointer, _ctype):
            return _Fixed()

        with mock.patch("ctypes.windll", windll, create=True), \
             mock.patch("ctypes.byref", side_effect=lambda obj: obj), \
             mock.patch("ctypes.c_void_p", return_value=fixed_ptr), \
             mock.patch("ctypes.cast", side_effect=_fake_cast), \
             mock.patch("ctypes.create_string_buffer",
                        side_effect=lambda *a, **k: b"\\"):
            self.assertEqual(hunt._windows_version(str(self.root / "app.exe")),
                             "3.14.5.6")

        path = str(self.root / "app.exe")
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(hunt, "_windows_version") as fake:
            fake.return_value = "2.0.20.3"
            self.assertEqual(hunt._file_version(path), "2.0.20.3")
            fake.assert_called_once_with(path)

    def test_missing_jar_and_missing_manifest(self):
        self.assertEqual(hunt._file_version(str(self.root / "nope.jar")), None)
        jar = _make_jar(self.root / "plain.jar", None)
        # No manifest, no dotted token in the name -> unknown.
        self.assertEqual(hunt._file_version(str(jar)), None)
        jar2 = _make_jar(self.root / "plain-1.0.0.jar", None)
        self.assertEqual(hunt._file_version(str(jar2)), "1.0.0")


class HuntFileVersionBoundsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.params = {"paths": str(self.root)}

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **params):
        merged = dict(self.params)
        merged.update(params)
        out = hunt.run_hunt(hunt.hunt_file, merged)
        return json.loads(out)

    def test_log4j_example_from_the_spec(self):
        _make_jar(self.root / "log4j-core-2.14.1.jar",
                  "Implementation-Version: 2.14.1\n")
        _make_jar(self.root / "log4j-core-2.15.0.jar",
                  "Implementation-Version: 2.15.0\n")
        _make_jar(self.root / "log4j-core-2.17.1.jar",
                  "Implementation-Version: 2.17.1\n")

        body = self._run(name="log4j-core-*.jar", version_lt="2.17.1")
        matches = body["matches"]
        self.assertEqual(len(matches), 2)
        self.assertEqual(sorted(m["version"] for m in matches),
                         ["2.14.1", "2.15.0"])
        self.assertTrue(all(m["path"].startswith(str(self.root))
                            for m in matches))

    def test_unknown_version_never_matches_a_bound(self):
        _make_jar(self.root / "no-version.jar", None)
        self.assertEqual(self._run(name="*.jar", version_lt="9.9.9")["matches"], [])
        self.assertEqual(self._run(name="*.jar", version_eq="0.0.1")["matches"], [])

        # Mutation check: a None version slipping past the guard must crash
        # the bound comparison (compare(None, ...)), never produce a match.
        original = hunt._version_bound_holds
        def _leaky(version, key, bound, scheme):
            if version is None:
                return True
            return original(version, key, bound, scheme)
        with mock.patch.object(hunt, "_version_bound_holds", side_effect=_leaky):
            self.assertEqual(self._run(name="*.jar", version_lt="9.9.9")["matches"], [])

    def test_no_bound_no_version_io(self):
        _make_jar(self.root / "log4j-core-2.14.1.jar",
                  "Implementation-Version: 2.14.1\n")

        def _explode(_path):
            raise AssertionError("_file_version must not run without bounds")

        with mock.patch.object(hunt, "_file_version", side_effect=_explode):
            body = self._run(name="*.jar")
        self.assertEqual(len(body["matches"]), 1)
        self.assertEqual(body["matches"][0]["version"], None)

    def test_bounds_equality_and_greater(self):
        _make_jar(self.root / "app.jar", "Implementation-Version: 2.14.1\n")
        self.assertEqual(
            [m["version"] for m in
             self._run(name="*.jar", version_eq="2.14.1")["matches"]],
            ["2.14.1"])
        self.assertEqual(
            [m["version"] for m in
             self._run(name="*.jar", version_gte="2.14.1")["matches"]],
            ["2.14.1"])
        self.assertEqual(
            [m["version"] for m in
             self._run(name="*.jar", version_gt="2.14.1")["matches"]],
            [])
        self.assertEqual(
            [m["version"] for m in
             self._run(name="*.jar", version_lte="2.14.1")["matches"]],
            ["2.14.1"])


if __name__ == "__main__":
    unittest.main()
