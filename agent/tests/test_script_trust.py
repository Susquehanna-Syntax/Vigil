"""SEC-2: the agent runs a named script only if nobody but an administrator can change it.

Reproduced live on the Windows test VM (2026-10-07): a standard user could create
C:\\ProgramData\\Vigil\\scripts, own it, and plant a script that execute_script then
ran as LocalSystem. Linux checked only the file's mode bits, not its owner or the
directories above it.
"""
import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isort: split
from vigil_agent import scripttrust

USERS = "S-1-5-32-545"
ALICE = "S-1-5-21-1111111111-2222222222-3333333333-1001"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
FULL = 0x1F01FF
READ_EXECUTE = 0x1200A9


class FakePath:
    def __init__(self, name, uid=0, mode=0o100755):
        self.name, self._st = name, SimpleNamespace(st_uid=uid, st_mode=mode)

    def stat(self):
        return self._st

    def __str__(self):
        return self.name


class ChainTests(unittest.TestCase):
    def test_chain_runs_from_the_script_to_the_scripts_dir(self):
        root = Path("/etc/vigil/scripts")
        self.assertEqual(scripttrust.chain(root / "a" / "b.sh", root),
                         [root / "a" / "b.sh", root / "a", root])


class PosixTests(unittest.TestCase):
    def test_root_owned_not_group_writable_is_fine(self):
        self.assertEqual(scripttrust.posix_problem(FakePath("/s/x.sh")), "")

    def test_owned_by_someone_else_is_refused(self):
        self.assertIn("not owned by root", scripttrust.posix_problem(FakePath("/s/x.sh", uid=1000)))

    def test_group_writable_is_refused(self):
        self.assertIn("writable", scripttrust.posix_problem(FakePath("/s", mode=0o40775)))


class WindowsTests(unittest.TestCase):
    def test_locked_folder_is_fine(self):
        acl = {"owner": ADMINS, "rules": [{"sid": SYSTEM, "rights": FULL}, {"sid": ADMINS, "rights": FULL},
                                          {"sid": USERS, "rights": READ_EXECUTE}]}
        self.assertEqual(scripttrust.windows_problem(Path("C:/x"), acl), "")

    def test_folder_a_standard_user_created_is_refused(self):
        # What the VM showed: the creating user owns the folder with full control.
        acl = {"owner": ALICE, "rules": [{"sid": ALICE, "rights": FULL}, {"sid": USERS, "rights": READ_EXECUTE}]}
        self.assertIn("not an administrator", scripttrust.windows_problem(Path("C:/x"), acl))

    def test_users_allowed_to_write_is_refused(self):
        # ProgramData's inherited "Users: create files / write data".
        acl = {"owner": ADMINS, "rules": [{"sid": USERS, "rights": 0x2 | 0x4}]}
        self.assertIn(USERS, scripttrust.windows_problem(Path("C:/x"), acl))

    def test_a_check_that_cannot_complete_refuses(self):
        with patch.object(scripttrust, "os", SimpleNamespace(name="nt")), \
                patch.object(scripttrust, "windows_acl", side_effect=OSError("no powershell")):
            problem = scripttrust.untrusted(Path("C:/p/scripts/x.ps1"), Path("C:/p/scripts"))
        self.assertIn("could not check", problem)

    def test_untrusted_folder_above_a_trusted_file_is_refused(self):
        good = {"owner": ADMINS, "rules": [{"sid": SYSTEM, "rights": FULL}]}
        bad = {"owner": ALICE, "rules": [{"sid": ALICE, "rights": FULL}]}
        acls = {"x.ps1": good, "scripts": bad}
        with patch.object(scripttrust, "os", SimpleNamespace(name="nt")), \
                patch.object(scripttrust, "windows_acl", side_effect=lambda p: acls[Path(p).name]):
            problem = scripttrust.untrusted(Path("C:/p/scripts/x.ps1"), Path("C:/p/scripts"))
        self.assertIn("not an administrator", problem)


class RunTimeBackstopTests(unittest.TestCase):
    """The installer cannot revoke a handle opened before its lock, so the
    check that counts is the one execute_script makes every time it runs."""

    def test_execute_script_checks_trust_on_every_run(self):
        import inspect

        from vigil_agent import executor
        src = inspect.getsource(executor._execute_script)
        self.assertIn("untrusted(script_path, scripts_dir)", src)
        self.assertLess(src.index("untrusted(script_path, scripts_dir)"), src.index("_run([str(script_path)]"))


if __name__ == "__main__":
    unittest.main()
