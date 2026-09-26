"""The privilege-mismatch warning must give advice that actually works.

It used to say: remove the `User=` line from the unit and restart. That is not
sufficient, and running it proved so — the monitor-mode unit also carries
`ProtectSystem=strict`, under which /boot is read-only even for uid 0:

    # systemd-run --property=ProtectSystem=strict \\
        /bin/sh -c 'id -u; mkdir -p /boot/vigil-probe-test'
    0
    mkdir: Read-only file system

An operator following the old advice exactly still got

    [Errno 30] Read-only file system: '/boot/vigil-reprovision'

which is the same one-failure-at-a-time confusion the warning exists to
prevent. Re-running the installer regenerates the unit from the mode in
agent.yml and is the complete fix.
"""

import tests._safety_net  # noqa: F401 — the guard, even when this file is run or imported on its own

import logging
import unittest
from unittest.mock import patch

from vigil_agent.__main__ import _warn_on_privilege_mismatch


class _Cfg:
    def __init__(self, mode):
        self.mode = mode


class PrivilegeMismatchAdvice(unittest.TestCase):
    def _warn_text(self, mode, euid):
        with patch("vigil_agent.__main__.os.geteuid", return_value=euid):
            with self.assertLogs("vigil", level="WARNING") as cm:
                _warn_on_privilege_mismatch(_Cfg(mode))
        return "\n".join(cm.output)

    def test_monitor_mode_says_nothing(self):
        with patch("vigil_agent.__main__.os.geteuid", return_value=997):
            with patch.object(logging.getLogger("vigil"), "warning") as w:
                _warn_on_privilege_mismatch(_Cfg("monitor"))
                w.assert_not_called()

    def test_root_says_nothing(self):
        with patch("vigil_agent.__main__.os.geteuid", return_value=0):
            with patch.object(logging.getLogger("vigil"), "warning") as w:
                _warn_on_privilege_mismatch(_Cfg("managed"))
                w.assert_not_called()

    def test_managed_as_non_root_warns(self):
        self.assertIn("not running as root", self._warn_text("managed", 997))

    def test_the_advice_points_at_the_installer(self):
        text = self._warn_text("managed", 997)
        self.assertIn(
            "install.sh", text,
            "the only complete fix is re-running the installer, which "
            "regenerates the unit from the mode in agent.yml",
        )

    def test_the_advice_does_not_stop_at_removing_user(self):
        """Removing User= alone leaves ProtectSystem=strict in place."""
        text = self._warn_text("managed", 997)
        self.assertIn(
            "ProtectSystem", text,
            "advice that mentions only User= is incomplete: /boot stays "
            "read-only for root under ProtectSystem=strict, and reprovision "
            "still fails",
        )


if __name__ == "__main__":
    unittest.main()
