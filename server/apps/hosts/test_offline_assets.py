"""The UI must not fetch anything from the internet at runtime.

An air-gapped install is a supported deployment. Vigil went out of its way to
make one work — the agent talks only to the server, the KEV snapshot is
bundled — and then loaded Chart.js from a CDN, so an air-gapped install had no
charts on any page. Vendored assets fix that; this stops it coming back.

It also stops a floating version: ``chart.js@4`` let a CDN release change the
dashboard with no commit here.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

#: Only *assets the page loads* — a <script src>, a <link href>, an <img src>.
#: An <a href> to GitHub is a link the operator may click, not a dependency.
_REMOTE = re.compile(
    r"""<(?:script|link|img)\b[^>]*?\b(?:src|href)\s*=\s*["']https?://""",
    re.I)

#: setup.html still pulls QRious for the TOTP enrolment QR code. QRious is
#: GPL v3, and Vigil ships a commercially-licensed apps_business/ alongside
#: AGPL core, so vendoring it is a licensing decision rather than a mechanical
#: one. Tracked; until it is made, the exception is written down rather than
#: silently passing.
KNOWN_REMOTE = {"setup.html"}

#: Lines exempt by content rather than by file. The webfonts degrade to the
#: fallback stacks in vigil.css rather than breaking anything, so they are a
#: cosmetic loss on an air-gapped install rather than a functional one —
#: unlike the charts, which simply did not render. Vendoring three variable
#: families is a separate decision about repo weight.
KNOWN_REMOTE_LINES = ("fonts.googleapis.com", "fonts.gstatic.com")


def _templates() -> list[Path]:
    root = Path(settings.BASE_DIR) / "templates"
    return sorted(root.rglob("*.html"))


class OfflineAssetTests(TestCase):
    def test_the_template_directory_is_where_we_think_it_is(self):
        """A guard that silently scans nothing passes forever."""
        names = {p.name for p in _templates()}
        self.assertIn("base.html", names)
        self.assertGreater(len(names), 5)

    def test_no_template_loads_an_asset_over_the_network(self):
        offenders = []
        for path in _templates():
            if path.name in KNOWN_REMOTE:
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if not _REMOTE.search(line):
                    continue
                if any(host in line for host in KNOWN_REMOTE_LINES):
                    continue
                offenders.append(f"{path.name}:{n}: {line.strip()[:100]}")
        self.assertEqual(
            offenders, [],
            "vendor the asset into static/js/vendor/ instead — an air-gapped "
            "install cannot reach a CDN:\n  " + "\n  ".join(offenders))

    def test_the_vendored_files_the_templates_reference_exist(self):
        base = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text()
        static_root = Path(settings.BASE_DIR) / "static"
        referenced = re.findall(r"""\{%\s*static\s+['"]((?:js|css)/vendor/[^'"]+)['"]""", base)
        self.assertTrue(referenced, "base.html references no vendored assets")
        for rel in referenced:
            self.assertTrue((static_root / rel).is_file(),
                            f"base.html references {rel}, which is not checked in")

    def test_every_vendored_file_records_its_provenance(self):
        """A binary blob with no version or licence is not maintainable."""
        readme = (Path(settings.BASE_DIR) / "static" / "js" / "vendor" / "README.md")
        self.assertTrue(readme.is_file(), "static/js/vendor/README.md is missing")
        text = readme.read_text()
        vendor_dir = readme.parent
        for path in sorted(vendor_dir.glob("*.js")):
            self.assertIn(path.name, text,
                          f"{path.name} is vendored but not described in vendor/README.md")
        self.assertIn("Licence", text)
        self.assertIn("sha256", text)


class WidgetDefaultsTests(TestCase):
    """A widget's default settings must name data the agent actually reports.

    The metric widgets originally defaulted to category "system", metric
    "cpu_percent" — neither of which exists. A freshly added chart rendered an
    empty window, which reads as broken rather than unconfigured.
    """

    def test_metric_defaults_name_a_category_the_agent_reports(self):
        from apps.dashboards.widgets import WIDGET_REGISTRY

        # What the collector actually emits; see apps/metrics ingest.
        reported = {
            "cpu": {"usage_percent", "load_1m"},
            "memory": {"usage_percent", "swap_usage_percent"},
            "disk": {"usage_percent"},
            "network": {"bytes_sent", "bytes_recv"},
        }
        for kind, spec in WIDGET_REGISTRY.items():
            settings = spec.get("settings") or {}
            if "metric" not in settings:
                continue
            category = settings["category"]["default"]
            metric = settings["metric"]["default"]
            self.assertIn(category, reported,
                          f"{kind} defaults to category {category!r}, which nothing reports")
            self.assertIn(metric, reported[category],
                          f"{kind} defaults to {category}/{metric}, which nothing reports")
