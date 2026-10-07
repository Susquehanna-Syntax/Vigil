"""The browser half of the sign-in timeout (QA-14b) is wired the way it must be.

There is no JS runtime in the suite, so these read the shipped files as text.
Each check defends one behaviour the feature depends on: the heartbeat rides
real use and never a timer, a tab re-asks the server before it frightens anyone
or leaves, and nothing reaches for an inline handler the CSP would refuse.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "server" / "templates" / "base.html"
JS = REPO / "server" / "static" / "js"
SESSION = JS / "vigil-session.js"
UTILS = JS / "vigil-utils.js"

#: Every way the file may start a repeating timer. A heartbeat on a timer would
#: keep a session alive with nobody at the keyboard, so only the tick may.
_TIMERS = re.compile(r"\b(?:setInterval|pollingInterval)\s*\(([^,)]+)")


def _body(src: str, name: str) -> str:
    """A function's text, from its header to the next top-level definition.

    Exact enough for these checks because every function in the file is
    top-level and in source order; brace-matching would only add ways to be
    wrong about a file this small.
    """
    start = src.index(f"function {name}(")
    rest = src[start + 1:]
    nxt = re.search(r"^(?:async )?function |^document\.addEventListener", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


class SessionScriptLoadedTests(SimpleTestCase):
    def test_script_is_loaded_after_modal(self):
        html = TEMPLATE.read_text()
        self.assertIn("js/vigil-session.js", html)
        self.assertGreater(
            html.index("js/vigil-session.js"), html.index("js/vigil-modal.js"),
            "the timeout script needs mountModal, so it loads after the modal")


class HeartbeatTests(SimpleTestCase):
    def test_heartbeat_only_on_real_use(self):
        src = SESSION.read_text()
        for event in ("pointerdown", "keydown", "wheel", "touchstart"):
            self.assertIn(f"addEventListener('{event}', _sessionOnUse", src)
        self.assertIn("/api/v1/accounts/session/", src)
        self.assertIn("'activity/'", src)
        self.assertEqual(
            set(_TIMERS.findall(src)), {"_sessionTick"},
            "only the one-second tick may run on a timer — a heartbeat on a "
            "timer would keep a session alive with nobody at the keyboard")

    def test_warning_window_is_two_minutes(self):
        src = SESSION.read_text()
        self.assertIn("SESSION_WARN_SECONDS = 120", src)
        self.assertIn("HEARTBEAT_MS = 60 * 1000", src)


class RecheckTests(SimpleTestCase):
    def test_rechecks_before_warning_and_leaving(self):
        tick = _body(SESSION.read_text(), "_sessionTick")
        self.assertGreaterEqual(
            tick.count("_sessionFetch('GET')"), 2,
            "a tab asks the server before warning and before leaving: another "
            "tab's heartbeat extends this one")


class LeaveTests(SimpleTestCase):
    def test_leave_keeps_the_place(self):
        leave = _body(SESSION.read_text(), "_sessionLeave")
        self.assertIn("/login/?expired=", leave)
        self.assertIn("encodeURIComponent(location.pathname", leave)


class CspTests(SimpleTestCase):
    def test_no_inline_handlers_or_html_injection(self):
        src = SESSION.read_text()
        for handler in ("onclick=", "onchange="):
            self.assertNotIn(handler, src)
        # The countdown is re-written every second, so it must never be markup.
        warn = _body(src, "_sessionSetMessage")
        self.assertIn("msg.textContent = _sessionMessage(", warn)
        self.assertNotIn("innerHTML", warn)

    def test_apijson_redirects_on_session_401(self):
        utils = UTILS.read_text()
        self.assertIn("body.reason.startsWith('session_')", utils)
        self.assertIn("_sessionLeave(", utils)
