"""The host drawer's line about how the agent's last self-update ended.

Every value in ``Host.agent_update`` was written by the agent, so what matters
is that the drawer renders it as text and that the markup hook exists: these
tests read the sources as text.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase
from vigil.css_text import design_css

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
CSS = ROOT / "static" / "css"
TEMPLATES = ROOT / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentUpdateDrawerTemplateTests(SimpleTestCase):

    def test_drawer_hides_the_update_line_below_the_version_line(self):
        html = _read(TEMPLATES / "base.html")
        version_at = html.index('id="detail-agent-version"')
        update_at = html.index('id="detail-agent-update"')
        self.assertGreater(update_at, version_at)
        tag = html[html.rindex("<", 0, update_at):html.index(">", update_at) + 1]
        self.assertIn("hidden", tag)


class AgentUpdateDrawerJsTests(SimpleTestCase):

    def setUp(self):
        self.js = _read(JS / "vigil-host-cards.js")

    def _body(self) -> str:
        start = self.js.index("function _renderDetailAgentUpdate(u) {")
        rest = self.js[start:]
        end = rest.index("\n}\n")
        return rest[:end]

    def test_renders_and_clears_on_every_drawer_open(self):
        self.assertIn("function _renderDetailAgentUpdate(u) {", self.js)
        refresh = self.js[self.js.index("async function _refreshDetailAgentVersion("):]
        refresh = refresh[:refresh.index("\n}\n")]
        self.assertIn("_renderDetailAgentUpdate(data.agent_update || null);", refresh)
        open_fn = self.js[:self.js.index("function loadJackilRelated(")]
        self.assertIn("_renderDetailAgentUpdate(null);", open_fn)

    def test_update_values_never_reach_inner_html(self):
        body = self._body()
        self.assertNotIn("innerHTML", body)

    def test_all_three_states_are_handled(self):
        body = self._body()
        for state in ("confirmed", "rolled_back", "not_applied"):
            self.assertIn(state, body)


class AgentUpdateDrawerCssTests(SimpleTestCase):

    def test_tint_fills_the_box_and_there_is_no_side_stripe(self):
        css = design_css()
        rules = re.findall(r"\.detail-agent-update[^{]*\{[^}]*\}", css)
        self.assertTrue(rules, "no .detail-agent-update rule found")
        self.assertIn(".detail-agent-update--bad", css)
        for rule in rules:
            self.assertNotIn("border-left", rule)
