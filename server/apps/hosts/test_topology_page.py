"""The Topology page's wiring (QA-32): the sidebar entry, the include, and the
rules the map is built on.

The map's geometry lives in ``layoutTopology``, which must stay pure — the same
response twice is the same map twice — and the page draws hostnames, addresses
and tags straight out of the fleet, so nothing reaches the DOM unescaped and no
inline handler comes back.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]

#: Inline handlers CSP refuses, in the shape a template or a built string has them.
INLINE = re.compile(r"""\bon(?:click|change|input|submit|error|load|mouse\w+|pointer\w+)\s*=""")

#: A response body assigned where a hostname could break out unescaped.
RAW_BODY = re.compile(r"""innerHTML\s*=\s*(?:data|body|resp(?:onse)?|json)\b""")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TopologyPageTests(SimpleTestCase):
    def test_the_page_is_in_the_sidebar_and_included(self):
        base = _read("templates/base.html")
        self.assertIn('data-page="topology"', base)
        self.assertIn("js/vigil-topology.js", base)
        self.assertIn('{% include "pages/_topology.html" %}', _read("templates/dashboard.html"))

    def test_the_page_carries_the_toolbar_canvas_and_panel(self):
        page = _read("templates/pages/_topology.html")
        for needle in ('id="page-topology"', 'id="topo-q"', 'id="topo-site"', 'id="topo-tags"',
                       'id="topo-banner"', 'id="topo-canvas"', 'id="topo-panel"',
                       'data-topo-toggle="containers"', 'data-topo-toggle="external"',
                       'id="topo-fit"', 'id="topo-refresh"'):
            self.assertIn(needle, page)
        self.assertIn('hidden', page.split('id="topo-banner"')[1][:40], "the banner starts hidden")

    def test_the_script_owns_the_map(self):
        js = _read("static/js/vigil-topology.js")
        for needle in ("/api/v1/hosts/topology/", "function layoutTopology(data, sitesIn, chosenSite)",
                       "function renderTopology(", "function fitTopology(", "function openTopoHost(",
                       "localStorage", "delegateClick(", "vigil-topo-tags", "pageName !== 'topology'",
                       "selectContainersHost(", "openHostDetail(", "2026.14.4 agent"):
            self.assertIn(needle, js)

    def test_layout_is_told_its_inputs_and_measures_only_the_canvas(self):
        """The map's shape comes from its arguments, and the one thing the layout
        may measure is the canvas it will be drawn in — wrapping needs a width."""
        js = _read("static/js/vigil-topology.js")
        start = js.index("function layoutTopology(")
        end = js.index("\nfunction _topoEdge(")
        body = js[start:end]
        for argument in ("data", "sitesIn", "chosenSite"):
            self.assertIn(argument, js[start:js.index(")", start) + 1])
        self.assertIn("clientWidth", body)
        self.assertNotIn("clientHeight", body, "the map wraps by width; the view fits the rest")
        self.assertNotIn("topoState.site", body, "the selected site is an argument, not a read")
        for forbidden in ("innerHTML", "createElementNS", "appendChild", "querySelector",
                          "replaceChildren"):
            self.assertNotIn(forbidden, body)

    def test_everything_drawn_is_escaped(self):
        js = _read("static/js/vigil-topology.js")
        for needle in ("escHtml(", "escAttr(", "textContent"):
            self.assertIn(needle, js, "hostnames, addresses and tags come from the fleet")
        self.assertNotRegex(js, RAW_BODY, "no response body goes to innerHTML")

    def test_no_inline_handlers_and_no_unescaped_attribute(self):
        for rel in ("static/js/vigil-topology.js", "templates/pages/_topology.html"):
            for line_no, line in enumerate(_read(rel).splitlines(), 1):
                self.assertNotRegex(line, INLINE, f"{rel}:{line_no}: {line.strip()[:80]}")
