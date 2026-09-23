"""The agent's machine identity cannot be its token.

The token is whatever the installer or admin wrote into agent.yml: it can be
rotated, copied to another box, or lost — re-enrolling a machine today creates
a second host record for the same physical machine. A stable per-machine id
(systemd machine-id, Windows MachineGuid, macOS IOPlatformUUID) identifies the
machine itself, so the server can later recognise a re-enrolment.
"""

import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from vigil_agent import collector


class MachineFingerprintTests(unittest.TestCase):
    def _patch_linux_read_text(self, values: dict[str, str]):
        def fake(*args, **kwargs):
            path = args[0] if args else kwargs.get("path")
            key = str(path)
            if key in values:
                return values[key]
            raise OSError(f"no such file: {key}")

        return unittest.mock.patch.object(Path, "read_text", autospec=True,
                                          side_effect=fake)

    def test_linux_reads_etc_machine_id(self):
        with unittest.mock.patch.object(collector.sys, "platform", "linux"), \
                self._patch_linux_read_text({"/etc/machine-id": "  abc-def-0123\n"}):
            self.assertEqual(collector.machine_fingerprint(), "abc-def-0123")

    def test_linux_falls_back_to_dbus_machine_id(self):
        with unittest.mock.patch.object(collector.sys, "platform", "linux"), \
                self._patch_linux_read_text({
                    "/var/lib/dbus/machine-id": " dbus-456\n",
                }):
            self.assertEqual(collector.machine_fingerprint(), "dbus-456")

    def test_windows_reads_machineguid(self):
        fake_winreg = types.ModuleType("winreg")
        calls = {}

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def open_key(hive, name, reserved, access):
            calls["hive"] = hive
            calls["name"] = name
            calls["access"] = access
            return _Key()

        def query_value_ex(key, value):
            self.assertEqual(value, "MachineGuid")
            return ("abc-123", 1)

        fake_winreg.HKEY_LOCAL_MACHINE = 0x80000002
        fake_winreg.KEY_READ = 0x20019
        fake_winreg.KEY_WOW64_64KEY = 0x0100
        fake_winreg.OpenKey = open_key
        fake_winreg.QueryValueEx = query_value_ex

        with unittest.mock.patch.object(collector.sys, "platform", "win32"), \
                unittest.mock.patch.dict(sys.modules, {"winreg": fake_winreg}):
            self.assertEqual(collector.machine_fingerprint(), "abc-123")

        self.assertEqual(calls["hive"], 0x80000002)
        self.assertEqual(calls["name"], r"SOFTWARE\Microsoft\Cryptography")
        self.assertTrue(calls["access"] & fake_winreg.KEY_WOW64_64KEY,
                        "a 32-bit agent must read the 64-bit hive")

    def test_an_unreadable_source_returns_empty(self):
        with unittest.mock.patch.object(collector.sys, "platform", "linux"), \
                self._patch_linux_read_text({}):
            self.assertEqual(collector.machine_fingerprint(), "")

    def test_the_fingerprint_is_truncated(self):
        with unittest.mock.patch.object(collector.sys, "platform", "linux"), \
                self._patch_linux_read_text({"/etc/machine-id": "x" * 500}):
            self.assertEqual(collector.machine_fingerprint(), "x" * 200)


if __name__ == "__main__":
    unittest.main()
