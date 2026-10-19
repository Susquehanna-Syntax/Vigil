"""The topology map pans and zooms (QA-51, user 2026-10-16: "not being able
to move around in topology").

The cause: _topoSvg() checked `svg.attrs.id`, a property no DOM element has,
so it returned null in every browser and pan, zoom, Fit, search-centring and
link dimming all did nothing. Found by driving the page in Chromium; pinned
here by the source and by the pure zoom maths run in node.
"""

import json
import shutil
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from apps.reports.test_reports_ui import _code_only, _function, _node

ROOT = Path(__file__).resolve().parents[2]
JS = _code_only((ROOT / "static/js/vigil-topology.js").read_text())
CSS = (ROOT / "static/css/vigil.css").read_text()


class TopologyFindsItsMapTests(SimpleTestCase):

    def test_the_svg_is_found_by_a_real_dom_lookup(self):
        body = _function(JS, "_topoSvg")
        self.assertIn("querySelector('#topo-svg')", body)
        self.assertNotRegex(JS, r"svg\.attrs\b")

    def test_the_transform_group_is_the_svgs_own_g(self):
        self.assertIn("querySelector(':scope > g')", _function(JS, "_topoSvgRoot"))


class TopologyPanTests(SimpleTestCase):

    def test_a_drag_may_start_on_a_card(self):
        wire = _function(JS, "_topoWirePanZoom")
        self.assertNotIn("closest('.topo-card')) return", wire)
        self.assertIn("TOPO_DRAG_SLOP", wire)

    def test_the_click_that_ends_a_drag_never_reaches_the_card(self):
        wire = _function(JS, "_topoWirePanZoom")
        self.assertIn("if (drag.moving) swallowClick = true;", wire)
        self.assertIn("ev.stopPropagation();", wire)
        self.assertIn("}, true);", wire)

    def test_keys_pan_and_zoom(self):
        wire = _function(JS, "_topoWirePanZoom")
        for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "'+'", "'-'", "'0'"):
            self.assertIn(key, wire)

    def test_zoom_buttons_sit_on_the_canvas(self):
        self.assertIn("canvas.replaceChildren(svg, _topoZoomControls())", JS)
        for how in ("in", "out", "fit"):
            self.assertIn(f'data-topo-zoom="{how}"', _function(JS, "_topoZoomControls"))
        self.assertIn("delegateClick('[data-topo-zoom]'", JS)
        self.assertIn(".topo-canvas { position: relative; }", CSS)


@skipUnless(shutil.which("node"), "node is not installed")
class TopologyZoomMathsTests(SimpleTestCase):

    def _zoom(self, view, px, py, factor):
        code = "\n".join([
            "const TOPO_ZOOM_MIN = 0.25, TOPO_ZOOM_MAX = 2.5;",
            _function(JS, "topoZoomAt"),
            f"console.log(JSON.stringify(topoZoomAt({json.dumps(view)}, {px}, {py}, {factor})));"])
        return json.loads(_node(code))

    def test_the_point_under_the_pointer_stays_put(self):
        view = {"x": 40, "y": -20, "k": 1}
        out = self._zoom(view, 300, 200, 2)
        # The map point under (300, 200) before and after.
        before = ((300 - view["x"]) / view["k"], (200 - view["y"]) / view["k"])
        after = ((300 - out["x"]) / out["k"], (200 - out["y"]) / out["k"])
        self.assertEqual(out["k"], 2)
        self.assertAlmostEqual(before[0], after[0])
        self.assertAlmostEqual(before[1], after[1])

    def test_zoom_is_clamped(self):
        self.assertEqual(self._zoom({"x": 0, "y": 0, "k": 2}, 0, 0, 10)["k"], 2.5)
        self.assertEqual(self._zoom({"x": 0, "y": 0, "k": 0.3}, 0, 0, 0.1)["k"], 0.25)
