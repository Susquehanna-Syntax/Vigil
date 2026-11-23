"""The JavaScript catalog behind t() and tn() in vigil-utils.js."""

from django.test import TestCase


class JavaScriptCatalogTests(TestCase):
    def test_the_catalog_serves_gettext_and_ngettext(self):
        resp = self.client.get("/jsi18n/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp["Content-Type"])
        body = resp.content.decode()
        self.assertIn("django.gettext", body)
        self.assertIn("django.ngettext", body)

    def test_the_catalog_needs_no_inline_script_or_eval(self):
        # Loaded as <script src>, so it must run under script-src 'self'.
        body = self.client.get("/jsi18n/").content.decode()
        self.assertNotIn("eval(", body)
