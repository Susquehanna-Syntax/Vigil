"""Device checks in the dashboard (S9, phase 04a): the markup, the script and the CSS.

The Checks section lives in a browser-built modal and is drawn with DOM calls, so
these tests read the sources as text: what matters is that the ids the script
looks up exist inside the device modal, that no API value is ever placed with
``innerHTML`` (a check's target, detail and URL come from an operator or a
device), and that the state dot stays a dot rather than growing a side stripe.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
CSS = ROOT / "static" / "css"

#: Every id openDeviceModal and the check code look up. If one is missing from
#: the modal the button simply does nothing, which no test would otherwise see.
MODAL_IDS = (
    "device-checks",
    "device-checks-list",
    "device-check-add",
    "device-check-kind",
    "device-check-target",
    "device-check-port",
    "device-check-community",
    "device-check-oids",
    "device-check-interval",
)


def _device_modal(html: str) -> str:
    """The device modal element alone, so an id elsewhere cannot satisfy a test."""
    start = html.index('<div class="modal" id="device-modal">')
    end = html.index('<script', start)
    return html[start:end]


def _tag(html: str, needle: str) -> str:
    """The start tag of the element carrying ``needle`` — attribute order in a
    template is not something a test should have to repeat."""
    cut = html.index(needle)
    return html[html.rindex("<", 0, cut):html.index(">", cut)]


class DeviceChecksMarkupTests(SimpleTestCase):
    def setUp(self):
        self.html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.modal = _device_modal(self.html)

    def test_the_checks_section_is_inside_the_device_modal(self):
        for element_id in MODAL_IDS:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.modal)

    def test_the_walk_checkbox_sits_in_a_hidden_label(self):
        label = re.search(r'<label[^>]*id="device-check-walk"[^>]*>.*?</label>', self.modal)
        self.assertIsNotNone(label, "no #device-check-walk label in the device modal")
        self.assertRegex(label.group(0), r'<label[^>]*\bhidden\b')
        self.assertIn('<input type="checkbox" id="device-check-interfaces">', label.group(0))

    def test_the_section_starts_hidden_and_ports_are_admin_wired(self):
        # A new device has no id to hang a check on, so the section is hidden
        # until the modal opens on an existing one.
        self.assertIn('id="device-checks" hidden', self.modal)
        self.assertIn('data-act="addDeviceCheck"', self.modal)

    def test_the_kind_options_are_the_five_a_poller_runs(self):
        for kind in ("icmp", "tcp", "http", "dns", "snmp"):
            self.assertIn(f'value="{kind}"', self.modal)

    def test_the_community_box_is_neither_shown_nor_autofilled(self):
        # It holds an SNMP community: masked, and no browser offers to fill it.
        tag = _tag(self.modal, 'id="device-check-community"')
        self.assertIn('type="password"', tag)
        self.assertIn('autocomplete="off"', tag)

    def test_the_v3_passwords_are_neither_shown_nor_autofilled(self):
        for box in ("device-check-v3-auth-password", "device-check-v3-priv-password"):
            with self.subTest(box=box):
                tag = _tag(self.modal, f'id="{box}"')
                self.assertIn('type="password"', tag)
                self.assertIn('autocomplete="off"', tag)

    def test_the_v3_row_and_the_version_pick_start_hidden(self):
        self.assertIn('hidden', _tag(self.modal, 'id="device-check-v3"'))
        version = _tag(self.modal, 'id="device-check-snmp-version"')
        self.assertIn('hidden', version)
        self.assertIn('data-change="setDeviceCheckSnmpVersion"', version)
        # SHA-256 first: the stronger protocol is the one a new check gets.
        auth = self.modal[self.modal.index('id="device-check-v3-auth"'):]
        self.assertLess(auth.index('value="sha256"'), auth.index('value="sha"'))

    def test_the_oids_box_is_a_plain_textarea_the_operator_can_read_back(self):
        # The OIDs list is configuration, not a secret: a text control.
        self.assertIn('<textarea', _tag(self.modal, 'id="device-check-oids"'))

    def test_the_list_carries_the_two_new_fields_to_the_page(self):
        """check_state and can_edit_checks are named in the script that reads them.

        openDeviceModal fills its form from the row the table hands it, so a
        field the list answers but the script never mentions would simply never
        appear — which no browser test here would catch.
        """
        js = (JS / "vigil-devices.js").read_text(encoding="utf-8")
        self.assertIn("check_state", js)         # renderDevices' dot
        self.assertIn("can_edit_checks", js)     # who may write checks

    def test_the_latency_chart_covers_a_day(self):
        # The spec asks for the last 24 hours, not whatever retention happens to hold.
        self.assertIn("hours=24", (JS / "vigil-devices.js").read_text(encoding="utf-8"))

    def test_no_device_check_code_uses_an_inline_onclick_handler(self):
        """Every check handler is delegated: the page's CSP has no 'unsafe-inline'.

        The device table used to interpolate ids into onclick attributes, and one
        comment still says so; the check rows and their buttons must not.
        """
        code = "\n".join(line for line in (JS / "vigil-devices.js").read_text(encoding="utf-8").splitlines()
                         if "onclick" in line and not line.lstrip().startswith(("//", "*", "/*")))
        self.assertEqual(code, "")
        self.assertNotIn("onclick", self.modal)


class DeviceChecksScriptTests(SimpleTestCase):
    def setUp(self):
        self.js = (JS / "vigil-devices.js").read_text(encoding="utf-8")
        self.html = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    def test_the_functions_the_markup_calls_exist(self):
        for name in ("loadDeviceChecks", "addDeviceCheck", "toggleDeviceCheckChart",
                     "setDeviceCheckKind", "deleteDeviceCheck"):
            with self.subTest(name=name):
                self.assertRegex(self.js, rf"(async )?function {name}\(")

    def test_the_add_button_is_a_delegated_action(self):
        # The page's CSP carries no inline scripts: the button names its handler
        # with a data-act attribute and vigil-devices.js defines that function.
        # The name is assembled from pieces so this file cannot satisfy its own
        # assertion by carrying the literal; grep base.html for the attribute.
        name = "addDevice" + "Check"
        self.assertIn(f'data-act="{name}"', _device_modal(self.html))
        self.assertRegex(self.js, rf"async function {name}\(")

    def test_no_api_value_is_placed_with_innerhtml_in_the_check_code(self):
        """Every function that renders a check builds nodes and sets textContent.

        Checked per function body rather than file-wide: renderDevices still
        lays out its table with escaped author-written markup, which is fine —
        what must never reach innerHTML is a check's target, detail or URL.
        """
        bodies = re.split(r"\n(?:async )?function ", self.js)
        relevant = [b for b in bodies
                    if b.split("(")[0].startswith(("_checkRow", "_checkLabel", "loadDeviceChecks",
                                                   "toggleDeviceCheckChart", "openDeviceChecks",
                                                   "_deviceEl", "_ms", "collapseDeviceCheckChart",
                                                   "_snmpValuesList", "_interfaceTable",
                                                   "_ifaceCell", "_interfaceNote",
                                                   "_loadCheckInterfaces", "_selectInterface"))]
        self.assertTrue(len(relevant) >= 5, "the check renderer is missing from vigil-devices.js")
        for body in relevant:
            name = body.split("(")[0]
            with self.subTest(function=name):
                self.assertNotIn("innerHTML", body)

    def test_the_values_list_is_built_from_text_never_markup(self):
        """An SNMP result's values come back from a device, verbatim: the one
        function that places them names Name and Uptime first, drops what the
        row already says, and builds every value with textContent."""
        body = next((b for b in re.split(r"\n(?:async )?function ", self.js)
                     if b.split("(")[0] == "_snmpValuesList"), None)
        self.assertIsNotNone(body, "vigil-devices.js has no _snmpValuesList")
        self.assertNotIn("innerHTML", body)
        for literal in ("'sys_name'", "'uptime_s'", "'Name'", "'Uptime'",
                        "'device-check-values'", "'kind'", "'target'", "textContent"):
            self.assertIn(literal, body)

    def test_the_interface_rows_are_one_delegated_click(self):
        # Rows are drawn per expansion, so one page-level listener answers for
        # all of them; the JS tests call _selectInterface directly.
        self.assertIn("delegateClick('.device-check-ifaces tr[data-if-index]'", self.js)
        self.assertRegex(self.js, r"_selectInterface\(el\.closest\('table'\), el\)")

    def test_the_interface_chart_is_destroyed_when_it_goes_away(self):
        body = re.search(r"function collapseDeviceCheckChart\(\)[\s\S]*?\n}", self.js).group(0)
        self.assertIn("_interfaceChart.destroy()", body)
        self.assertIn("_checkInterfaces.remove()", body)

    def test_the_latency_chart_is_destroyed_when_it_goes_away(self):
        # Two Chart.js instances on one canvas leak the first; collapse is the
        # only path out of an expanded row, and closing the modal uses it too.
        self.assertIn("_checkChart.destroy()", self.js)
        self.assertRegex(self.js, r"function closeDeviceModal\(\)[\s\S]*?collapseDeviceCheckChart\(\)")

    def test_the_empty_chart_explains_the_poller(self):
        self.assertIn("Settings > Agents", self.js)


class DeviceChecksStyleTests(SimpleTestCase):
    def setUp(self):
        self.css = (CSS / "vigil.css").read_text(encoding="utf-8")

    def _rules(self, selector_prefix: str):
        """Every rule whose selector is *selector_prefix* or something under it
        (``.device-check-row``, ``.device-check-dot--ok``), and not a sibling
        class that merely shares its name's first letters — the prefix carries
        its trailing hyphen for exactly that distinction."""
        css = re.sub(r"/\*.*?\*/", " ", self.css, flags=re.DOTALL)
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selectors = [s.strip() for s in match.group(1).split(",")]
            if not any(s == selector_prefix.rstrip("-.") or s.startswith(selector_prefix)
                       or (s + " ").startswith(selector_prefix.rstrip("-.") + "-")
                       for s in selectors):
                continue
            yield match.group(1).strip(), match.group(2)

    def test_the_rows_and_the_add_row_have_rules(self):
        rows = {sel.strip() for sel, _ in self._rules(".device-check-")}
        listed = {sel.strip() for sel, _ in self._rules(".device-checks")}
        self.assertTrue(any(s.startswith(".device-check-row") for s in rows), rows)
        self.assertIn(".device-check-add", rows)
        self.assertIn(".device-checks-list", listed)

    def test_a_sibling_class_is_not_mistaken_for_the_rows(self):
        # ".device-checks-list" is the container, not a row: the prefix scan for
        # rows must not answer with it, or the stripe test below would be
        # quietly checking a rule it never intended to.
        rows = {sel.strip() for sel, _ in self._rules(".device-check-")}
        self.assertNotIn(".device-checks-list", rows)

    def test_no_device_check_rule_uses_a_side_stripe(self):
        for selector, body in self._rules(".device-check"):
            for line in body.split(";"):
                prop = line.split(":")[0].strip()
                self.assertNotIn(prop, ("border-left", "border-right"),
                                 f"{selector} colours one side")

    def test_the_state_colours_come_from_tokens(self):
        # The modifiers are the state; the base rule is geometry only.
        for selector, body in self._rules(".device-check-dot"):
            if "--" not in selector:
                continue
            self.assertTrue(re.search(r"--mint|--rose|--text-3", body),
                            f"{selector} hard-codes a state colour: {body}")
