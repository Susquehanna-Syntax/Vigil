"""Periodic refresh in the UI must go through ``pollingInterval``.

A bare ``setInterval`` keeps fetching and redrawing in a tab nobody is looking
at. With a few Vigil tabs open on a low-power machine that was enough to make
hovering a chart visibly stutter — the timers in the hidden tabs were competing
for the same main thread as the one in front of you.

``pollingInterval`` skips the tick while ``document.visibilityState`` is
hidden, and runs a single catch-up when the tab comes back so the numbers on
screen are never stale by a whole interval. This test exists so the next
periodic refresh someone adds gets that for free instead of reintroducing the
lag.

``vigil-utils.js`` is exempt: it is where ``pollingInterval`` is implemented.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

# The one file allowed to call setInterval directly — it is the implementation.
EXEMPT = {"vigil-utils.js"}

_SET_INTERVAL = re.compile(r"\bsetInterval\s*\(")


def _js_files() -> list[Path]:
    root = Path(settings.BASE_DIR) / "static" / "js"
    return sorted(p for p in root.rglob("*.js") if "node_modules" not in p.parts)


class PollingGuardTests(TestCase):
    def test_the_js_directory_is_where_we_think_it_is(self):
        """A guard that silently scans nothing passes forever."""
        files = _js_files()
        self.assertGreater(len(files), 10, "found almost no JS to check")
        self.assertIn("vigil-utils.js", {p.name for p in files})

    def test_no_bare_setinterval_outside_the_helper(self):
        offenders = []
        for path in _js_files():
            if path.name in EXEMPT:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if _SET_INTERVAL.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "use pollingInterval(fn, ms) instead of setInterval so the timer "
            "pauses while the tab is hidden:\n  " + "\n  ".join(offenders))

    def test_the_helper_still_gates_on_visibility(self):
        """If this stops being true the guard above is protecting nothing."""
        src = (Path(settings.BASE_DIR) / "static" / "js" / "vigil-utils.js").read_text()
        self.assertIn("function pollingInterval", src)
        self.assertIn("visibilityState", src)
        self.assertIn("visibilitychange", src)

    def test_every_poller_is_cleared_through_the_helper(self):
        """clearInterval on a pollingInterval id leaks the catch-up record."""
        offenders = []
        for path in _js_files():
            if path.name in EXEMPT:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\bclearInterval\s*\(", line):
                    offenders.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "use clearPollingInterval(id) so the timer is forgotten as well "
            "as stopped:\n  " + "\n  ".join(offenders))
