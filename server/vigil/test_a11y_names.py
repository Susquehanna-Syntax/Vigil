"""Every select has a name, and every tab bar says so in the way screen readers read.

The axe pass (phase 05a) reported unnamed controls and labelled groups with no
role; both are fixed where the markup is built, not with overrides here. These
read the shipped artifacts — the templates, the dashboard picker's template
string, and vigil-nav.js — because a rule that only passes against a test fixture
would let the next refactor quietly drop the attribute again. Colour contrast is
phase 05a's territory; nothing here looks at CSS values.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from vigil.css_text import design_css

ROOT = Path(settings.BASE_DIR).parent
STATIC = ROOT / "server" / "static"
TEMPLATES = ROOT / "server" / "templates"
NAV_JS = STATIC / "js" / "vigil-nav.js"
RUN_PY = ROOT / "scripts" / "e2e" / "run.py"


def template(name: str) -> str:
    return (TEMPLATES / "pages" / name).read_text()


class SelectNameTests(SimpleTestCase):
    def test_the_app_source_select_names_itself(self):
        apps = template("_apps.html")
        select = re.search(r"<select[^>]*id=\"apps-source\"[^>]*>", apps)
        self.assertIsNotNone(select, "the app source select is gone from _apps.html")
        # Wrapped for translation in phase 09b; the rendered attribute is the
        # same string, so assert on the translate tag around it.
        self.assertIn('{% translate "App source" %}', select.group(0))

    def test_the_dashboard_picker_names_itself(self):
        # The picker is built in JavaScript, so its name lives in a template string.
        dashboards = (STATIC / "js" / "vigil-dashboards.js").read_text()
        select = re.search(r"<select[^>]*id=\"dash-switch\"[^>]*>", dashboards)
        self.assertIsNotNone(select, "the dashboard switcher is gone from vigil-dashboards.js")
        # Wrapped for translation in phase 10k; the rendered attribute is still
        # the same word, so assert on the t() call around it.
        self.assertIn("aria-label=\"${escAttr(t('Dashboard'))}\"", select.group(0))


class LabelledGroupRoleTests(SimpleTestCase):
    def test_the_category_list_is_a_group(self):
        store = template("_store.html")
        cats = re.search(r"<div[^>]*id=\"store-cats\"[^>]*>", store)
        self.assertIsNotNone(cats, "the category list is gone from _store.html")
        self.assertIn('role="group"', cats.group(0))
        self.assertIn("aria-label=", cats.group(0))

    def test_the_pinned_hosts_row_is_a_group(self):
        containers = template("_containers.html")
        pins = re.search(r"<div[^>]*id=\"containers-pins\"[^>]*>", containers)
        self.assertIsNotNone(pins, "the pinned hosts row is gone from _containers.html")
        self.assertIn('role="group"', pins.group(0))
        self.assertIn("aria-label=", pins.group(0))


class TabSemanticsTests(SimpleTestCase):
    def setUp(self):
        self.nav = NAV_JS.read_text()

    def test_the_tabs_pattern_is_applied_from_one_place(self):
        # Every .tab-bar on every page has to behave the same, so the wiring is
        # one function applied to all of them rather than per-page markup.
        self.assertIn("function initTabBar(bar)", self.nav)
        self.assertRegex(self.nav.replace(" ", ""), r"querySelectorAll\('\.tab-bar'\)\.forEach\(")

    def test_the_bar_is_a_tablist_of_tabs(self):
        self.assertIn("'role', 'tablist'", self.nav)
        self.assertIn("'role', 'tab'", self.nav)

    def test_each_tab_reports_whether_it_is_selected_and_focusable(self):
        self.assertIn("'aria-selected'", self.nav)
        self.assertIn("'tabindex'", self.nav)
        # Roving tabindex: exactly one tab is in the tab order at a time.
        self.assertRegex(self.nav.replace(" ", ""), r"'tabindex',on\?'0':'-1'")

    def test_a_tab_points_at_the_panel_it_controls(self):
        self.assertIn("'aria-controls'", self.nav)
        self.assertIn("'role', 'tabpanel'", self.nav)
        self.assertIn("'aria-labelledby'", self.nav)

    def test_keyboard_activation_goes_through_a_click(self):
        # Other scripts listen for clicks on tabs (vigil-tasks.js reloads its
        # library), so a key has to produce one rather than call activate().
        keys = self.nav.split("addEventListener('keydown'")[-1]
        self.assertRegex(keys, r"\.click\(\)")

    def test_arrows_home_end_space_and_enter_are_handled(self):
        for key in ("ArrowRight", "ArrowLeft", "Home", "End", "Enter"):
            self.assertIn(f"'{key}'", self.nav)
        self.assertRegex(self.nav, r"case ' ':")

    def test_the_focused_tab_gets_a_visible_ring(self):
        css = design_css()
        self.assertIn(".tab:focus-visible", css)


class KeyboardFlowTests(SimpleTestCase):
    """The browser flow scripts/e2e/run.py drives: alert tabs that the keyboard
    can move between, asserted against the source it runs — a live server is the
    architect's, and this suite must not start one."""

    def setUp(self):
        self.nav = NAV_JS.read_text()
        self.run = RUN_PY.read_text()

    def test_the_keyboard_flow_moves_a_tab_and_comes_back_home(self):
        flow = self.run.split('def keyboard_tabs(')[1].split("\n\ndef ")[0]
        for key in ("ArrowRight", "Home"):
            self.assertIn(f'press("{key}")', flow)
        # …and it checks what a real browser reports: the active tab, its ARIA
        # state, the element holding the focus, and the panel that came with it.
        for observed in ("aria-selected", "activeElement", 'after["panel"]', "classList.contains('active')"):
            self.assertIn(observed, flow)

    def test_the_keyboard_flow_checks_every_page_reported_to_the_backend(self):
        # A tab bar's keyboard is only one of the things a page can break; the
        # run reports problems to /api/v1/browser-errors/, and each flow ends by
        # draining them. A flow that forgets to would let its own failures pass.
        flow = self.run.split('def keyboard_tabs(')[1].split("\n@flow(")[0]
        self.assertIn("problems = s.drain()", flow)
        self.assertIn("assert not problems, problems", flow)

    def test_the_keyboard_flow_starts_where_a_tab_bar_leaves_the_keyboard(self):
        # A real browser has no keyboard position until the user gives it one:
        # focusing a tab by script is what Tab does. The harness cannot know that;
        # vigil-nav.js has to leave the focus where activation put it.
        flow = self.run.split('def keyboard_tabs(')[1].split("\n@flow(")[0]
        self.assertIn("focus()", flow)
        self.assertRegex(self.nav, r"next\.click\(\);\s*next\.focus\(\);")

    def test_the_keyboard_flow_is_part_of_the_suite_and_reports_a_screenshot(self):
        self.assertIn('keyboard_tabs(admin)', self.run)
        flow = self.run.split('def keyboard_tabs(')[1].split("\n@flow(")[0]
        self.assertIn('s.shot("keyboard-tabs")', flow)

