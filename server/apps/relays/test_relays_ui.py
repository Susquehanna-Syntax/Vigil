"""Settings → Relays, wired the way the rest of Settings is.

No JS runner here, same as the other *_ui / *_page suites in this repo: the
pane's markup, the handlers it names, and the strings an admin is shown are
pinned from source and from a rendered page. The buttons stay inert without the
dispatcher finding them on window, which apps.hosts.test_inline_handlers checks
for every ``data-act`` name in every template — a name with no function behind
it is a button that silently does nothing.
"""

import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import Role, UserProfile

REPO = Path(__file__).resolve().parents[3]
TEMPLATE = REPO / "server" / "templates" / "pages" / "_settings.html"
SETTINGS_JS = REPO / "server" / "static" / "js" / "vigil-settings.js"
CSS = REPO / "server" / "static" / "css" / "vigil.css"

#: Everything the pane's buttons name.
HANDLERS = ("loadRelays", "addRelay", "toggleRelay", "editRelay", "rotateRelayKey",
            "deleteRelay", "relayInstall")


class RelaysPaneMarkupTests(TestCase):
    """Rendered, not just read: the nav button and the pane both sit inside the
    dashboard view's context, and a broken tag there is invisible to a scan."""

    def setUp(self):
        admin = get_user_model().objects.create_user(username="relay-admin")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_login(admin)

    def test_rendered_settings_page_carries_the_relays_pane(self):
        html = self.client.get("/").content.decode()
        self.assertIn('data-pane="relays"', html)
        self.assertIn('id="relays-list"', html)
        self.assertIn('data-act="addRelay"', html)

    def test_the_pane_is_a_sibling_of_the_agents_and_users_panes(self):
        body = TEMPLATE.read_text(encoding="utf-8")
        navs = re.findall(r'<button class="settings-nav-item[^"]*" data-pane="(\w+)"', body)
        self.assertEqual(navs[navs.index("agents") + 1], "relays")

        panes = re.findall(r'<div class="settings-pane[^"]*" data-pane="(\w+)"', body)
        self.assertEqual(panes[panes.index("agents") + 1], "relays")
        self.assertIn("users", panes[panes.index("relays"):])

    def test_the_add_form_fields_exist(self):
        body = TEMPLATE.read_text(encoding="utf-8")
        for element_id in ("relays-usage", "relays-list", "relay-name", "relay-location",
                           "relay-url", "relay-priority"):
            self.assertIn(f'id="{element_id}"', body)


class RelaysJsWiringTests(TestCase):
    def setUp(self):
        self.js = SETTINGS_JS.read_text(encoding="utf-8")

    def test_every_named_handler_is_defined(self):
        for name in HANDLERS:
            self.assertRegex(
                self.js, rf'(?:async )?function {name}\(',
                f"{name} is named by a data-act attribute but never defined")

    def test_the_nav_handler_loads_the_pane_on_reveal(self):
        """Mutation guard: drop the loadRelays() call from the settings-nav
        click handler and this is the test that goes red.

        The slice ends at the listener's own closing `});`, so it holds the
        pane switches and nothing written after them — otherwise a later
        `loadRelays()` anywhere in the file would hide the missing call.
        """
        start = self.js.index("#settings-nav .settings-nav-item")
        nav = self.js[start:self.js.index("\n    });", start)]
        self.assertRegex(nav, r"dataset\.pane === 'relays'.*loadRelays\(\)")

    def test_the_compose_snippet_is_the_one_the_relay_container_needs(self):
        self.assertIn("susquehannasyntax/vigil-relay", self.js)
        self.assertIn("RELAY_KEY_SHA256", self.js)

    def test_no_inline_handlers_and_no_unescaped_interpolation(self):
        """An ``onclick="…"`` attribute in generated markup puts
        'unsafe-inline' back in the CSP. `.onclick =` property assignment (what
        vigil-sites.js and these dialogs do) is not one of them."""
        code = SETTINGS_JS.read_text(encoding="utf-8")
        for bad in ('onclick="', 'onchange="'):
            self.assertNotIn(bad, code, f"{bad} in vigil-settings.js")
        self.assertNotIn("${{", code)

    def test_server_values_reach_markup_escaped(self):
        """A relay's name, url and location are typed by whoever has admin and
        drawn into every row on this dashboard."""
        for field in ("name", "url", "location"):
            bare = re.compile(r"\$\{r\." + field + r"[?) ]")
            self.assertIsNone(bare.search(self.js),
                              f"`${{r.{field}}}` reaches markup without escHtml/escAttr")
        self.assertIn("escHtml(r.name)", self.js)
        self.assertIn("escHtml(r.url)", self.js)
        self.assertIn("escHtml(r.location)", self.js)
        self.assertIn("escAttr(r.id)", self.js)

    def test_the_install_commands_point_at_the_relay(self):
        self.assertIn("{origin}/agent/install.sh", self.js)
        self.assertIn("{origin}/agent/install.ps1", self.js)
        self.assertIn("/api/v1/agent-profiles/enroll-config/", self.js)


class RelaysCssTests(TestCase):
    def test_the_pane_has_styles_and_they_are_appended(self):
        css = CSS.read_text(encoding="utf-8")
        for selector in (".relay-row", ".relay-status", ".relay-code"):
            self.assertIn(selector, css)
        self.assertGreater(len(css.splitlines()), 4400)

    def test_no_coloured_side_border_on_relay_rows(self):
        css = CSS.read_text(encoding="utf-8")
        for block in re.findall(r"\.relay-[a-z-]+[^{]*\{[^}]*\}", css):
            self.assertNotIn("border-left", block, block)
            self.assertNotIn("border-right", block, block)


class RelayInstallCommandTests(TestCase):
    """The per-relay install command, run the way a host runs it.

    The one-time config token must reach the installer as VIGIL_CONFIG_TOKEN
    and the agent token as VIGIL_TOKEN: swapping them made the config token
    every new host's credential and left its agent.yml unredeemed.
    """

    def _command(self):
        import shutil
        import subprocess

        if not shutil.which("node"):
            self.skipTest("node is unavailable")
        js = (Path(__file__).resolve().parents[2] / "static" / "js" / "vigil-settings.js").read_text()
        constant = re.search(r"^const _RELAY_LINUX_CMD = .*$", js, re.M).group(0)
        builder = re.search(r"^function _relayLinuxCmd\(.*?^\}$", js, re.M | re.S).group(0)
        out = subprocess.run(
            ["node", "-e", f"{constant}\n{builder}\n"
             "process.stdout.write(_relayLinuxCmd('https://relay.test', 'tok123', 'cfg456'));"],
            capture_output=True, text=True, timeout=30, check=True)
        return out.stdout

    def test_the_installer_gets_the_agent_token_and_the_config_token_apart(self):
        import subprocess

        command = self._command()
        self.assertIn("curl -fsSL https://relay.test/agent/install.sh", command)
        run = command.replace(
            "curl -fsSL https://relay.test/agent/install.sh",
            "printf %s 'printf \"%s %s\" \"$VIGIL_TOKEN\" \"$VIGIL_CONFIG_TOKEN\"'",
        ).replace("sudo ", "")
        result = subprocess.run(["bash", "-c", run], capture_output=True, text=True,
                                timeout=30, check=False)
        self.assertEqual(result.stdout, "tok123 cfg456", result.stderr)

    def test_the_config_names_this_relay_as_the_primary(self):
        js = (Path(__file__).resolve().parents[2] / "static" / "js" / "vigil-settings.js").read_text()
        self.assertIn("overrides: { primary_relay: String(relay.id) }", js)


class DashboardEndpointsSurviveTheGateTests(TestCase):
    """Turning the built-in relay off refuses agents that come direct; it must
    not refuse the admin's own browser on dashboard endpoints."""

    def test_agent_binaries_list_loads_with_the_builtin_relay_off(self):
        from apps.relays.models import Relay

        Relay.objects.filter(kind=Relay.Kind.BUILTIN).update(enabled=False)
        admin = get_user_model().objects.create_user("gate-admin", password="pw")
        UserProfile.objects.create(user=admin, role=Role.ADMIN)
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/agent/info/").status_code, 200)
