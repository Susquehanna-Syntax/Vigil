"""File and package actions report what they touched — for packages, the version now installed."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil_agent import executor, pkg_manager
from vigil_agent.config import AgentConfig
from vigil_agent.pkg_manager import PackageManager


def _config(**kw):
    base = dict(server_url="https://vigil.example.com", agent_token="t", mode="full_control",
                data_dir=Path("/nonexistent-vigil-data"))
    base.update(kw)
    return AgentConfig(**base)


def _pm_run(answers):
    def run(cmd, timeout=None):
        joined = " ".join(cmd)
        for key, value in answers.items():
            if key in joined:
                if isinstance(value, Exception):
                    raise value
                return value
        return "ok"
    return run


class FileOutputTests(unittest.TestCase):
    def test_file_actions_report_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.txt"
            out = executor._write_file({"path": str(f), "content": "hello"}, _config())
            self.assertEqual(out.data, {"path": str(f), "bytes": 5})
            d = Path(tmp) / "dir"
            self.assertEqual(executor._create_directory({"path": str(d)}, _config()).data, {"path": str(d)})
            c = Path(tmp) / "b.txt"
            self.assertEqual(executor._copy_file({"src": str(f), "dest": str(c)}, _config()).data,
                             {"src": str(f), "dest": str(c)})
            m = Path(tmp) / "c.txt"
            self.assertEqual(executor._move_file({"src": str(c), "dest": str(m)}, _config()).data,
                             {"src": str(c), "dest": str(m)})
            self.assertEqual(executor._set_permissions({"path": str(m), "mode": "0600"}, _config()).data,
                             {"path": str(m)})
            self.assertEqual(executor._delete_path({"path": str(m)}, _config()).data,
                             {"path": str(m), "recursive": False})
            self.assertEqual(executor._delete_path({"path": str(d), "recursive": True}, _config()).data,
                             {"path": str(d), "recursive": True})


class PackageOutputTests(unittest.TestCase):
    def _run_with(self, name, answers, handler, params):
        pm = PackageManager(name=name)
        with patch.object(executor, "detect_pkg_manager", return_value=pm), \
             patch.object(pkg_manager, "_run", _pm_run(answers)), \
             patch.object(executor, "_assert_initramfs_clean", side_effect=lambda t: t):
            return handler(params, _config())

    def test_install_reports_version_apt(self):
        out = self._run_with("apt-get", {"dpkg-query": "1.2.3-1ubuntu1"},
                             executor._install_package, {"package_name": "curl"})
        self.assertEqual(out.data, {"package": "curl", "manager": "apt-get",
                                    "installed_version": "1.2.3-1ubuntu1"})

    def test_installed_version_rpm_pacman_brew_snap(self):
        cases = {"dnf": ({"rpm -q": "8.0.1-3.fc40"}, "8.0.1-3.fc40"),
                 "pacman": ({"pacman -Q": "curl 8.9.1-2"}, "8.9.1-2"),
                 "brew": ({"brew list": "curl 8.10.0"}, "8.10.0"),
                 "snap": ({"snap list": "Name Version Rev\ncurl 8.4.0 1754"}, "8.4.0")}
        for name, (answers, version) in cases.items():
            with patch.object(pkg_manager, "_run", _pm_run(answers)):
                self.assertEqual(PackageManager(name=name).installed_version("curl"), version, name)

    def test_installed_version_unknown_is_empty(self):
        for name in ("apk", "winget"):
            self.assertEqual(PackageManager(name=name).installed_version("curl"), "", name)

    def test_version_query_failure_does_not_fail_the_step(self):
        out = self._run_with("apt-get", {"dpkg-query": RuntimeError("no such package")},
                             executor._update_package, {"package_name": "curl"})
        self.assertEqual(out.data["installed_version"], "")

    def test_remove_reports_package(self):
        out = self._run_with("apt-get", {}, executor._remove_package, {"package_name": "curl"})
        self.assertEqual(out.data, {"package": "curl", "manager": "apt-get"})

    def test_package_updates_report_manager_on_every_path(self):
        pm = PackageManager(name="apt-get")
        with patch.object(executor, "detect_pkg_manager", return_value=pm), \
             patch.object(pkg_manager, "_run", _pm_run({})), \
             patch.object(executor, "_run", _pm_run({})), \
             patch.object(executor, "_assert_initramfs_clean", side_effect=lambda t: t):
            for security_only in (True, False):
                out = executor._run_package_updates({"security_only": security_only}, _config())
                self.assertEqual(out.data, {"manager": "apt-get", "security_only": security_only})
        pm = PackageManager(name="pacman")
        with patch.object(executor, "detect_pkg_manager", return_value=pm), \
             patch.object(pkg_manager, "_run", _pm_run({})), \
             patch.object(executor, "_assert_initramfs_clean", side_effect=lambda t: t):
            out = executor._run_package_updates({"security_only": True}, _config())
            self.assertEqual(out.data, {"manager": "pacman", "security_only": True})


if __name__ == "__main__":
    unittest.main()
