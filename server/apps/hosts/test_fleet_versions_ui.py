"""Settings > Agent Binary shows the fleet's agent versions (read as text)."""

import re
from pathlib import Path

from django.test import SimpleTestCase
from vigil.css_text import design_css

SERVER = Path(__file__).resolve().parents[2]


def _read(relative):
    return (SERVER / relative).read_text(encoding="utf-8")


class FleetVersionsMarkupTests(SimpleTestCase):
    def test_block_sits_after_agent_versions_line(self):
        html = _read("templates/pages/_settings.html")
        agent = html.index('id="agent-versions"')
        fleet = html.index('id="fleet-versions"')
        self.assertGreater(fleet, agent)


class FleetVersionsScriptTests(SimpleTestCase):
    def setUp(self):
        self.js = _read("static/js/vigil-settings.js")
        start = self.js.index("async function loadFleetVersions()")
        end = self.js.index("function copyInstallCmd()", start)
        self.body = self.js[start:end]

    def test_defines_and_fetches_endpoint(self):
        self.assertIn("async function loadFleetVersions()", self.js)
        self.assertIn("/api/v1/hosts/agent-versions/", self.body)

    def test_called_next_to_load_agent_info(self):
        html = _read("templates/pages/_settings.html")
        self.assertIn("loadAgentInfo(); loadFleetVersions();", html)
        upload = self.js.index("loadAgentInfo();\n    loadFleetVersions();")
        self.assertGreater(upload, 0)

    def test_body_never_uses_innerhtml(self):
        self.assertNotIn("innerHTML", self.body)
        self.assertNotIn("insertAdjacentHTML", self.body)


class FleetVersionsCssTests(SimpleTestCase):
    def test_rules_exist_without_side_stripes(self):
        css = design_css()
        rules = re.findall(r"(\.fleet-versions[^{]*)\{([^}]*)\}", css)
        selectors = [selector.strip() for selector, _ in rules]
        self.assertIn(".fleet-versions", selectors)
        for selector, body in rules:
            self.assertNotIn("border-left", body, selector)
