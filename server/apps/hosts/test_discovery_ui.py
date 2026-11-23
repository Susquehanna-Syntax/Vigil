"""The Discover dialog on the Inventory page (S9, phase 06e): its markup, script and CSS.

The dialog's contents are built with DOM calls from what the discovery endpoints
answer, so these tests read the sources as text: what matters is that every id
the script looks up exists in the modal, that the community box is neither shown
nor autofilled, that no scan value is ever placed with ``innerHTML`` (an IP, a
name and a sys_descr all arrive from a poller that got them from a device), and
that a scan row's state stays a tinted pill rather than growing a side stripe.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
CSS = ROOT / "static" / "css"

#: Every id openDiscovery and the scan code look up. One missing from the modal
#: and its button simply does nothing, which no other test here would notice.
MODAL_IDS = (
    "discovery-form",
    "discovery-poller",
    "discovery-cidr",
    "discovery-ports",
    "discovery-community",
    "discovery-help",
    "discovery-scans",
    "discovery-result",
)


def _discovery_modal(html: str) -> str:
    """The discovery modal element alone, so an id elsewhere cannot satisfy a test."""
    start = html.index('<div class="modal modal-wide discovery-modal" id="discovery-modal">')
    end = html.index('<div class="modal-overlay" id="task-detail-overlay"', start)
    return html[start:end]


def _inventory_buttons(inventory: str) -> str:
    """The Unmanaged Devices header's two buttons, in their own span."""
    start = inventory.index('<span class="dev-actions">')
    return inventory[start:inventory.index("</span>", inventory.index("Add Device", start))]


def _tag(html: str, needle: str) -> str:
    """The start tag of the element carrying ``needle`` — attribute order in a
    template is not something a test should have to repeat."""
    cut = html.index(needle)
    return html[html.rindex("<", 0, cut):html.index(">", cut)]


class DiscoveryButtonTests(SimpleTestCase):
    def setUp(self):
        self.inventory = (TEMPLATES / "pages" / "_inventory.html").read_text(encoding="utf-8")
        self.buttons = _inventory_buttons(self.inventory)

    def test_the_discover_button_sits_beside_add_device(self):
        self.assertIn('data-act="openDiscovery"', self.buttons)
        self.assertIn('data-act="openDeviceModal"', self.buttons)
        self.assertLess(self.buttons.index("openDiscovery"), self.buttons.index("openDeviceModal"))

    def test_the_button_carries_an_icon_of_its_own(self):
        tag = _tag(self.buttons, 'data-act="openDiscovery"')
        self.assertIn("btn-outline", tag)
        at = self.buttons.index('data-act="openDiscovery"')
        icon = self.buttons[at:self.buttons.index("</svg>", at)]
        self.assertIn('<svg viewBox="0 0 24 24"', icon)

    def test_the_script_opens_the_modal_by_that_name(self):
        js = (JS / "vigil-discovery.js").read_text(encoding="utf-8")
        self.assertRegex(js, r"(async )?function openDiscovery\(")


class DiscoveryMarkupTests(SimpleTestCase):
    def setUp(self):
        self.html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.modal = _discovery_modal(self.html)

    def test_every_id_the_script_looks_up_is_in_the_modal(self):
        for element_id in MODAL_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.modal)

    def test_the_modal_and_its_overlay_are_a_pair_named_as_the_close_function(self):
        # vigil-modal.js finds closeDiscovery() from "discovery-modal"; the
        # overlay carries the same prefix so Escape and a click outside work.
        self.assertIn('<div class="modal-overlay" id="discovery-overlay" data-act="closeDiscovery">',
                      self.html)
        self.assertIn('id="discovery-modal"', self.html)
        self.assertIn('data-act="closeDiscovery"', self.modal)

    def test_the_modal_is_wide_and_titled(self):
        self.assertIn('<div class="modal modal-wide discovery-modal" id="discovery-modal">', self.html)
        self.assertIn("Discover devices", self.modal)

    def test_the_form_and_the_result_start_hidden(self):
        # The form only once a poller this user may scan from is known to exist,
        # and the result only once a scan is opened.
        self.assertRegex(_tag(self.modal, 'id="discovery-form"'), r"\bhidden\b")
        self.assertRegex(_tag(self.modal, 'id="discovery-result"'), r"\bhidden\b")

    def test_the_community_box_is_neither_shown_nor_autofilled(self):
        # It holds an SNMP community: masked, and no browser offers to fill it.
        tag = _tag(self.modal, 'id="discovery-community"')
        self.assertIn('type="password"', tag)
        self.assertIn('autocomplete="off"', tag)

    def test_the_scan_button_names_its_handler(self):
        # The page's CSP carries no inline scripts: the button names its handler
        # with a data-submit attribute and vigil-discovery.js defines that name.
        # Assembled from pieces so this file cannot satisfy its own assertion.
        name = "startDiscovery" + "Scan"
        self.assertIn(f'data-submit="{name}"', self.modal)
        self.assertRegex((JS / "vigil-discovery.js").read_text(encoding="utf-8"),
                         rf"async function {name}\(")

    def test_every_box_in_the_form_is_labelled(self):
        for element_id in ("discovery-poller", "discovery-cidr", "discovery-ports",
                           "discovery-community"):
            with self.subTest(element_id=element_id):
                self.assertIn(f'for="{element_id}"', self.modal)

    def test_the_placeholders_are_the_ones_the_spec_named(self):
        # A ratchet on the wording: both are load-bearing — the first is the
        # shape a refused cidr explains against, the second names what an empty
        # ports box means.
        self.assertIn('placeholder="10.0.0.0/24"', _tag(self.modal, 'id="discovery-cidr"'))
        self.assertIn('placeholder="Ports (optional)"', _tag(self.modal, 'id="discovery-ports"'))
        # What an empty ports box means is said once, in the help line.
        help_tag = self.modal[self.modal.index('id="discovery-help"'):]
        self.assertIn("22, 23, 80, 443, 445, 3389, 8080 and 8443", help_tag)

    def test_the_help_line_says_what_a_scan_may_be(self):
        help_tag = self.modal[self.modal.index('id="discovery-help"'):]
        self.assertIn("Private IPv4 networks only", help_tag)
        self.assertIn("1024 addresses", help_tag)
        self.assertIn("next check-in", help_tag)

    def test_no_inline_style_anywhere_in_the_dialog(self):
        # A ratchet on another branch holds base.html to its count of them; the
        # dialog's every look lives in vigil.css instead.
        self.assertNotIn('style="', self.modal)
        self.assertNotIn('style="', _inventory_buttons(
            (TEMPLATES / "pages" / "_inventory.html").read_text(encoding="utf-8")))

    def test_no_inline_handler_in_the_dialog(self):
        for attribute in ("onclick", "onchange", "onsubmit"):
            with self.subTest(attribute=attribute):
                self.assertNotIn(attribute, self.modal)


class DiscoveryScriptTests(SimpleTestCase):
    def setUp(self):
        self.js = (JS / "vigil-discovery.js").read_text(encoding="utf-8")
        self.html = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    def test_the_script_is_loaded_after_the_device_registry_it_reloads(self):
        scripts = [m.group(1) for m in re.finditer(r"vigil-([a-z-]+)\.js", self.html)]
        self.assertLess(scripts.index("devices"), scripts.index("discovery"))

    def test_no_api_value_is_placed_with_innerhtml(self):
        """An answer's IP, name, sys_descr and error are placed as text only.

        A poller got every one of them from a device: a sys_descr of
        "<img src=x onerror=…>" has to land as the text it is."""
        self.assertNotIn("innerHTML", self.js)

    def test_the_script_names_the_endpoints_it_calls(self):
        for literal in ("discovery/pollers/", "discovery/scans/", "/adopt/"):
            with self.subTest(literal=literal):
                self.assertIn(literal, self.js)

    def test_the_functions_the_markup_and_the_page_call_exist(self):
        for name in ("openDiscovery", "closeDiscovery", "startDiscoveryScan",
                     "selectAllDiscoveryHosts", "adoptDiscoveryHosts", "deleteDiscoveryScan"):
            with self.subTest(name=name):
                self.assertRegex(self.js, rf"(async )?function {name}\(")

    def test_the_scan_rows_are_one_delegated_click(self):
        # Rows are drawn per load, so one page-level listener answers for all.
        self.assertIn("delegateClick('.discovery-scan-row'", self.js)

    def test_a_row_is_a_button_a_keyboard_reaches(self):
        self.assertIn('row.type = \'button\'', self.js)
        self.assertIn("'discovery-scan-row'", self.js)

    def test_the_poll_is_one_timeout_chain_that_closes_with_the_modal(self):
        self.assertIn("DISCOVERY_POLL_MS", self.js)
        self.assertRegex(self.js, r"function closeDiscovery\(\)[\s\S]*?_discoveryStopPolling\(\)")
        self.assertIn("clearTimeout(_discoveryTimer)", self.js)

    def test_the_community_is_sent_but_never_written_back_to_the_page(self):
        # The box is the only place it stands, and it is cleared once queued.
        body = re.search(r"async function _startDiscoveryScan\([\s\S]*?\n}", self.js).group(0)
        self.assertIn("communityBox.value", body)
        self.assertIn("communityBox.value = ''", body)
        # Nothing else reads the box, so nothing else can show it: not a toast,
        # not a line of the dialog.
        for other in re.split(r"\n(?:async )?function ", self.js):
            if other.startswith("_startDiscoveryScan"):
                continue
            self.assertNotIn("communityBox", other)

    def test_the_type_pick_is_copied_from_the_add_device_modal(self):
        # A second list of device types here would drift from the first.
        body = re.search(r"function _discoveryAdoptRow\([\s\S]*?\n}", self.js).group(0)
        self.assertIn("'device-type'", body)
        self.assertNotIn("router", body)

    def test_an_answer_is_read_by_the_names_the_api_answers_under(self):
        for field in ("sys_name", "sys_descr", "alive_by", "open_ports", "latency_ms",
                      "known", "truncated", "scanned", "found", "can_scan"):
            with self.subTest(field=field):
                self.assertIn(field, self.js)


class DiscoveryStyleTests(SimpleTestCase):
    def setUp(self):
        self.css = (CSS / "vigil.css").read_text(encoding="utf-8")

    def _rules(self, selector_prefix: str):
        """Every rule whose selector is *selector_prefix* or something under it.
        Same reader as test_device_checks_ui.py's: a prefix carries its trailing
        hyphen so a sibling class sharing the name's letters is not swept in."""
        css = re.sub(r"/\*.*?\*/", " ", self.css, flags=re.DOTALL)
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selectors = [s.strip() for s in match.group(1).split(",")]
            if not any(s == selector_prefix.rstrip("-.") or s.startswith(selector_prefix)
                       or (s + " ").startswith(selector_prefix.rstrip("-.") + "-")
                       for s in selectors):
                continue
            yield match.group(1).strip(), match.group(2)

    def test_the_dialogs_classes_have_rules(self):
        found = {sel for sel, _ in self._rules(".discovery-")}
        for wanted in (".discovery-form", ".discovery-help", ".discovery-scans",
                       ".discovery-scan-row", ".discovery-result", ".discovery-known",
                       ".discovery-adopt"):
            with self.subTest(wanted=wanted):
                self.assertIn(wanted, found)
        self.assertIn(".dev-actions", {sel for sel, _ in self._rules(".dev-actions")})

    def test_the_table_scans_sideways_in_its_wrapper_not_the_dialog(self):
        wrap = dict(self._rules(".discovery-table-wrap"))
        self.assertIn("auto", wrap[".discovery-table-wrap"])
        self.assertIn("overflow-x", wrap[".discovery-table-wrap"])

    def test_no_discovery_rule_uses_a_side_stripe(self):
        for selector, body in self._rules(".discovery-"):
            for line in body.split(";"):
                prop = line.split(":")[0].strip()
                self.assertNotIn(prop, ("border-left", "border-right"),
                                 f"{selector} colours one side")

    def test_the_state_pill_colours_come_from_tokens(self):
        # The modifiers are the state; the base rule is geometry only.
        states = [(sel, body) for sel, body in self._rules(".discovery-scan-state")
                  if "--" in sel]
        self.assertTrue(states, "no state modifier rules")
        for selector, body in states:
            wanted = ("--rose" if "failed" in selector
                      else "--mint" if "done" in selector else "--sky")
            self.assertIn(wanted, body, f"{selector} hard-codes a state colour: {body}")
