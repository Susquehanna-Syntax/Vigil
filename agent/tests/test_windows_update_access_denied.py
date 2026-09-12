"""An unprivileged agent cannot query Windows Update. Say so once, not hourly.

install.ps1 runs monitor mode as the virtual account NT SERVICE\\vigil-agent,
which is the point — the agent gives up privilege it does not need. But the
Windows Update COM API refuses that account, so on a real service install the
scan fails every cycle with

    com_error: (-2147352567, 'Exception occurred.',
                (0, None, None, None, 0, -2147024891), None)

where -2147024891 is E_ACCESSDENIED. Observed on a Windows 11 VM: the service
ran fine and ingested metrics, and logged a full traceback each interval for a
condition that cannot change while the process lives.

Reporting None stays correct — the server keeps what it knew rather than being
told zero by a machine that cannot count. Only the noise changes.
"""

import unittest
import unittest.mock
from unittest.mock import patch

from vigil_agent import windows_update


class _ComError(Exception):
    pass


ACCESS_DENIED = _ComError(
    -2147352567, "Exception occurred.",
    (0, None, None, None, 0, -2147024891), None,
)
OTHER_FAILURE = _ComError(
    -2147352567, "Exception occurred.",
    (0, None, None, None, 0, -2147467259), None,
)


class AccessDeniedDetection(unittest.TestCase):
    def test_recognises_access_denied(self):
        self.assertTrue(windows_update._is_access_denied(ACCESS_DENIED))

    def test_does_not_swallow_other_com_errors(self):
        self.assertFalse(windows_update._is_access_denied(OTHER_FAILURE))

    def test_ignores_unrelated_exceptions(self):
        self.assertFalse(windows_update._is_access_denied(ValueError("nope")))


class SummaryUnderAccessDenied(unittest.TestCase):
    def setUp(self):
        windows_update._access_denied_logged["said"] = False
        windows_update._summary_cache.update({"at": 0, "value": None})

    def _run(self, exc, times=1):
        backend = unittest.mock.Mock()
        backend.scan.side_effect = exc
        with patch.object(windows_update, "detect", return_value=backend):
            with self.assertLogs("vigil.windows_update", level="DEBUG") as cm:
                out = [windows_update.summary(force=True) for _ in range(times)]
        return out, cm.output

    def test_reports_no_summary_rather_than_zero(self):
        out, _ = self._run(ACCESS_DENIED)
        self.assertEqual(out, [None],
                         "reporting zero would tell the server this machine "
                         "is up to date when it simply could not look")

    def test_warns_once_then_stays_quiet(self):
        _, logs = self._run(ACCESS_DENIED, times=3)
        warnings = [l for l in logs if l.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, logs)
        self.assertIn("not permitted", warnings[0])

    def test_a_real_failure_still_warns_every_time(self):
        """Only the permanent privilege case is deduplicated."""
        _, logs = self._run(OTHER_FAILURE, times=2)
        warnings = [l for l in logs if l.startswith("WARNING")]
        self.assertEqual(len(warnings), 2, logs)


if __name__ == "__main__":
    unittest.main()
