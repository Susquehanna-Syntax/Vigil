"""The Containers page owns container management; the Monitor page links to it."""
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class ContainersPageTests(SimpleTestCase):

    def test_the_page_is_in_the_sidebar_and_included(self):
        base = _read("templates/base.html")
        self.assertIn('data-page="containers"', base)
        self.assertIn("js/vigil-containers.js", base)
        self.assertIn('{% include "pages/_containers.html" %}', _read("templates/dashboard.html"))

    def test_the_page_carries_the_container_sections(self):
        page = _read("templates/pages/_containers.html")
        for needle in ('id="page-containers"', 'id="containers-host"', 'id="docker-stacks"',
                       'id="docker-count"', 'id="managed-stacks"'):
            self.assertIn(needle, page)

    def test_the_monitor_page_no_longer_manages_containers(self):
        mon_html = _read("templates/pages/_monitor.html")
        mon_js = _read("static/js/vigil-monitor.js")
        self.assertNotIn('id="docker-stacks"', mon_html)
        self.assertNotIn('id="managed-stacks"', mon_html)
        self.assertNotIn("renderDockerContainers", mon_js)
        self.assertNotIn("openContainerLogs", mon_js)
        self.assertIn('data-act="openContainersForCurrentMonitor"', mon_html)

    def test_the_containers_script_owns_rendering_and_logs(self):
        js = _read("static/js/vigil-containers.js")
        for needle in ("async function renderDockerContainers(hostId)", "function openContainerLogs",
                       "function selectContainersHost", "function openContainersForCurrentMonitor",
                       "pageName !== 'containers'"):
            self.assertIn(needle, js)
