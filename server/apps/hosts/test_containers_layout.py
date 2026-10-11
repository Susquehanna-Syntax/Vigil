"""The Containers page layout (14.3): host picker and pins, sub-tabs for
containers / images / volumes / networks, a quick-filter bar, the storage
strip, and every removal going through a built-in task."""
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class ContainersLayoutTests(SimpleTestCase):
    def setUp(self):
        self.page = (ROOT / "templates/pages/_containers.html").read_text(encoding="utf-8")
        self.js = (ROOT / "static/js/vigil-containers.js").read_text(encoding="utf-8")

    def test_tabs_panels_and_filter_bar(self):
        for tab in ("containers", "images", "volumes", "networks"):
            self.assertIn(f'data-ctr-tab="{tab}"', self.page)
            self.assertIn(f'data-ctr-panel="{tab}"', self.page)
        for el in ('id="ctr-filter-q"', 'id="ctr-filter-chips"', 'id="ctr-storage"', 'id="containers-pins"'):
            self.assertIn(el, self.page)

    def test_removals_are_built_in_tasks(self):
        for name in ("Measure container storage", "Remove image", "Prune dangling images",
                     "Prune unused images", "Remove volume", "Prune unused volumes", "Remove network",
                     "Prune unused networks"):
            self.assertIn(f'data-ctr-task="{name}"', self.js)
        self.assertIn("openBuiltinTask(el.dataset.ctrTask, containersHostId, inputs)", self.js)
        self.assertNotIn("/images/prune", self.js, "the page never calls the engine itself")

    def test_the_page_opens_on_the_busiest_host(self):
        self.assertIn("const busiest = hosts.length && hosts[0].containers ? hosts[0].id : null;", self.js)
        self.assertIn("/api/v1/hosts/docker-summary/", self.js)
