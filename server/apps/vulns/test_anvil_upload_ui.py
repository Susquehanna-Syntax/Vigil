"""QA-03: the Vulnerabilities page must be able to import an Anvil record."""
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def _template() -> str:
    return (Path(settings.BASE_DIR) / "templates/pages/_vulns.html").read_text(encoding="utf-8")


def _js() -> str:
    return (Path(settings.BASE_DIR) / "static/js/vigil-vulns.js").read_text(encoding="utf-8")


class AnvilUploadUiTests(SimpleTestCase):
    def test_button_and_input_wired(self):
        html = _template()
        self.assertIn('data-act="pickAnvilRecord"', html)
        self.assertIn('id="vulns-anvil-file"', html)
        self.assertIn('type="file"', html)
        self.assertIn('data-change="uploadAnvilRecord"', html)
        self.assertIn('data-act="refreshVulns"', html)

    def test_upload_posts_to_the_anvil_endpoint(self):
        js = _js()
        self.assertIn("function pickAnvilRecord", js)
        self.assertIn("async function uploadAnvilRecord", js)
        self.assertIn("/api/v1/vulns/anvil/", js)
        self.assertIn("formData.append('record'", js)
        self.assertIn("getCsrf()", js)
        self.assertIn("data.detail", js)
        self.assertIn("refreshVulns()", js)
