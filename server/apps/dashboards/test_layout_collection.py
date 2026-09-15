"""The saved layout must be the layout on screen.

Gridstack's `save()` is a lossy serialiser. `removeInternalForSave`
(gridstack-all.js, v12.4.0) ends with

    1!==e.w && e.w!==e.minW || delete e.w
    1!==e.h && e.h!==e.minH || delete e.h

so it drops `w` whenever the width is 1 *or* equal to the node's own `minW`,
and `h` on the same rule. `_dashRender` passes the catalogue's minW/minH into
every `addWidget`, so every widget sitting at its minimum comes back from
`save()` carrying no size at all.

Filling that hole from `DASH.current.widgets` — the geometry the *server* last
sent — means a resize down to the minimum is silently replaced by the size the
widget had before the resize. With `float: true` the restored, larger widget
then collides on the next load and Gridstack shoves its neighbours down, so one
discarded resize scrambles the whole board rather than one card.

The fix is to stop asking the lossy serialiser for geometry: the live node
behind each grid item always carries a complete x/y/w/h. These tests guard that,
and they run without a browser.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO = Path(__file__).resolve().parents[3]
DASHBOARDS_JS = REPO / "server" / "static" / "js" / "vigil-dashboards.js"


def _collect_layout_body() -> str:
    """The source of `_dashCollectLayout`, comments stripped.

    The comments in this function explain the very pattern being banned — they
    quote the `?? original.w` fallback in order to say why it was wrong — so
    scanning raw text would flag the explanation as the offence.
    """
    src = DASHBOARDS_JS.read_text()
    start = src.index("function _dashCollectLayout()")
    end = src.index("\nasync function _dashSaveLayout()", start)
    body = src[start:end]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return body


class LayoutCollectionTests(SimpleTestCase):
    """What gets sent on save is read off the grid, not off the last response."""

    def setUp(self):
        self.body = _collect_layout_body()

    def test_geometry_is_read_from_the_live_grid_items(self):
        self.assertIn(
            "getGridItems()", self.body,
            "_dashCollectLayout must walk the grid's own items, whose nodes "
            "always carry a complete geometry",
        )
        self.assertIn(
            "gridstackNode", self.body,
            "geometry must come from the live Gridstack node behind each item",
        )

    def test_the_lossy_serialiser_is_not_used_for_geometry(self):
        self.assertNotIn(
            ".save(", self.body,
            "Gridstack's save() deletes w/h for any widget at its minimum "
            "size; it cannot be the source of the geometry we persist",
        )

    def test_size_does_not_fall_back_to_the_last_server_response(self):
        for axis in ("w", "h"):
            self.assertNotRegex(
                self.body, rf"\?\?\s*original\.{axis}\b",
                f"falling back to original.{axis} sends the size the widget "
                f"had before the operator resized it, not the size on screen",
            )

    def test_kind_and_settings_still_come_from_the_client_copy(self):
        # Gridstack knows nothing about either, so these must keep looking the
        # widget up by id — the lookup whose miss used to drop a widget from
        # the payload entirely.
        self.assertIn("original.kind", self.body)
        self.assertIn("original.settings", self.body)
