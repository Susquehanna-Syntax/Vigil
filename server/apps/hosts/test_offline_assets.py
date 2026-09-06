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

#: Nothing is exempt. The QR generator is rendered server-side and the fonts
#: are self-hosted, so every asset the UI needs is in this repo.
KNOWN_REMOTE: set[str] = set()
KNOWN_REMOTE_LINES: tuple[str, ...] = ()


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


class SelfHostedFontTests(TestCase):
    """The SQSY typefaces ship with Vigil rather than being fetched per load."""

    def test_the_font_css_exists_and_is_referenced(self):
        base = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text()
        self.assertIn("css/fonts.css", base)
        self.assertTrue((Path(settings.BASE_DIR) / "static" / "css" / "fonts.css").is_file())

    def test_every_font_file_the_css_names_is_checked_in(self):
        css_path = Path(settings.BASE_DIR) / "static" / "css" / "fonts.css"
        css = css_path.read_text()
        referenced = re.findall(r"url\('\.\./fonts/([^']+)'\)", css)
        self.assertTrue(referenced, "fonts.css declares no font files")
        fonts_dir = Path(settings.BASE_DIR) / "static" / "fonts"
        for name in referenced:
            self.assertTrue((fonts_dir / name).is_file(),
                            f"fonts.css names {name}, which is not checked in")

    def test_the_three_sqsy_families_are_all_present(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "fonts.css").read_text()
        for family in ("DM Sans", "Fraunces", "IBM Plex Mono"):
            self.assertIn(f"font-family: '{family}'", css)


class ServerSideQrTests(TestCase):
    """The setup page draws its enrolment QR without fetching a library."""

    def test_the_qr_is_rendered_as_inline_svg(self):
        from apps.accounts.views import _totp_qr_svg

        svg = _totp_qr_svg("otpauth://totp/Vigil:admin?secret=ABCDEFGH&issuer=Vigil")
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertGreater(len(svg), 500, "suspiciously small for a QR code")

    def test_two_different_secrets_produce_different_codes(self):
        from apps.accounts.views import _totp_qr_svg

        a = _totp_qr_svg("otpauth://totp/Vigil:a?secret=AAAAAAAA&issuer=Vigil")
        b = _totp_qr_svg("otpauth://totp/Vigil:b?secret=BBBBBBBB&issuer=Vigil")
        self.assertNotEqual(a, b)

    def test_the_setup_template_no_longer_loads_a_qr_library(self):
        setup = (Path(settings.BASE_DIR) / "templates" / "setup.html").read_text()
        self.assertNotIn("qrious", setup.lower())
        self.assertIn("totp_qr_svg", setup)
