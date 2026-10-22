"""The Connect menu and the web terminal, checked as source.

Both live in files no Python test can execute, and the two things most likely
to go wrong there are invisible to a green API suite: an attribute pointing at
a function nobody defined (a button that does nothing, with no error), and a
terminal that renders what the host sent as markup — which turns
``printf '<img src=x onerror=…>'`` into code running with the operator's
session cookie. Reading the files is how those are caught without a browser.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4] / "server"
TEMPLATES = ROOT / "templates"
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css" / "vigil.css"

CHANGED = [TEMPLATES / "_host_card.html", TEMPLATES / "pages" / "_monitor.html",
           JS / "vigil-host-cards.js", JS / "vigil-terminal.js",
           JS / "vigil-containers.js", JS / "vigil-monitor.js",
           JS / "vigil-widgets.js"]

#: An attribute of this shape is a handler the CSP will never run. The same
#: name as a DOM property (`el.onclick = fn`) is not: that is assignment to a
#: property of an element we created, which is legal under the policy.
INLINE_ATTR = re.compile(r"<[a-zA-Z][^>]*\son(click|change|input|submit|load)\s*=")


def read(path: Path) -> str:
    return path.read_text()


def _strip_js(src: str) -> str:
    """JS with its comments blanked out, one line per line.

    The ban being tested is on code, and these files' comments are written
    partly to explain the ban — so scanning raw text reads the explanation as
    the offence. apps/hosts/test_inline_handlers.py does the same for the same
    reason.
    """
    out, i, n = [], 0, len(src)
    in_line = in_block = False
    while i < n:
        ch, nxt = src[i], src[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
        elif in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        elif ch == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
            continue
        elif ch == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
            continue
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class HostCardTests(SimpleTestCase):
    def test_the_card_offers_one_connect_button(self):
        html = read(TEMPLATES / "_host_card.html")
        self.assertIn("openConnectMenu", html)
        self.assertIn("Connect", html)

    def test_the_card_no_longer_downloads_an_rdp_file_itself(self):
        # The .rdp download is a menu item now. A card carrying both would be
        # the second button this phase exists to remove.
        self.assertNotIn("downloadHostRdp", read(TEMPLATES / "_host_card.html"))


class MonitorPageTests(SimpleTestCase):
    def setUp(self):
        self.html = read(TEMPLATES / "pages" / "_monitor.html")

    def test_the_header_has_one_connect_button(self):
        self.assertIn("mon-connect-btn", self.html)
        self.assertIn('data-act="openConnectMenuForMonitor"', self.html)

    def test_ssh_and_rdp_are_no_longer_separate_buttons(self):
        for gone in ("mon-ssh-btn", "mon-rdp-btn"):
            self.assertNotIn(gone, self.html)

    def test_the_terminal_assets_are_named_for_on_demand_loading(self):
        for attr in ('data-js="{% static', 'data-xterm="{% static',
                     'data-fit="{% static', 'data-css="{% static'):
            self.assertIn(attr, self.html)
        self.assertIn("vigil-term-assets", self.html)

    def test_the_terminal_is_not_loaded_on_every_page_view(self):
        # base.html carries the vendor scripts every page needs. xterm is not
        # one of them; loadTerminalAssets() appends it on first use.
        self.assertNotIn("vigil-terminal.js", read(TEMPLATES / "base.html"))
        self.assertIn("{% load static %}", self.html.splitlines()[0])


class ConnectMenuTests(SimpleTestCase):
    def setUp(self):
        self.src = _strip_js(read(JS / "vigil-host-cards.js"))

    def test_the_menu_and_the_asset_loader_are_defined(self):
        for fn in ("openConnectMenu", "loadTerminalAssets"):
            self.assertRegex(self.src, rf"\bfunction {fn}\s*\(")

    def test_scripts_load_in_the_order_xterm_requires(self):
        order = [self.src.index(m) for m in
                 ("src.dataset.xterm", "src.dataset.fit", "src.dataset.js")]
        self.assertEqual(order, sorted(order))

    def test_menu_items_are_bound_with_addEventListener(self):
        # Generated markup, and the CSP has no 'unsafe-inline': an onclick set
        # on one of these rows is a menu item that does nothing.
        src = self.src[self.src.index("function openConnectMenu"):]
        self.assertNotIn(".onclick", src)
        self.assertIn("addEventListener('click'", src)

    def test_a_missing_asset_element_is_an_error_not_a_silent_hang(self):
        self.assertIn("terminal assets not found", self.src)

    def test_the_monitor_page_reuses_the_same_menu(self):
        monitor = _strip_js(read(JS / "vigil-monitor.js"))
        self.assertRegex(monitor, r"\bfunction openConnectMenuForMonitor\s*\(")
        self.assertIn("openConnectMenu(monitorHostId", monitor)

    def test_the_widget_card_opens_the_menu_instead_of_an_rdp_file(self):
        widgets = _strip_js(read(JS / "vigil-widgets.js"))
        self.assertIn('data-host-act="connect"', widgets)
        self.assertNotIn('data-host-act="rdp"', widgets)


class TerminalTests(SimpleTestCase):
    def setUp(self):
        self.src = _strip_js(read(JS / "vigil-terminal.js"))

    def test_every_base64_call_is_inside_the_utf_8_helper(self):
        # The helper is the only place raw text may reach the base64 step.
        src = self.src
        head = src[:src.index("function _b64FromUtf8")]
        helper = src[src.index("function _b64FromUtf8"):
                     src.index("function _bytesFromB64")]
        self.assertNotIn("btoa(", head)
        self.assertIn("btoa(", helper)
        self.assertEqual(src.count("btoa("), 1)

    def test_open_session_is_defined_and_writes_through_xterm(self):
        self.assertRegex(
            self.src, r"async function openSession\s*\(hostId,\s*kind,\s*container\)")
        self.assertIn("term.write(", self.src)

    def test_nothing_from_the_host_reaches_innerhtml(self):
        """A shell's output is arbitrary bytes. Any markup path lets the agent
        run script in the operator's session."""
        offenders = [line.strip() for line in self.src.splitlines()
                     if "innerHTML" in line and (".d" in line or "out" in line)]
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_input_is_encoded_as_utf_8_bytes_before_base64(self):
        # btoa(str) throws on the first character above U+00FF — a UTF-8
        # filename pasted into a shell is an ordinary thing to type.
        self.assertRegex(self.src, r"function _b64FromUtf8\s*\(")
        self.assertIn("new TextEncoder()", self.src)

    def test_the_resize_is_debounced_and_carries_the_new_size(self):
        # Debounced: a drag on the dialog's grip would otherwise fire a resize
        # — and a SIGWINCH on the agent — per frame.
        self.assertIn("ResizeObserver", self.src)
        self.assertRegex(self.src, r"t:\s*'resize'")
        self.assertIn(", 150);", self.src)

    def test_line_mode_is_taken_from_the_agent_rather_than_guessed(self):
        self.assertIn("msg.pty === false", self.src)


class ContainerShellTests(SimpleTestCase):
    def test_each_container_row_offers_a_shell(self):
        src = read(JS / "vigil-containers.js")
        self.assertIn("data-ctr-shell", src)
        self.assertRegex(src, r"openSession\([^)]*'container'")


class NoInlineHandlersTests(SimpleTestCase):
    def test_no_changed_file_carries_an_inline_handler(self):
        # The CSP has no 'unsafe-inline', so one of these is a handler that
        # never fires — which reads as "the Connect button does nothing".
        offenders = []
        for path in CHANGED:
            body = _strip_js(read(path)) if path.suffix == ".js" else read(path)
            for n, line in enumerate(body.splitlines(), 1):
                if INLINE_ATTR.search(line):
                    offenders.append(f"{path.name}:{n}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class StylesheetTests(SimpleTestCase):
    def test_the_terminal_dialog_is_styled(self):
        css = read(CSS)
        for selector in (".connect-menu", ".term-modal", ".term-host"):
            self.assertIn(selector, css)

    def test_the_dialog_is_resizable_the_way_the_logs_dialog_is(self):
        css = read(CSS)
        block = css[css.index(".modal.term-modal"):]
        self.assertIn("is-resized", block[:2000])

    def test_append_only_kept_the_stylesheet_longer(self):
        # vigil.css is append-only for this phase; a shorter file means
        # something was rewritten rather than added.
        self.assertGreater(len(read(CSS).splitlines()), 4600)


class TerminalTeardownTests(SimpleTestCase):
    """A keydown listener left on document after a session ends would close
    the next session too: termSession is shared between them."""

    def test_the_escape_listener_is_removed_on_teardown(self):
        js = (Path(__file__).resolve().parents[3] / "static" / "js" / "vigil-terminal.js").read_text()
        self.assertIn("document.addEventListener('keydown', onKey)", js)
        self.assertIn("document.removeEventListener('keydown', onKey)", js)

    def test_close_reasons_lose_every_control_character(self):
        js = (Path(__file__).resolve().parents[3] / "static" / "js" / "vigil-terminal.js").read_text()
        self.assertIn(r"replace(/[\x00-\x1f\x7f-\x9f]/g, '')", js)


class WebSocketServerDependencyTests(SimpleTestCase):
    """Uvicorn answers every WebSocket upgrade with 404 unless a WebSocket
    library is installed. The unit tests drive the ASGI app directly and never
    notice; a deployed image would serve the dashboard and no session."""

    def test_the_image_installs_a_websocket_library_for_uvicorn(self):
        root = Path(__file__).resolve().parents[3]
        for name in ("requirements.txt", "requirements.lock"):
            text = (root / name).read_text()
            self.assertRegex(text, r"(?m)^websockets[=>]", name)
